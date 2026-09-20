from __future__ import annotations

import re
from pydantic import BaseModel, Field

from app.core.rag.hybrid import Evidence
from app.infrastructure.llm.aliyun import ModelServiceError, PaidCallsDisabled
from app.infrastructure.llm.contracts import ChatInput, ModelMessage


DOMAIN_NAMES = {"product": "产品", "process": "办理流程", "policy": "政策与收费"}
DOMAIN_RULES = {
    "product": "只说明产品功能、适用场景、条件和明确边界，不承诺审批结果。",
    "process": "只说明材料、步骤、渠道和预计时效，不声称已代用户办理。",
    "policy": "只说明资料中的收费、限制、合规和安全要求；费率以有效资料及银行最终确认为准。",
}


class AgentDraft(BaseModel):
    domain: str
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)
    generation_mode: str
    model: str | None = None
    degraded: bool = False
    degradation_reason: str | None = None
    model_usage: list[dict] = Field(default_factory=list)
    skills_used: list[str] = Field(default_factory=list)


def extractive_answer(domain: str, evidence: list[Evidence]) -> str:
    if not evidence:
        return f"暂未在已发布且有效的{DOMAIN_NAMES[domain]}知识中找到依据，建议转人工核实。"
    lines = [f"根据当前有效的合成演示资料，{DOMAIN_NAMES[domain]}信息如下："]
    for item in evidence[:3]:
        content = " ".join(item.content.strip().split())
        if len(content) > 220:
            content = content[:220] + "…"
        lines.append(f"- {content} [资料:{item.id}]")
    return "\n".join(lines)


class BankDomainAgent:
    def __init__(self, domain: str, model_client=None, skill_manager=None):
        if domain not in DOMAIN_NAMES:
            raise ValueError("未知银行领域")
        self.domain = domain
        self.model_client = model_client
        self.skill_manager = skill_manager

    async def answer(self, question: str, context: list[dict], evidence: list[Evidence]) -> AgentDraft:
        ids = [item.id for item in evidence]
        skill = self.skill_manager.prompt_for(question, self.domain) if self.skill_manager else None
        skill_text = skill.text if skill else ""
        skill_names = skill.names if skill else []
        if not evidence:
            return AgentDraft(
                domain=self.domain, answer=extractive_answer(self.domain, evidence), evidence_ids=[],
                generation_mode="no_evidence", degraded=True, degradation_reason="no_evidence",
                skills_used=skill_names,
            )
        materials = "\n\n".join(
            f"[资料:{item.id}] 标题：{item.title}\n内容：{item.content}" for item in evidence[:4]
        )
        recent = "\n".join(f"{row['role']}：{row['content']}" for row in context[-6:]) or "无"
        try:
            system = (
                    f"你是银行对公智能客服的{DOMAIN_NAMES[self.domain]} Agent。{DOMAIN_RULES[self.domain]}"
                    "只能依据给定资料回答；每项事实必须在句末使用 [资料:ID] 引用；不得编造真实银行名称、"
                    "费率、审批结果或已执行交易。资料不足时明确说明并建议人工核实。"
                )
            if skill_text:
                system += "\n\n[动态业务 Skill]\n" + skill_text
            output = await self.model_client.chat(ChatInput(messages=[
                ModelMessage(role="system", content=system),
                ModelMessage(role="user", content=f"最近对话：\n{recent}\n\n本次问题：{question}\n\n有效资料：\n{materials}"),
            ], max_tokens=700))
            answer = output.text.strip()
            if not answer:
                raise ValueError("empty model answer")
            answer = re.sub(
                r"\[资料:([^\]]+)\]",
                lambda match: match.group(0) if match.group(1) in ids else "（无效引用已移除）",
                answer,
            )
            # 即使模型漏引，也必须把本次事实依据公开给前端。
            cited = [identifier for identifier in ids if f"[资料:{identifier}]" in answer]
            if not cited:
                answer += "\n\n资料来源：" + " ".join(f"[资料:{identifier}]" for identifier in ids[:3])
            return AgentDraft(
                domain=self.domain, answer=answer, evidence_ids=ids,
                generation_mode="llm_grounded", model=output.model,
                model_usage=[{"operation": f"agent_{self.domain}", "model": output.model,
                              "input_tokens": output.input_tokens, "output_tokens": output.output_tokens}],
                skills_used=skill_names,
            )
        except PaidCallsDisabled:
            reason = "generation_paid_calls_disabled"
        except ModelServiceError as error:
            reason = "generation_" + error.category
        except Exception:
            reason = "generation_unavailable"
        return AgentDraft(
            domain=self.domain, answer=extractive_answer(self.domain, evidence), evidence_ids=ids,
            generation_mode="evidence_extract", degraded=True, degradation_reason=reason,
            skills_used=skill_names,
        )
