"""Run one evaluation case against the retail graph.

A CP1 or CP2 case is a scripted conversation prefix plus one new customer
message. The prefix is replayed with its scripted extractions, so it always
reaches the same LangGraph state; only the case message goes to the
interpreter under test (the real model, or ``ExpectedInterpreter`` when the
case builder derives expected results). Nothing here reads policy text or
decides outcomes; it only drives the graph and collects its records.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any, Protocol

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.action_log.action_ledger import ActionLedger
from agent.action_log.policy_gate import PolicyGate
from agent.retail_graph.graph import build_graph, _turn


class Interpreter(Protocol):
    def extract(self, latest_message: str, state: dict) -> dict[str, Any]: ...


class _PrefixThenInterpreter:
    """Scripted extractions for the prefix turns, then the interpreter under test."""

    def __init__(self, prefix: list[dict[str, Any]], interpreter: Interpreter):
        self.prefix = prefix
        self.interpreter = interpreter

    def extract(self, latest_message: str, state: dict) -> dict[str, Any]:
        turn = _turn(state)
        if turn <= len(self.prefix):
            return self.prefix[turn - 1]["parsed"]
        return self.interpreter.extract(latest_message, state)


class ExpectedInterpreter:
    """Returns the case's expected extraction: what a correct reading gives."""

    def __init__(self, parsed: dict[str, Any]):
        self.parsed = parsed

    def extract(self, latest_message: str, state: dict) -> dict[str, Any]:
        return self.parsed


def coupon_lookup_for(verdict: dict[str, Any] | None):
    """Stand-in for check_coupon, fixed by the case. The Oracle route itself is
    covered by agent/oracle_demo and agent/retail_tools tests and the smoke run."""
    def lookup(customer_id: str, coupon_code: str) -> dict[str, Any]:
        if verdict is None:
            return {"status": "not_found", "customer_right": None}
        if verdict.get("coupon_code", coupon_code).upper() != str(coupon_code).upper():
            return {"status": "not_found", "customer_right": None}
        return {"status": "resolved", "customer_right": verdict["customer_right"],
                "reason_code": verdict.get("reason_code", "case_fixture")}
    return lookup


def _turn_view(state: dict[str, Any], turn: int) -> dict[str, list]:
    return {key: [e for e in state.get(key) or [] if e.get("turn") == turn]
            for key in ("extract_events", "cp1_events", "cp2_events")}


def run_case(case: dict[str, Any], interpreter: Interpreter) -> dict[str, Any]:
    """Replay the prefix, send the case message, and return that turn's records."""
    prefix = case["prefix"]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        ledger = ActionLedger(folder / "actions.sqlite")
        gate = PolicyGate(ledger)
        with SqliteSaver.from_conn_string(str(folder / "checkpoints.sqlite")) as saver:
            graph = build_graph(_PrefixThenInterpreter(prefix, interpreter), gate, saver,
                                coupon_lookup=coupon_lookup_for(case.get("coupon_verdict")))
            config = {"configurable": {"thread_id": case["id"]}}
            state: dict[str, Any] = {}
            transcript = []
            for turn in prefix:
                state = graph.invoke({"messages": [HumanMessage(content=turn["message"])]}, config)
                transcript.append({"customer": turn["message"], "agent": state["reply_text"]})
            stop = _stop_point(state)
            rows_before = len(ledger.list_conversation(case["id"]))
            state = graph.invoke({"messages": [HumanMessage(content=case["message"])]}, config)
            transcript.append({"customer": case["message"], "agent": state["reply_text"]})
            turn = len(prefix) + 1
            return {
                "case_id": case["id"],
                "stop_point": stop,
                "turn": turn,
                **_turn_view(state, turn),
                "ledger": ledger.list_conversation(case["id"])[rows_before:],
                "transcript": transcript,
                "requests": state.get("requests", []),
            }


def run_script(turns: list[dict[str, Any]], coupon_verdicts: list[dict] | None = None,
               thread_id: str = "script") -> dict[str, Any]:
    """Run a whole scripted conversation (used for full-conversation references)."""
    verdicts = {v["coupon_code"].upper(): v for v in coupon_verdicts or []}

    def lookup(customer_id: str, coupon_code: str) -> dict[str, Any]:
        return coupon_lookup_for(verdicts.get(str(coupon_code).upper()))(customer_id, coupon_code)

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        ledger = ActionLedger(folder / "actions.sqlite")
        with SqliteSaver.from_conn_string(str(folder / "checkpoints.sqlite")) as saver:
            graph = build_graph(_PrefixThenInterpreter(turns, ExpectedInterpreter({})),
                                PolicyGate(ledger), saver, coupon_lookup=lookup)
            config = {"configurable": {"thread_id": thread_id}}
            transcript = []
            for turn in turns:
                state = graph.invoke({"messages": [HumanMessage(content=turn["message"])]}, config)
                transcript.append({"customer": turn["message"], "agent": state["reply_text"]})
            return {"ledger": ledger.list_conversation(thread_id), "transcript": transcript,
                    "requests": state.get("requests", [])}


def _stop_point(state: dict[str, Any]) -> str:
    """What the agent was waiting for when the prefix ended."""
    if not state:
        return "start"
    events = state.get("cp1_events") or []
    last = events[-1] if events else {}
    return last.get("asked") or last.get("next_step") or "start"
