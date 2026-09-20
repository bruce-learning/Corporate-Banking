"""确认当前常驻服务响应、监听范围和秘密文件权限；不输出秘密。"""
import json
import os
import stat
import subprocess
import time
import urllib.request

import native
from app.config import Settings
from app import __version__


def main():
    settings = Settings()
    base = f"http://127.0.0.1:{settings.app_port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                assert json.load(response)["version"] == __version__
            break
        except urllib.error.URLError:
            time.sleep(0.1)
    else:
        raise RuntimeError("常驻 API 未启动成功")
    with urllib.request.urlopen(base + "/api/v1/health/dependencies", timeout=5) as response:
        assert response.status == 200 and all(json.load(response)["checks"].values())
    with urllib.request.urlopen(base + "/ready", timeout=5) as response:
        ready = json.load(response)
        assert response.status == 200 and ready["status"] == "ready" and all(ready["checks"].values())
    assert native.worker_pid(), "后台Worker未运行"
    ports = {settings.app_port, settings.mcp_port, settings.chroma_port, native.PGPORT, native.REDISPORT}
    listeners = {}
    for network in ["/proc/net/tcp", "/proc/net/tcp6"]:
        with open(network) as source:
            for line in list(source)[1:]:
                fields = line.split()
                address, port = fields[1].split(":")
                port = int(port, 16)
                if fields[3] == "0A" and port in ports:
                    assert address == "0100007F", "项目端口不是 IPv4 本机限定监听"
                    listeners[port] = "127.0.0.1"
    assert set(listeners) == ports
    for path in [native.ROOT / ".env", native.VAR / "secrets/admin-initial-password"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for user in ["postgres", "redis"]:
            assert subprocess.run(["runuser", "-u", user, "--", "test", "-r", str(path)]).returncode != 0
    # 既有 SSH 目录不改动；755 并不等于可写，拒绝组/其他用户写权限即可。
    assert stat.S_IMODE((native.ROOT.parent.parent / ".ssh").stat().st_mode) & 0o022 == 0
    print(json.dumps({"runtime": "passed", "listeners": listeners, "secrets_protected": True,
                      "bank_ready": True, "worker_running": True,
                      "paid_calls_enabled": settings.enable_paid_model_calls}))


if __name__ == "__main__":
    main()
