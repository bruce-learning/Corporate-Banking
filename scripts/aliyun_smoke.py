"""最小阿里云实调；需要双重显式授权，且不会打印密钥或输入全文。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.infrastructure.llm.aliyun import AliyunModelClient
from app.infrastructure.llm.contracts import ChatInput, ModelMessage


async def run(confirm: bool) -> dict:
    settings = get_settings()
    if not confirm:
        raise RuntimeError("必须传入 --confirm-paid-call")
    if not settings.enable_paid_model_calls:
        raise RuntimeError("ENABLE_PAID_MODEL_CALLS 必须为 true；可仅对本次命令临时设置")
    client = AliyunModelClient(settings)
    try:
        chat = await client.chat(ChatInput(
            messages=[ModelMessage(role="user", content="只回复：连接成功")],
            max_tokens=16,
        ))
        documents = await client.embed(["海岳银行是虚构演示银行。", "企业网银属于演示知识。"])
        query = await client.embed(["企业网银是什么？"])
        return {
            "chat_backend": chat.backend,
            "chat_model": chat.model,
            "chat_has_text": bool(chat.text),
            "chat_tokens": chat.input_tokens + chat.output_tokens,
            "embedding_backend": documents.backend,
            "embedding_model": documents.model,
            "document_vectors": len(documents.vectors),
            "query_vectors": len(query.vectors),
            "dimensions": len(query.vectors[0]),
        }
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid-call", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.confirm_paid_call)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
