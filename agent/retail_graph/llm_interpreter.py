"""Optional real LLM adapter for the six-node retail LangGraph.

No model call happens on import. Set OPENAI_API_KEY to run this adapter.
The default model is gpt-5.4-mini; RETAIL_AGENT_MODEL can override it.
Graph routing and policy authorization stay in Python.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError

from .graph import ChatState, INTENTS


Intent = Literal[
    "return_size", "return_color", "return_stain",
    "promo_code_invalid", "promo_code_out_of_date",
    "manage_cancel", "manage_upgrade", "manage_downgrade",
]
IdentifierType = Literal["account_id", "full_name", "username", "email"]
ActionType = Literal[
    "initiate_return", "refund_item", "cancel_item_and_refund",
    "reschedule_delivery", "credit_shipping_fee", "issue_replacement_coupon",
]


class AccountIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: IdentifierType
    value: str


class RequestExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    order_id: str | None = None
    item_index: int | None = None
    item_name: str | None = None
    coupon_code: str | None = None
    problem_detail: str | None = None


class ActiveFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = None
    item_index: int | None = None
    item_name: str | None = None
    coupon_code: str | None = None
    problem_detail: str | None = None


class TurnExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_identifier: AccountIdentifier | None = None
    requests: list[RequestExtraction] = Field(default_factory=list)
    fields: ActiveFields = Field(default_factory=ActiveFields)


class ActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_index: int | None = None
    amount_cents: int | None = None
    delivery_window: Literal["tomorrow", "next_week"] | None = None
    charge_amount_cents: int | None = None
    return_method: Literal["by_mail", "in_store", "drop_off_center"] | None = None
    address_confirmed: bool | None = None


class ResolutionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_type: ActionType | None = None
    payload: ActionPayload = Field(default_factory=lambda: ActionPayload())
    policy_rule_id: str | None = None
    reason: str


class OpenAIInterpreter:
    def __init__(self, model_name: str | None = None,
                 policy_file: Path | None = None):
        from langchain_openai import ChatOpenAI

        model_name = model_name or os.getenv("RETAIL_AGENT_MODEL") or "gpt-5.4-mini"
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("Set OPENAI_API_KEY before using the real LLM adapter")
        self.model = ChatOpenAI(model=model_name)
        self.extractor = self.model.with_structured_output(
            TurnExtraction, method="function_calling", strict=False
        )
        self.proposer = self.model.with_structured_output(
            ResolutionProposal, method="function_calling", strict=False
        )
        policy_path = policy_file or (
            Path(__file__).resolve().parents[2] / "processed_data" / "policy" / "policy_rules.json"
        )
        self.rules = json.loads(Path(policy_path).read_text(encoding="utf-8"))["rules"]

    @staticmethod
    def _invoke_structured(runnable: Any, messages: list[tuple[str, str]]) -> BaseModel:
        try:
            return runnable.invoke(messages)
        except (ValidationError, OutputParserException):
            retry_messages = [
                ("system", messages[0][1] + " Your prior output did not match the schema. "
                 "Return valid structured fields only; use null for unsupported actions "
                 "or missing facts."),
                *messages[1:],
            ]
            return runnable.invoke(retry_messages)

    def extract(self, latest_message: str, state: ChatState) -> dict[str, Any]:
        open_requests = [
            {key: request.get(key) for key in ("id", "intent", "order_id", "item_index", "item_name", "coupon_code", "problem_detail")}
            for request in state.get("requests", [])
            if request.get("status") in {"open", "waiting"}
        ]
        answer = self._invoke_structured(self.extractor, [
            ("system",
             "You extract information from a retail customer chat. Valid intents: "
             + ", ".join(sorted(INTENTS))
             + ". Read the customer's desired outcome, not isolated keywords: "
               "wanting to cancel an extra item is manage_cancel even if they mention its size. "
               "For delivery changes, manage_upgrade means earlier or faster delivery "
               "(for example, tomorrow); manage_downgrade means later or slower delivery "
               "(for example, next week). Moving delivery to next week is never an upgrade. "
               "Use requests only for newly expressed needs; use fields for details "
               "supplied about the current open request. Return only identifiers and "
               "values explicitly stated by the customer. Never invent an order ID, "
               "coupon code, account ID, item index, or verified fact. "
               "If the customer names an item, extract item_name as spoken; "
               "do not invent a database item index. "
               "For a return, capture the concrete size/color mismatch or stain "
               "description as problem_detail if the customer stated it. "
               "If a field is absent, leave it null."),
            ("user", f"Open requests: {json.dumps(open_requests)}\n"
                     f"Latest customer message: {latest_message}"),
        ])
        return answer.model_dump(exclude_none=True)

    def propose(self, request: dict[str, Any], state: ChatState) -> dict[str, Any]:
        rules = [
            {"id": rule["id"], "when": rule["when"], "decision": rule["decision"]}
            for rule in self.rules if request["intent"] in rule["applies_to"]
        ]
        facts = request.get("order_result") or request.get("coupon_result") or {}
        answer = self._invoke_structured(self.proposer, [
            ("system",
             "Select one action using only the verified facts and supplied policy rules. "
             "You do not execute the action; the backend independently checks the exact "
             "action and payload. Never invent a missing fact. "
             "Map policy decisions to these action types: remove an item and refund -> "
             "cancel_item_and_refund; refund a shipped member's unwanted item -> "
             "refund_item; update delivery to tomorrow or next week -> "
             "reschedule_delivery. For cancel/refund, payload contains only item_index "
             "and amount_cents copied from the selected verified item. For delivery "
             "changes, payload contains only delivery_window ('tomorrow' or 'next_week'); "
             "a non-Gold upgrade also needs verified charge consent and charge_amount_cents. "
             "Never put order_id, item_name, member_level, or shipping_status in payload. "
             "For a return, do not propose initiate_return until the return address and "
             "method have been confirmed by the backend; missing confirmation means "
             "action_type=null. For coupon requests, replacement issuance is unavailable, "
             "so action_type=null. If any other required fact is missing or the policy "
             "does not cover the case, set action_type=null. Always provide a short reason. "
             "Do not claim a real refund, order change, payment, or coupon issuance."),
            ("user", json.dumps({
                "intent": request["intent"],
                "order_id": request.get("order_id"),
                "item_index": request.get("item_index"),
                "item_name": request.get("item_name"),
                "coupon_code_present": bool(request.get("coupon_code")),
                "problem_detail": request.get("problem_detail"),
                "member_level": state.get("member_level"),
                "verified_facts": facts,
                "policy_rules": rules,
            }, ensure_ascii=False)),
        ])
        proposal = answer.model_dump(exclude_none=True)
        payload = proposal.get("payload", {})
        allowed_fields = {
            "cancel_item_and_refund": {"item_index", "amount_cents"},
            "refund_item": {"item_index", "amount_cents"},
            "reschedule_delivery": {"delivery_window", "charge_amount_cents"},
            "initiate_return": {"item_index", "return_method", "address_confirmed"},
            "credit_shipping_fee": {"amount_cents"},
        }.get(proposal.get("action_type"), set())
        proposal["payload"] = {key: value for key, value in payload.items()
                               if key in allowed_fields}
        return proposal
