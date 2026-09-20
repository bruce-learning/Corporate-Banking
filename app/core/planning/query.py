from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


REFERENCES = ("这个", "那个", "上述", "刚才", "它", "该产品", "该业务", "那", "这些")
BANK_HINTS = ("网银", "账户", "贷款", "融资", "结算", "票据", "收款", "付款", "产品", "业务")


class QueryRewrite(BaseModel):
    original: str
    rewritten: str
    used_context: bool = False
    mode: Literal["unchanged", "context_concat"] = "unchanged"
    context_sequences: list[int] = Field(default_factory=list)


def rewrite_query(message: str, context: list[dict], maximum_messages: int = 6) -> QueryRewrite:
    """确定性改写，避免为了代词补全再产生一次模型调用和新幻觉。"""
    current = " ".join(message.split())
    if not current:
        raise ValueError("查询不能为空")
    explicit = any(word in current for word in BANK_HINTS)
    ambiguous = any(word in current for word in REFERENCES) or (len(current) <= 12 and not explicit)
    if not ambiguous:
        return QueryRewrite(original=current, rewritten=current)
    recent = context[-maximum_messages:]
    for reverse_index, row in enumerate(reversed(recent), 1):
        if row.get("role") != "user":
            continue
        previous = " ".join(str(row.get("content") or "").split())
        if previous and previous != current:
            return QueryRewrite(
                original=current, rewritten=f"{previous}；用户追问：{current}",
                used_context=True, mode="context_concat", context_sequences=[len(context) - reverse_index + 1],
            )
    return QueryRewrite(original=current, rewritten=current)
