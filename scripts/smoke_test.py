"""自动启动临时本机进程、执行检查并停止；无模型调用。"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = os.environ.copy()
    environment.update(
        APP_HOST="127.0.0.1", APP_PORT=str(port), ENABLE_PAID_MODEL_CALLS="false"
    )
    process = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "run_api.py")],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("临时 API 进程意外退出")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as response:
                    result = json.load(response)
                    assert response.status == 200 and result["stage"] == "chromadb_rag_mcp"
                break
            except urllib.error.URLError:
                time.sleep(0.1)
        else:
            raise TimeoutError("健康检查未在截止时间内通过")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=2)
            raise AssertionError("聊天主链路完成前不应报告业务已就绪")
        except urllib.error.HTTPError as error:
            assert error.code == 503
        print(json.dumps({"health": "passed", "readiness": "expected_503", "paid_calls": 0}))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
