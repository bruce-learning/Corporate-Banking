"""Create the initial Docker administrator without printing its password."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.core.application.auth import hash_password  # noqa: E402
from app.infrastructure.database.store import Store  # noqa: E402
from app.models.entities.storage import User  # noqa: E402


def initial_password() -> str:
    path = Path(os.environ.get("INITIAL_ADMIN_PASSWORD_FILE", "/run/secrets/admin_password"))
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("初始管理员密码文件不可读") from error
    if not 12 <= len(password) <= 128:
        raise RuntimeError("初始管理员密码必须为 12～128 字符")
    return password


async def create() -> None:
    settings = Settings()
    store = Store(settings.database_url.get_secret_value())
    try:
        async with store.sessions() as session:
            existing = await session.scalar(select(User).where(User.username == "admin"))
            if existing:
                if existing.role != "admin":
                    raise RuntimeError("admin 用户已存在但不是管理员，拒绝自动提权")
                print(json.dumps({"admin_created": False, "reason": "already_exists"}))
                return
            session.add(User(username="admin", role="admin", password_hash=await hash_password(initial_password())))
            await session.commit()
        print(json.dumps({"admin_created": True, "password_printed": False}))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(create())
