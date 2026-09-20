"""前台启动，绑定本机；用 Ctrl+C 停止，不隐式创建后台服务。"""

import sys
import logging
from pathlib import Path

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        app_dir=str(PROJECT_ROOT),
        reload=False,
        access_log=False,
    )
