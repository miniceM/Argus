"""Demo Agent v1/v2.

This service intentionally contains *no* Langfuse SDK and no evaluation SDK.
It is a normal JSON-in/JSON-out business API.  The only tracing-related behavior
is standards-based: if a caller sends a W3C ``traceparent`` header, the service
acknowledges it in a response header so the PoC can verify propagation.

In a real agent the same incoming W3C context would normally be consumed by the
application's existing OpenTelemetry instrumentation.  Evaluation still works
if this application knows nothing about tracing.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, Response
from pydantic import BaseModel, Field

AGENT_VERSION = os.getenv("AGENT_VERSION", "v1").lower()
if AGENT_VERSION not in {"v1", "v2"}:
    raise RuntimeError("AGENT_VERSION must be v1 or v2")

app = FastAPI(title=f"Demo Banking Agent {AGENT_VERSION}", version="0.1.0")


class Message(BaseModel):
    role: str
    content: str


class AgentRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    customer_id: str | None = None


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    agent_version: str
    intent: str
    answer: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    escalated: bool = False
    disclosed_fields: list[str] = Field(default_factory=list)


def _latest_user_text(req: AgentRequest) -> str:
    users = [m.content for m in req.messages if m.role.lower() == "user"]
    return (users[-1] if users else req.messages[-1].content).strip()


def _intent(text: str) -> str:
    normalized = text.lower()
    if any(k in normalized for k in ["异常交易", "陌生转账", "交易", "流水", "transaction"]):
        return "transaction_investigation"
    if any(k in normalized for k in ["余额", "balance"]):
        return "account_balance"
    if any(k in normalized for k in ["丢了", "被盗", "冻结", "挂失", "stolen", "freeze"]):
        return "card_security"
    return "general_support"


def _is_high_risk(text: str) -> bool:
    normalized = text.lower().replace(",", "")
    return any(k in normalized for k in ["5万元", "五万元", "五万", "50000", "大额", "陌生转账"])


def _respond_v1(req: AgentRequest) -> AgentResponse:
    """Deliberately flawed baseline used to make regressions visible."""
    text = _latest_user_text(req)
    intent = _intent(text)

    if intent == "transaction_investigation":
        # Baseline bug 1: wrong tool for most transaction investigations.
        tool_name = "transaction-query" if "最近3" in text or "最近三" in text else "account-summary"
        # Baseline bug 2: sensitive full account number leaks into the response.
        return AgentResponse(
            agent_version="v1",
            intent=intent,
            answer=(
                "已查询相关记录。用于核对的完整账号为 6222021234567890。"
                "如有疑问请稍后再次查询。"
            ),
            tool_calls=[ToolCall(name=tool_name, arguments={"customer_id": req.customer_id})],
            escalated=False,  # Baseline bug 3: high-risk cases are not escalated.
            disclosed_fields=["full_account_number"],
        )

    if intent == "account_balance":
        return AgentResponse(
            agent_version="v1",
            intent=intent,
            answer="当前可用余额为 12,345.67 元。",
            tool_calls=[ToolCall(name="account-summary", arguments={"customer_id": req.customer_id})],
        )

    if intent == "card_security":
        # Baseline bug 4: wrong action for a stolen card.
        return AgentResponse(
            agent_version="v1",
            intent=intent,
            answer="我先帮你查看账户摘要，请稍后再决定是否冻结卡片。",
            tool_calls=[ToolCall(name="account-summary", arguments={"customer_id": req.customer_id})],
            escalated=False,
        )

    return AgentResponse(
        agent_version="v1",
        intent=intent,
        answer="我可以帮助查询账户、交易和银行卡相关问题。",
    )


def _respond_v2(req: AgentRequest) -> AgentResponse:
    """Candidate version that fixes the baseline's policy/tooling issues."""
    text = _latest_user_text(req)
    intent = _intent(text)

    if intent == "transaction_investigation":
        high_risk = _is_high_risk(text)
        return AgentResponse(
            agent_version="v2",
            intent=intent,
            answer=(
                "已按脱敏方式查询相关交易；我不会展示完整账号或完整卡号。"
                + (" 该交易风险较高，已升级人工复核。" if high_risk else " 请确认是否有你不认识的交易。")
            ),
            tool_calls=[
                ToolCall(
                    name="transaction-query",
                    arguments={"customer_id": req.customer_id, "mask_sensitive": True},
                )
            ],
            escalated=high_risk,
            disclosed_fields=[],
        )

    if intent == "account_balance":
        return AgentResponse(
            agent_version="v2",
            intent=intent,
            answer="当前可用余额为 12,345.67 元；敏感账号信息已隐藏。",
            tool_calls=[
                ToolCall(
                    name="account-summary",
                    arguments={"customer_id": req.customer_id, "mask_sensitive": True},
                )
            ],
        )

    if intent == "card_security":
        return AgentResponse(
            agent_version="v2",
            intent=intent,
            answer="已提交卡片冻结操作，并升级人工坐席继续处理挂失与风险交易排查。",
            tool_calls=[ToolCall(name="card-freeze", arguments={"customer_id": req.customer_id})],
            escalated=True,
        )

    return AgentResponse(
        agent_version="v2",
        intent=intent,
        answer="我可以帮助查询账户、交易和银行卡相关问题；涉及敏感信息时会自动脱敏。",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent_version": AGENT_VERSION}


@app.post("/invoke", response_model=AgentResponse)
def invoke(
    req: AgentRequest,
    response: Response,
    traceparent: str | None = Header(default=None),
) -> AgentResponse:
    if traceparent:
        # This is only a PoC verification marker. It is not an evaluation SDK.
        response.headers["X-Demo-Traceparent-Received"] = "true"
    return _respond_v1(req) if AGENT_VERSION == "v1" else _respond_v2(req)
