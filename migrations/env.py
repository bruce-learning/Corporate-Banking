import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from app.config import get_settings
from app.models.entities.storage import Base


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def migrate():
    url = get_settings().database_url.get_secret_value()
    if not url:
        raise RuntimeError("未配置数据库，不能迁移")
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("迁移需连接独立 PostgreSQL")
else:
    asyncio.run(migrate())
