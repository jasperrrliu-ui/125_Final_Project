"""Optional real LLM adapter for the retail LangGraph.

No model call happens on import. Set OPENAI_API_KEY to run this adapter.
The default model is gpt-5.4-mini; RETAIL_AGENT_MODEL can override it.

The model has one job: read the customer's latest message and fill a fixed
schema (identity details, requests, request details, answers to the
agent's questions, option choice, yes/no). It does not see or apply policy.
Eligibility, options, amounts, and authorization are computed in Python by
the policy engine and the policy gate.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError

from .graph import ACTIVE_STATUSES, ChatState


Intent = Literal[
    "return_size", "return_color", "return_stain",
    "promo_code_invalid", "promo_code_out_of_date",
    "manage_cancel", "manage_upgrade", "manage_downgrade",
]


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str | None = None
    full_name: str | None = None
    zip_code: str | None = None
    account_id: str | None = None


class RequestExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    order_id: str | None = None
    item_name: str | None = None
    coupon_code: str | None = None


class ActiveFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = None
    item_name: str | None = None
    coupon_code: str | None = None


class CustomerFacts(BaseModel):
    """Answers to the agent's policy questions, exactly as the customer stated them."""

    model_config = ConfigDict(extra="forbid")
    has_receipt: bool | None = None
    purchase_date: str | None = Field(default=None, description="ISO date YYYY-MM-DD")
    original_packaging: bool | None = None


class TurnExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: Identity = Field(default_factory=Identity)
    requests: list[RequestExtraction] = Field(default_factory=list)
    fields: ActiveFields = Field(default_factory=ActiveFields)
    customer_facts: CustomerFacts = Field(default_factory=CustomerFacts)
    selected_option: str | None = None
    confirmation: Literal["yes", "no"] | None = None
    withdraw_active_request: bool = False
    out_of_scope: str | None = None


SYSTEM_PROMPT = (
    "You read one message from a retail customer and fill the schema. You never "
    "decide eligibility or actions.\n"
    "- requests: only needs newly expressed in this message. Valid intents: "
    "return_size, return_color, return_stain (return an item for wrong size, wrong "
    "color, or a stain); promo_code_invalid (code issued within the last 7 days does "
    "not work); promo_code_out_of_date (code issued more than 7 days ago does not "
    "work); manage_cancel (remove an unwanted extra item from an order); "
    "manage_upgrade (faster delivery, e.g. tomorrow); manage_downgrade (slower "
    "delivery, e.g. next week). Read the desired outcome, not keywords: moving "
    "delivery later is never an upgrade.\n"
    "- out_of_scope: a short description if the customer asks for something none "
    "of these intents covers; otherwise null.\n"
    "- identity: email, full name, zip code, or account ID only if the customer "
    "stated them.\n"
    "- fields: order ID, item name, or coupon code given for the open request.\n"
    "- customer_facts: answers to the agent's last questions (receipt, purchase "
    "date as YYYY-MM-DD, original packaging). Only what the customer stated.\n"
    "- selected_option: if the agent offered options, the option_id the customer "
    "picked (match by meaning, e.g. 'by mail' -> return_by_mail); otherwise null.\n"
    "- confirmation: 'yes' if the customer clearly agrees to go ahead, 'no' if they "
    "clearly refuse; otherwise null.\n"
    "- withdraw_active_request: true only if the customer drops the open request.\n"
    "Never invent an order ID, coupon code, email, name, zip code, date, or answer. "
    "Leave anything not stated as null."
)


class OpenAIInterpreter:
    def __init__(self, model_name: str | None = None):
        from langchain_openai import ChatOpenAI

        model_name = model_name or os.getenv("RETAIL_AGENT_MODEL") or "gpt-5.4-mini"
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("Set OPENAI_API_KEY before using the real LLM adapter")
        self.model = ChatOpenAI(model=model_name)
        self.extractor = self.model.with_structured_output(
            TurnExtraction, method="function_calling", strict=False
        )

    @staticmethod
    def _invoke_structured(runnable: Any, messages: list[tuple[str, str]]) -> BaseModel:
        try:
            return runnable.invoke(messages)
        except (ValidationError, OutputParserException):
            retry_messages = [
                ("system", messages[0][1] + " Your prior output did not match the schema. "
                 "Return valid structured fields only; use null for anything not stated."),
                *messages[1:],
            ]
            return runnable.invoke(retry_messages)

    @staticmethod
    def conversation_context(state: ChatState) -> dict[str, Any]:
        """What the model needs to interpret a short reply such as 'yes' or 'by mail'."""
        open_requests = []
        for request in state.get("requests", []):
            if request.get("status") not in ACTIVE_STATUSES:
                continue
            open_requests.append({
                "id": request["id"],
                "intent": request["intent"],
                "status": request["status"],
                "order_id": request.get("order_id"),
                "item_name": request.get("item_name"),
                "coupon_code": request.get("coupon_code"),
                "asked_facts": request.get("asked_facts"),
                "offered_options": [
                    {"option_id": o["option_id"], "summary": o["summary"]}
                    for o in request.get("offered_options") or []
                ],
            })
        previous_agent_message = next(
            (m.content for m in reversed(state.get("messages", []))
             if getattr(m, "type", None) == "ai"), None)
        return {
            "customer_verified": bool(state.get("customer_id")),
            "open_requests": open_requests,
            "previous_agent_message": previous_agent_message,
        }

    def extract(self, latest_message: str, state: ChatState) -> dict[str, Any]:
        context = self.conversation_context(state)
        answer = self._invoke_structured(self.extractor, [
            ("system", SYSTEM_PROMPT),
            ("user", f"Conversation context: {json.dumps(context, ensure_ascii=False)}\n"
                     f"Latest customer message: {latest_message}"),
        ])
        result = answer.model_dump(exclude_none=True)
        # Drop empty sub-objects and default flags so the graph sees only signals.
        for key in ("identity", "fields", "customer_facts"):
            if not result.get(key):
                result.pop(key, None)
        if not result.get("withdraw_active_request"):
            result.pop("withdraw_active_request", None)
        return result
