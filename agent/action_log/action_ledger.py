"""A small, append-only ledger for simulated retail-agent actions.

This module does not perform refunds, modify orders, issue coupons, or decide
policy. The policy gate supplies an Authorization; the ledger verifies
that the proposal matches it and records the simulated or blocked outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4


ACTION_TYPES = frozenset({
    "initiate_return",
    "refund_item",
    "cancel_item_and_refund",
    "reschedule_delivery",
    "credit_shipping_fee",
})
RETURN_METHODS = frozenset({"by_mail", "in_store", "drop_off_center"})
DELIVERY_WINDOWS = frozenset({"tomorrow", "next_week"})


@dataclass(frozen=True)
class ActionProposal:
    conversation_id: str
    request_id: str  # Stable idempotency key for one proposed action.
    intent: str
    action_type: str
    customer_id: str
    order_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Authorization:
    """Trusted policy-gate output, never supplied by the LLM directly."""

    allowed_action: str | None
    verified_customer_id: str | None
    verified_order_id: str | None
    policy_rule_id: str | None
    policy_version: str
    approved_payload: dict[str, Any] | None = None
    denial_reason: str | None = None


def _required_text(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field_name: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ValueError(f"{field_name} must be a {'nonnegative' if allow_zero else 'positive'} integer")
    return value


def _validate_payload(action_type: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if action_type in {"refund_item", "cancel_item_and_refund"}:
        expected = {"item_index", "amount_cents"}
        if set(payload) != expected:
            raise ValueError(f"{action_type} requires item_index and amount_cents")
        _positive_int(payload["item_index"], "item_index", allow_zero=True)
        _positive_int(payload["amount_cents"], "amount_cents")
    elif action_type == "initiate_return":
        expected = {"item_index", "return_method", "address_confirmed"}
        if set(payload) != expected or payload["return_method"] not in RETURN_METHODS or payload["address_confirmed"] is not True:
            raise ValueError("initiate_return requires item_index, valid return_method, and confirmed address")
        _positive_int(payload["item_index"], "item_index", allow_zero=True)
    elif action_type == "reschedule_delivery":
        if set(payload) not in ({"delivery_window"}, {"delivery_window", "charge_amount_cents"}):
            raise ValueError("reschedule_delivery requires delivery_window and optional charge_amount_cents")
        if payload["delivery_window"] not in DELIVERY_WINDOWS:
            raise ValueError("delivery_window must be tomorrow or next_week")
        if "charge_amount_cents" in payload:
            _positive_int(payload["charge_amount_cents"], "charge_amount_cents")
    elif action_type == "credit_shipping_fee":
        if set(payload) != {"amount_cents"}:
            raise ValueError("credit_shipping_fee requires amount_cents")
        _positive_int(payload["amount_cents"], "amount_cents")


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ActionLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS action_events (
                    event_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    customer_id TEXT,
                    order_id TEXT,
                    payload_json TEXT NOT NULL,
                    policy_rule_id TEXT,
                    policy_version TEXT,
                    outcome TEXT NOT NULL CHECK(outcome IN ('simulated','blocked','handoff')),
                    reason_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS action_events_by_conversation
                    ON action_events(conversation_id, created_at);
            """)

    def _insert(self, record: dict[str, Any]) -> dict[str, Any]:
        request_hash = _canonical_hash(record)
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM action_events WHERE request_id=?", (record["request_id"],)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ValueError("request_id was reused with different action details")
                return self._public_result(previous)
            row = {
                "event_id": str(uuid4()),
                "request_id": record["request_id"],
                "request_hash": request_hash,
                "conversation_id": record["conversation_id"],
                "intent": record["intent"],
                "action_type": record["action_type"],
                "customer_id": record["customer_id"],
                "order_id": record["order_id"],
                "payload_json": json.dumps(record["payload"], sort_keys=True, ensure_ascii=False),
                "policy_rule_id": record["policy_rule_id"],
                "policy_version": record["policy_version"],
                "outcome": record["outcome"],
                "reason_code": record["reason_code"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute(
                "INSERT INTO action_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row.values()),
            )
            return self._public_result(row)

    @staticmethod
    def _public_result(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = {key: row[key] for key in (
            "event_id", "request_id", "conversation_id", "intent", "action_type",
            "customer_id", "order_id", "policy_rule_id", "policy_version",
            "outcome", "reason_code", "created_at",
        )}
        result["payload"] = json.loads(row["payload_json"])
        return result

    def record_action(self, proposal: ActionProposal, authorization: Authorization) -> dict[str, Any]:
        if proposal.action_type not in ACTION_TYPES:
            raise ValueError("unsupported action_type")
        for key in ("conversation_id", "request_id", "intent", "customer_id", "order_id"):
            _required_text(getattr(proposal, key), key)
        _required_text(authorization.policy_version, "policy_version")
        _validate_payload(proposal.action_type, proposal.payload)
        authorized = (
            proposal.action_type == authorization.allowed_action
            and proposal.customer_id == authorization.verified_customer_id
            and proposal.order_id == authorization.verified_order_id
            and proposal.payload == authorization.approved_payload
            and isinstance(authorization.policy_rule_id, str)
            and bool(authorization.policy_rule_id.strip())
        )
        return self._insert({
            "request_id": proposal.request_id,
            "conversation_id": proposal.conversation_id,
            "intent": proposal.intent,
            "action_type": proposal.action_type,
            "customer_id": proposal.customer_id,
            "order_id": proposal.order_id,
            "payload": proposal.payload,
            "policy_rule_id": authorization.policy_rule_id,
            "policy_version": authorization.policy_version,
            "outcome": "simulated" if authorized else "blocked",
            "reason_code": None if authorized else (authorization.denial_reason or "policy_gate_mismatch"),
        })

    def record_handoff(self, *, conversation_id: str, request_id: str, intent: str,
                       reason_code: str, customer_id: str | None = None,
                       order_id: str | None = None) -> dict[str, Any]:
        for key, value in (("conversation_id", conversation_id), ("request_id", request_id),
                           ("intent", intent), ("reason_code", reason_code)):
            _required_text(value, key)
        return self._insert({
            "request_id": request_id,
            "conversation_id": conversation_id,
            "intent": intent,
            "action_type": "handoff",
            "customer_id": customer_id,
            "order_id": order_id,
            "payload": {},
            "policy_rule_id": None,
            "policy_version": None,
            "outcome": "handoff",
            "reason_code": reason_code,
        })

    def list_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        _required_text(conversation_id, "conversation_id")
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM action_events WHERE conversation_id=? ORDER BY created_at,event_id",
                (conversation_id,),
            ).fetchall()
        return [self._public_result(row) for row in rows]
