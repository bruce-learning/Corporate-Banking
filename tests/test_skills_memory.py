from pathlib import Path
from types import SimpleNamespace

from app.core.memory.service import MemoryService
from app.core.skills.manager import SkillManager
from app.infrastructure.llm.contracts import FakeModelClient
from tests.conftest import login
from tests.conftest import conversation


def test_lightweight_skills_match_agent_keyword_and_global(tmp_path: Path):
    global_dir = tmp_path / "global"
    product_dir = tmp_path / "product"
    global_dir.mkdir(); product_dir.mkdir()
    (global_dir / "SKILL.md").write_text(
        "---\nname: 全局规则\nkeywords:\nagents: product, policy\nenabled: true\n---\n\n# 全局规则\n不得越权。",
        encoding="utf-8",
    )
    (product_dir / "SKILL.md").write_text(
        "---\nname: 产品规则\nkeywords: 产品, 功能\nagents: product\nenabled: true\n---\n\n# 产品规则\n说明功能。",
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path)
    manager.load()
    product = manager.prompt_for("这个产品有什么功能", "product")
    policy = manager.prompt_for("这个产品有什么功能", "policy")
    assert product.names == ["全局规则", "产品规则"]
    assert policy.names == ["全局规则"]
    assert "不得越权" in product.text and "说明功能" in product.text


async def test_skill_admin_endpoint_is_protected(runtime):
    user = await login(runtime)
    assert (await runtime.client.get("/api/v1/admin/skills", headers=user)).status_code == 403
    manager = await login(runtime, "manager")
    response = await runtime.client.get("/api/v1/admin/skills", headers=manager)
    assert response.status_code == 200
    assert response.json()["count"] == 4
    reloaded = await runtime.client.post("/api/v1/admin/skills/reload", headers=manager)
    assert reloaded.status_code == 200 and reloaded.json()["errors"] == []


class MemoryVectorStore:
    def __init__(self):
        self.rows = {"summary": {}, "profile": {}}

    async def upsert_memory(self, kind, identifier, document, vector, metadata):
        self.rows[kind][identifier] = SimpleNamespace(
            id=identifier, document=document, metadata=metadata, distance=0.0
        )

    async def query_memory(self, kind, vector, *, user_id, tenant_id, n_results):
        return [
            row for row in self.rows[kind].values()
            if row.metadata["user_id"] == user_id and row.metadata["tenant_id"] == tenant_id
        ][:n_results]

    async def memory_count(self, kind):
        return len(self.rows[kind])

    async def delete_memory(self, kind, *, user_id, conversation_id=None):
        self.rows[kind] = {
            key: row for key, row in self.rows[kind].items()
            if not (row.metadata["user_id"] == user_id and
                    (conversation_id is None or row.metadata.get("conversation_id") == conversation_id))
        }


class CountingModel(FakeModelClient):
    def __init__(self):
        self.embed_calls = 0

    async def embed(self, texts):
        self.embed_calls += 1
        return await super().embed(texts)


async def test_memory_summary_profile_isolation_and_sensitive_filter(runtime):
    vectors = MemoryVectorStore()
    service = MemoryService(runtime.store, runtime.cache, vectors, FakeModelClient(), runtime.settings)
    messages = [
        {"role": "user", "content": "企业网银有哪些功能"},
        {"role": "assistant", "content": "合成资料回答"},
    ] * 6
    written = await service.remember(
        runtime.users["alice"].id, "conversation-a", messages, 12,
        "我们公司是小微企业",
    )
    assert written["available"] is True and len(written["written"]) == 2
    recalled = await service.recall(runtime.users["alice"].id, "企业网银")
    assert recalled.summary_ids and recalled.profile_ids
    hidden = await service.recall(runtime.users["bob"].id, "企业网银")
    assert not hidden.summary_ids and not hidden.profile_ids

    blocked = await service.remember(
        runtime.users["alice"].id, "conversation-a", messages, 14,
        "我的验证码是123456，我们公司是科技企业",
    )
    assert blocked["written"] == []
    assert all("科技企业" not in row.document for row in vectors.rows["profile"].values())


async def test_blocked_secret_never_reaches_memory_embedding(runtime):
    model = CountingModel()
    service = MemoryService(runtime.store, runtime.cache, MemoryVectorStore(), model, runtime.settings)
    recalled = await service.recall(runtime.users["alice"].id, "我的验证码是123456")
    assert recalled.reason == "memory_query_blocked"
    assert model.embed_calls == 0


async def test_working_memory_uses_redis_and_is_bounded(runtime):
    service = runtime.app.state.memory_service
    user_id = runtime.users["alice"].id
    conversation_id = "synthetic-working-memory"
    messages = [{"role": "user", "content": f"消息{index}"} for index in range(20)]
    await service.save_working(user_id, conversation_id, messages)
    cached = await service.working_context(user_id, conversation_id)
    assert len(cached) == runtime.settings.context_message_limit
    assert cached[0]["content"] == "消息8"


async def test_memory_management_endpoints_are_user_scoped(runtime):
    user_id = runtime.users["alice"].id
    runtime.app.state.memory_service.model_client = FakeModelClient()
    messages = [{"role": "user", "content": "我们公司是科技企业"},
                {"role": "assistant", "content": "合成回答"}] * 6
    await runtime.app.state.memory_service.remember(
        user_id, "memory-api-conversation", messages, 12, "我们公司是科技企业"
    )
    alice = await login(runtime, "alice")
    bob = await login(runtime, "bob")
    profiles = await runtime.client.get("/api/v1/memories/profiles", headers=alice)
    assert profiles.status_code == 200 and len(profiles.json()["items"]) == 1
    assert (await runtime.client.get("/api/v1/memories/profiles", headers=bob)).json()["items"] == []
    item = profiles.json()["items"][0]
    updated = await runtime.client.patch(
        f"/api/v1/memories/profiles/{item['id']}", headers=alice,
        json={"content": "用户明确表达：enterprise_type=制造业企业", "status": "disabled"},
    )
    assert updated.status_code == 200 and updated.json()["metadata"]["status"] == "disabled"
    assert (await runtime.client.delete(
        f"/api/v1/memories/profiles/{item['id']}", headers=bob
    )).status_code == 404
    assert (await runtime.client.delete(
        f"/api/v1/memories/profiles/{item['id']}", headers=alice
    )).status_code == 204


async def test_chat_enqueues_memory_without_waiting_for_embedding(runtime):
    from tests.test_chat_mvp import install_gateway
    install_gateway(runtime)
    headers = await login(runtime)
    identifier = await conversation(runtime, headers)
    response = await runtime.client.post("/api/v1/chat", headers=headers, json={
        "conversation_id": identifier, "message": "企业网银有什么功能？",
    })
    assert response.status_code == 200
    write = response.json()["memory_context"]["write"]
    assert write["status"] == "queued" and write["job_id"]
