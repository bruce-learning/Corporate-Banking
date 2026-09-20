"""项目专属原生实例管理。秘密仅生成在受保护文件，不通过命令行或日志传递。"""
import asyncio
import getpass
import json
import os
from pathlib import Path
import pwd
import secrets
import signal
import socket
import subprocess
import sys
import time
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.core.application.auth import hash_password
from app.infrastructure.database.store import Store
from app.models.entities.storage import User
from redis import Redis
from sqlalchemy import select

VAR = ROOT / "var"
PGDATA = VAR / "postgresql"
PGSOCKET = VAR / "pgsocket"
REDISDIR = VAR / "redis"
PGBIN = Path("/usr/lib/postgresql/14/bin")
PGPORT, REDISPORT = 15432, 16379


def command(args, *, input=None, check=True):
    result = subprocess.run([str(value) for value in args], input=input, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        # 不打印参数、输入或数据库错误，避免配置秘密出现在日志。
        raise RuntimeError(f"子命令失败：{Path(str(args[0])).name}，退出码 {result.returncode}")
    return result


def as_postgres(args, **kwargs):
    return command(["runuser", "-u", "postgres", "--", *args], **kwargs)


def secret_file(path, content, owner=None):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(content)
    if owner:
        user = pwd.getpwnam(owner)
        os.chown(path, user.pw_uid, user.pw_gid)


def replace_env_values(path, values):
    """原子替换指定配置键；保留 API Key 等其他行且不输出内容。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    output, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            if key not in seen:
                output.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write("\n".join(output) + "\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def owned_directory(path, owner):
    path.mkdir(parents=True, exist_ok=True)
    user = pwd.getpwnam(owner)
    os.chown(path, user.pw_uid, user.pw_gid)
    path.chmod(0o700)


def free_port(port):
    with socket.socket() as probe:
        # TIME_WAIT 不是仍有监听进程；复用地址仍不能绑定活跃监听端口。
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"端口 {port} 已占用；拒绝操作未知实例") from None


def configuration():
    if os.geteuid() != 0:
        raise RuntimeError("当前原生初始化需 root 管理权限")
    ROOT.chmod(0o700)
    VAR.mkdir(exist_ok=True, mode=0o700)
    VAR.chmod(0o700)
    for folder in [VAR / "secrets", VAR / "logs", VAR / "run"]:
        folder.mkdir(exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    (VAR / "chroma").mkdir(exist_ok=True, mode=0o700)
    (VAR / "chroma").chmod(0o700)
    # 仅允许特定账户穿越父目录，不允许列举 /root 或读取其他子目录。
    for owner in ["postgres", "redis"]:
        for parent in [Path("/root"), Path("/root/autodl-tmp"), ROOT, VAR]:
            command(["setfacl", "-m", f"u:{owner}:--x", parent])
    for folder in [PGDATA, PGSOCKET]:
        owned_directory(folder, "postgres")
    owned_directory(REDISDIR, "redis")
    env = ROOT / ".env"
    if not env.exists():
        db_password, redis_password = secrets.token_hex(32), secrets.token_hex(32)
        secret_file(env, f"APP_ENV=development\nAPP_HOST=127.0.0.1\nAPP_PORT=8000\n"
            f"ENABLE_PAID_MODEL_CALLS=false\n"
            f"DATABASE_URL=postgresql+asyncpg://bankapp:{db_password}@127.0.0.1:{PGPORT}/bank\n"
            f"REDIS_URL=redis://:{redis_password}@127.0.0.1:{REDISPORT}/0\nCACHE_PREFIX=bank:v1\n")
    env.chmod(0o600)
    settings = Settings()
    db_url = urlsplit(settings.database_url.get_secret_value())
    redis_url = urlsplit(settings.redis_url.get_secret_value())
    if (db_url.hostname, db_url.port, db_url.path, db_url.username) != ("127.0.0.1", PGPORT, "/bank", "bankapp"):
        raise RuntimeError("原生脚本仅管理预期的独立 bank 数据库")
    if (redis_url.hostname, redis_url.port) != ("127.0.0.1", REDISPORT):
        raise RuntimeError("原生脚本仅管理预期的独立 Redis")
    redis_config = REDISDIR / "redis.conf"
    if not redis_config.exists():
        # 引号转义仅允许生成的十六进制密码。
        if not redis_url.password or not all(value in "0123456789abcdef" for value in redis_url.password):
            raise RuntimeError("初始化 Redis 密码格式不匹配")
        secret_file(redis_config, f"bind 127.0.0.1\nport {REDISPORT}\nprotected-mode yes\n"
            f"requirepass {redis_url.password}\ndir {REDISDIR}\ndaemonize yes\n"
            f"pidfile {REDISDIR / 'redis.pid'}\nlogfile {REDISDIR / 'redis.log'}\n"
            f"appendonly yes\nappendfsync everysec\nsave \"\"\nmaxmemory 128mb\nmaxmemory-policy noeviction\n",
            owner="redis")
    if not (PGDATA / "PG_VERSION").exists():
        as_postgres([PGBIN / "initdb", "-D", PGDATA, "--auth-local=peer", "--auth-host=scram-sha-256", "--encoding=UTF8"])
    pg_config = PGDATA / "bank.conf"
    if not pg_config.exists():
        secret_file(pg_config,
            f"listen_addresses='127.0.0.1'\nport={PGPORT}\nunix_socket_directories='{PGSOCKET}'\n"
            "password_encryption='scram-sha-256'\ntimezone='UTC'\nlog_statement='none'\n"
            "log_min_error_statement='panic'\nmax_connections=40\n", owner="postgres")
        with (PGDATA / "postgresql.conf").open("a", encoding="utf-8") as config:
            config.write("\ninclude='bank.conf'\n")
    return settings


def redis_client(settings):
    return Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=2,
                          socket_connect_timeout=2, decode_responses=True)


def start_pg():
    if as_postgres([PGBIN / "pg_ctl", "-D", PGDATA, "status"], check=False).returncode:
        free_port(PGPORT)
        as_postgres([PGBIN / "pg_ctl", "-D", PGDATA, "-l", PGDATA / "server.log", "-w", "start"])


def stop_pg():
    if not as_postgres([PGBIN / "pg_ctl", "-D", PGDATA, "status"], check=False).returncode:
        as_postgres([PGBIN / "pg_ctl", "-D", PGDATA, "-m", "fast", "-w", "stop"])


def redis_running(settings):
    try:
        client = redis_client(settings)
        pid = int((REDISDIR / "redis.pid").read_text())
        return bool(client.ping() and client.info("server")["process_id"] == pid)
    except Exception:
        return False


def start_redis(settings):
    if redis_running(settings):
        return
    free_port(REDISPORT)
    command(["runuser", "-u", "redis", "--", "redis-server", REDISDIR / "redis.conf"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if redis_running(settings):
            return
        time.sleep(0.1)
    raise RuntimeError("Redis 启动检查失败")


def stop_redis(settings):
    if redis_running(settings):
        redis_client(settings).shutdown(save=False)
        deadline = time.monotonic() + 5
        while (REDISDIR / "redis.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if (REDISDIR / "redis.pid").exists():
            raise RuntimeError("Redis 未按时退出，不操作未知进程")


def rotate_storage_credentials():
    """修复被示例配置覆盖的存储凭据，保留模型密钥和其他配置。"""
    if os.geteuid() != 0:
        raise RuntimeError("轮换存储凭据需要 root 管理权限")
    env = ROOT / ".env"
    redis_config = REDISDIR / "redis.conf"
    if not env.exists() or not redis_config.exists() or not (PGDATA / "PG_VERSION").exists():
        raise RuntimeError("原生实例尚未完成初始化")
    start_pg()
    db_password, redis_password = secrets.token_hex(32), secrets.token_hex(32)
    psql = ["psql", "-h", PGSOCKET, "-p", str(PGPORT), "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
    as_postgres(psql, input=f"ALTER ROLE bankapp PASSWORD '{db_password}';")

    config_text = redis_config.read_text(encoding="utf-8")
    match = re.search(r"(?m)^requirepass ([0-9a-f]+)$", config_text)
    if not match:
        raise RuntimeError("Redis 配置不是项目生成的受控格式")
    old_client = Redis(host="127.0.0.1", port=REDISPORT, password=match.group(1), socket_timeout=2)
    try:
        old_client.shutdown(save=False)
    except Exception:
        pass
    deadline = time.monotonic() + 5
    while (REDISDIR / "redis.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if (REDISDIR / "redis.pid").exists():
        raise RuntimeError("Redis 未按时停止，未改写配置")
    updated = re.sub(r"(?m)^requirepass [0-9a-f]+$", f"requirepass {redis_password}", config_text)
    temporary = redis_config.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(updated)
    user = pwd.getpwnam("redis")
    os.chown(temporary, user.pw_uid, user.pw_gid)
    os.replace(temporary, redis_config)

    replace_env_values(env, {
        "DATABASE_URL": f"postgresql+asyncpg://bankapp:{db_password}@127.0.0.1:{PGPORT}/bank",
        "REDIS_URL": f"redis://:{redis_password}@127.0.0.1:{REDISPORT}/0",
    })
    refreshed = Settings(_env_file=env)
    start_redis(refreshed)
    databases(refreshed)
    print("项目存储凭据已轮换；模型 Key 与其他配置未读取或输出")


def databases(settings):
    password = urlsplit(settings.database_url.get_secret_value()).password
    if not password or not all(value in "0123456789abcdef" for value in password):
        raise RuntimeError("数据库初始化密码格式不匹配")
    psql = ["psql", "-h", PGSOCKET, "-p", str(PGPORT), "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
    as_postgres(psql, input=f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bankapp') THEN "
        f"CREATE ROLE bankapp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}'; END IF; END $$;")
    for name in ["bank", "bank_test"]:
        exists = as_postgres([*psql, "-Atc", f"SELECT 1 FROM pg_database WHERE datname='{name}'"]).stdout.strip()
        if not exists:
            as_postgres(psql, input=f"CREATE DATABASE {name} OWNER bankapp;")
    command([ROOT / ".venv/bin/python", "-m", "alembic", "-c", ROOT / "alembic.ini", "upgrade", "head"])
    # 独立测试库同样通过真实迁移初始化；不复制或清空业务库。
    environment = os.environ.copy()
    environment["DATABASE_URL"] = settings.database_url.get_secret_value().rsplit("/", 1)[0] + "/bank_test"
    result = subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("测试数据库迁移失败")


async def account(username, role="user", password=None):
    if len(username) < 3 or len(username) > 64 or any(not (char.isascii() and (char.isalnum() or char in "_.-")) for char in username):
        raise ValueError("用户名格式不合法")
    if role not in {"user", "admin"}:
        raise ValueError("角色不合法")
    store = Store(Settings().database_url.get_secret_value())
    try:
        async with store.sessions() as session:
            if await session.scalar(select(User).where(User.username == username.lower())):
                return False
            password = password or getpass.getpass("新用户密码（至少 12 字符，不显示）：")
            if len(password) < 12 or len(password) > 128:
                raise ValueError("密码必须为 12～128 字符")
            session.add(User(username=username.lower(), password_hash=await hash_password(password), role=role))
            await session.commit()
            return True
    finally:
        await store.close()


def bootstrap():
    settings = configuration()
    start_pg()
    start_redis(settings)
    start_chroma(settings)
    databases(settings)
    password_path = VAR / "secrets/admin-initial-password"
    if not password_path.exists():
        password = secrets.token_urlsafe(24)
        if asyncio.run(account("admin", "admin", password)):
            secret_file(password_path, password + "\n")
    print("PostgreSQL、Redis、ChromaDB、数据库迁移和初始账户准备完成；秘密未输出")


def api_pid():
    try:
        pid = int((VAR / "run/api.pid").read_text())
        if str(ROOT / "scripts/run_api.py").encode() not in Path(f"/proc/{pid}/cmdline").read_bytes():
            return None
        return pid
    except (OSError, ValueError):
        return None


def mcp_pid():
    try:
        pid = int((VAR / "run/mcp.pid").read_text())
        if str(ROOT / "scripts/run_mcp.py").encode() not in Path(f"/proc/{pid}/cmdline").read_bytes():
            return None
        return pid
    except (OSError, ValueError):
        return None


def worker_pid():
    try:
        pid = int((VAR / "run/worker.pid").read_text())
        if str(ROOT / "scripts/run_worker.py").encode() not in Path(f"/proc/{pid}/cmdline").read_bytes():
            return None
        return pid
    except (OSError, ValueError):
        return None


def chroma_pid():
    try:
        pid = int((VAR / "run/chroma.pid").read_text())
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"chroma" not in command_line or b"run" not in command_line:
            return None
        return pid
    except (OSError, ValueError):
        return None


def chroma_running(settings):
    pid = chroma_pid()
    if not pid:
        return False
    try:
        with socket.create_connection((settings.chroma_host, settings.chroma_port), timeout=1):
            return True
    except OSError:
        return False


def start_chroma(settings):
    if settings.chroma_mode != "http" or chroma_running(settings):
        return
    free_port(settings.chroma_port)
    settings.chroma_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = VAR / "logs/chroma.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as target:
        process = subprocess.Popen([
            str(ROOT / ".venv/bin/chroma"), "run",
            "--path", str(settings.chroma_path),
            "--host", settings.chroma_host,
            "--port", str(settings.chroma_port),
        ], cwd=ROOT, stdout=target, stderr=target, start_new_session=True)
    (VAR / "run/chroma.pid").write_text(str(process.pid))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("ChromaDB 进程启动失败")
        if chroma_running(settings):
            return
        time.sleep(0.1)
    raise RuntimeError("ChromaDB 启动检查超时")


def stop_chroma(settings):
    pid = chroma_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while chroma_pid() and time.monotonic() < deadline:
            time.sleep(0.1)
        if chroma_pid():
            raise RuntimeError("ChromaDB 未按时停止，不强制操作未知进程")


def start_api(settings):
    if api_pid():
        return
    free_port(settings.app_port)
    log = VAR / "logs/api.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as target:
        process = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/run_api.py")],
            cwd=ROOT, stdout=target, stderr=target, start_new_session=True)
    (VAR / "run/api.pid").write_text(str(process.pid))


def stop_api():
    pid = api_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while api_pid() and time.monotonic() < deadline:
            time.sleep(0.1)
        if api_pid():
            raise RuntimeError("API 未按时停止，不强制删除数据或杀其他进程")


def start_mcp(settings):
    if mcp_pid():
        return
    free_port(settings.mcp_port)
    log = VAR / "logs/mcp.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as target:
        process = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/run_mcp.py")],
            cwd=ROOT, stdout=target, stderr=target, start_new_session=True)
    (VAR / "run/mcp.pid").write_text(str(process.pid))


def stop_mcp():
    pid = mcp_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while mcp_pid() and time.monotonic() < deadline:
            time.sleep(0.1)
        if mcp_pid():
            raise RuntimeError("MCP 未按时停止，不强制杀其他进程")


def start_worker():
    if worker_pid():
        return
    log = VAR / "logs/worker.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as target:
        process = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/run_worker.py")],
            cwd=ROOT, stdout=target, stderr=target, start_new_session=True)
    (VAR / "run/worker.pid").write_text(str(process.pid))


def stop_worker():
    pid = worker_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while worker_pid() and time.monotonic() < deadline:
            time.sleep(0.1)
        if worker_pid():
            raise RuntimeError("Worker 未按时停止，不强制删除任务")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "bootstrap":
        bootstrap()
        return
    settings = Settings()
    actions = {"start-pg": start_pg, "stop-pg": stop_pg,
        "start-redis": lambda: start_redis(settings), "stop-redis": lambda: stop_redis(settings),
        "start-chroma": lambda: start_chroma(settings), "stop-chroma": lambda: stop_chroma(settings),
        "start-api": lambda: start_api(settings), "stop-api": stop_api,
        "start-mcp": lambda: start_mcp(settings), "stop-mcp": stop_mcp,
        "start-worker": start_worker, "stop-worker": stop_worker}
    if action in actions:
        actions[action]()
    elif action == "rotate-storage-credentials":
        rotate_storage_credentials()
    elif action == "start":
        start_pg(); start_redis(settings); start_chroma(settings); start_mcp(settings); start_worker(); start_api(settings)
    elif action == "stop":
        stop_api(); stop_worker(); stop_mcp(); stop_chroma(settings); stop_redis(settings); stop_pg()
    elif action == "create-user":
        created = asyncio.run(account(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "user"))
        print("用户已创建" if created else "用户名已存在，未修改密码")
    elif action == "status":
        print(json.dumps({"api_running": bool(api_pid()), "mcp_running": bool(mcp_pid()),
            "worker_running": bool(worker_pid()),
            "chroma_running": chroma_running(settings), "redis_running": redis_running(settings),
            "postgres_running": not as_postgres([PGBIN / "pg_ctl", "-D", PGDATA, "status"], check=False).returncode,
            "paid_calls_enabled": settings.enable_paid_model_calls}))
    else:
        raise ValueError("不支持的管理操作")


if __name__ == "__main__":
    main()
