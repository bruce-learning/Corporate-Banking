"""Create a fresh, secret Docker environment from the existing server model config."""

from __future__ import annotations

import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".env"
TARGET = ROOT / ".env.docker"
EXAMPLE = ROOT / ".env.docker.seetacloud.example"
ADMIN_SECRET = ROOT / "var" / "secrets" / "docker-admin-initial-password"


def values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def replace(text: str, key: str, value: str) -> str:
    prefix = key + "="
    rows = text.splitlines()
    for index, row in enumerate(rows):
        if row.startswith(prefix):
            rows[index] = prefix + value
            return "\n".join(rows) + "\n"
    raise RuntimeError(f"模板缺少配置项：{key}")


def main() -> None:
    if TARGET.exists() or ADMIN_SECRET.exists():
        raise RuntimeError("Docker 秘密配置已经存在，拒绝自动覆盖")
    existing = values(SOURCE)
    api_key = existing.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise RuntimeError("现有服务器 .env 未配置 DASHSCOPE_API_KEY")

    postgres_password = secrets.token_hex(32)
    redis_password = secrets.token_hex(32)
    content = EXAMPLE.read_text(encoding="utf-8")
    replacements = {
        "DASHSCOPE_API_KEY": api_key,
        "ENABLE_PAID_MODEL_CALLS": existing.get("ENABLE_PAID_MODEL_CALLS", "false"),
        "POSTGRES_PASSWORD": postgres_password,
        "DATABASE_URL": f"postgresql+asyncpg://bankapp:{postgres_password}@127.0.0.1:25432/bank",
        "REDIS_PASSWORD": redis_password,
        "REDIS_URL": f"redis://:{redis_password}@127.0.0.1:26379/0",
    }
    for key, value in replacements.items():
        content = replace(content, key, value)

    TARGET.write_text(content, encoding="utf-8")
    TARGET.chmod(0o600)
    ADMIN_SECRET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ADMIN_SECRET.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
    ADMIN_SECRET.chmod(0o600)
    print({"docker_env_created": True, "secrets_printed": False})


if __name__ == "__main__":
    main()
