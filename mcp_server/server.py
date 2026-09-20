from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from app.config import Settings
from app.core.rag.hybrid import HybridSearchService, SearchResponse
from app.infrastructure.database.store import Store
from app.infrastructure.llm.aliyun import AliyunModelClient
from app.infrastructure.vector.chroma import ChromaVectorStore


@dataclass
class AppContext:
    store: Store
    model_client: AliyunModelClient
    vector_store: ChromaVectorStore
    search: HybridSearchService


def build_server(settings: Settings | None = None) -> MCPServer[AppContext]:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        store = Store(config.database_url.get_secret_value())
        # Keep keyword-only retrieval usable without a configured API key.
        # Paid model calls are still blocked by AliyunModelClient._guard().
        client = AliyunModelClient(config)
        vectors = ChromaVectorStore(config)
        try:
            yield AppContext(
                store=store, model_client=client, vector_store=vectors,
                search=HybridSearchService(store, client, vectors),
            )
        finally:
            await client.aclose()
            await vectors.close()
            await store.close()

    mcp = MCPServer("EchoMind Bank Corporate Knowledge", lifespan=lifespan)

    async def call(domain: str, query: str, top_k: int, ctx: Context[AppContext]) -> SearchResponse:
        return await ctx.request_context.lifespan_context.search.search(query, domain=domain, top_k=top_k)

    @mcp.tool()
    async def product_search(query: str, ctx: Context[AppContext], top_k: int = 5) -> SearchResponse:
        """检索虚构银行对公产品功能、适用场景、条件和边界。"""
        return await call("product", query, top_k, ctx)

    @mcp.tool()
    async def process_lookup(query: str, ctx: Context[AppContext], top_k: int = 5) -> SearchResponse:
        """检索虚构银行对公办理步骤、材料、渠道和时效。"""
        return await call("process", query, top_k, ctx)

    @mcp.tool()
    async def policy_lookup(query: str, ctx: Context[AppContext], top_k: int = 5) -> SearchResponse:
        """检索虚构银行对公收费、限制、合规和安全边界。"""
        return await call("policy", query, top_k, ctx)

    return mcp


mcp = build_server()


def main() -> None:
    settings = Settings()
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_sessions=20,
        session_idle_timeout=120.0,
        max_request_body_size=65536,
    )


if __name__ == "__main__":
    main()
