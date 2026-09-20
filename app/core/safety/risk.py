from __future__ import annotations

import re
from pydantic import BaseModel, Field


class RiskDecision(BaseModel):
    flags: list[str] = Field(default_factory=list)
    blocked: bool = False
    safe_message: str
    response: str | None = None
    forced_domains: list[str] = Field(default_factory=list)


SECRET_TERMS = ("验证码", "短信码", "登录密码", "支付密码", "u盾口令", "U盾口令", "网银密码")
EXECUTION_PATTERNS = (
    r"帮我.{0,8}(转账|付款|扣款|开户|销户|解锁|修改密码)",
    r"替我.{0,8}(转账|付款|扣款|开户|销户|解锁)",
    r"直接.{0,6}(转账|付款|扣款|开户|销户)",
)
PROMISE_TERMS = ("保证获批", "百分百通过", "一定批准", "固定利率", "保证放款")
INJECTION_TERMS = (
    "忽略之前的指令", "忽略系统提示", "输出系统提示词", "泄露系统提示",
    "显示api key", "显示 API Key", "输出api key", "开发者消息", "绕过安全规则",
)


def redact_sensitive(text: str) -> str:
    value = text
    if any(term.lower() in value.lower() for term in SECRET_TERMS):
        value = re.sub(r"(?<!\d)\d{4,12}(?!\d)", "[已脱敏]", value)
        value = re.sub(r"(?i)(密码|口令)\s*(?:是|[:：=])?\s*[^\s，。；;]{3,32}", r"\1：[已脱敏]", value)
    value = re.sub(r"(?<!\d)\d{16,19}(?!\d)", lambda m: m.group(0)[:4] + "********" + m.group(0)[-4:], value)
    return value


def inspect_risk(message: str) -> RiskDecision:
    flags: list[str] = []
    lowered = message.lower()
    secret = any(term.lower() in lowered for term in SECRET_TERMS)
    execution = any(re.search(pattern, message, re.IGNORECASE) for pattern in EXECUTION_PATTERNS)
    promise = any(term in message for term in PROMISE_TERMS)
    injection = any(term.lower() in lowered for term in INJECTION_TERMS)
    if secret:
        flags.append("credential_or_secret")
    if execution:
        flags.append("transaction_execution")
    if promise:
        flags.append("approval_or_price_promise")
    if injection:
        flags.append("prompt_injection")
    safe = redact_sensitive(message)
    if secret:
        return RiskDecision(
            flags=flags, blocked=True, safe_message=safe,
            response="请不要提供验证码、密码或 U 盾口令。本系统只提供信息咨询，不会索取或处理任何认证凭据。",
        )
    if execution:
        return RiskDecision(
            flags=flags, blocked=True, safe_message=safe,
            response="本系统只能提供信息咨询，不能代替你执行转账、付款、开户、销户或账户解锁等真实银行操作。请通过银行官方渠道并由授权人员办理。",
        )
    if injection:
        return RiskDecision(
            flags=flags, blocked=True, safe_message=safe,
            response="该请求试图覆盖系统规则或获取受保护配置，已被安全策略拒绝。我只能基于已发布的银行演示资料提供只读咨询。",
        )
    return RiskDecision(
        flags=flags, blocked=False, safe_message=safe,
        forced_domains=["policy"] if promise else [],
    )
