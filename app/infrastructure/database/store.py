from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

SCHEMA_REVISION = "0008"


class Store:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, echo=False, hide_parameters=True, pool_pre_ping=True,
            pool_size=5, max_overflow=5, connect_args={"timeout": 3, "command_timeout": 3,
            "server_settings": {"statement_timeout": "3000"}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def ready(self) -> bool:
        async with self.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            for table in ["users", "auth_sessions", "conversations", "messages", "request_records",
                          "document_versions", "knowledge_chunks", "knowledge_indexes", "chat_traces",
                          "ingestion_jobs", "evaluation_runs", "bad_cases", "model_usage_records",
                          "background_jobs"]:
                await connection.execute(text(f"SELECT * FROM {table} LIMIT 0"))
            return revision == SCHEMA_REVISION

    async def close(self):
        await self.engine.dispose()
