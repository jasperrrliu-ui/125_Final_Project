"""Eight-intent end-to-end smoke runner for the retail agent.

Preflight checks real local business and coupon tools without a model call.
--live also sends one synthetic customer message per intent to the configured
LLM and records CP1, CP2, and simulated action outcomes. These fixtures are
smoke cases, not reviewed ABCD gold labels or an accuracy benchmark.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import ValidationError

from agent.action_log.action_ledger import ActionLedger
from agent.action_log.policy_gate import PolicyGate
from agent.retail_graph.graph import build_graph
from agent.retail_tools.check_coupon import check_coupon
from agent.retail_tools.read_tools import lookup_customer, lookup_order


ROOT = Path(__file__).resolve().parents[2]
from agent.oracle_demo.core import DEMO_TIME
from agent.oracle_demo.stdlib_api import make_server


CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "return_size", "account_id": "ACCT-000001",
        "order_id": "5199420931", "coupon_code": None,
        "expected_outcome": ["blocked", "handoff"],
        "message": "I want to return the Calvin Klein jacket in order 5199420931. "
                   "It arrived in size 8, but I ordered size 7. My account ID is ACCT-000001.",
    },
    {
        "id": "return_color", "account_id": "ACCT-000001",
        "order_id": "5199420931", "coupon_code": None,
        "expected_outcome": ["blocked", "handoff"],
        "message": "I want to return the Calvin Klein jacket in order 5199420931. "
                   "It arrived blue, but I ordered black. My account ID is ACCT-000001.",
    },
    {
        "id": "return_stain", "account_id": "ACCT-000001",
        "order_id": "5199420931", "coupon_code": None,
        "expected_outcome": ["blocked", "handoff"],
        "message": "I want to return the Calvin Klein jacket in order 5199420931. "
                   "It has a dark stain on the front. My account ID is ACCT-000001.",
    },
    {
        "id": "promo_code_invalid", "account_id": "ACCT-000001",
        "order_id": None, "coupon_code": "FAKE20",
        "expected_outcome": "handoff",
        "expected_coupon_reason": "unknown_code",
        "message": "The coupon FAKE20 was rejected as an invalid code at checkout. "
                   "My account ID is ACCT-000001. Please help.",
    },
    {
        "id": "promo_code_out_of_date", "account_id": "ACCT-000001",
        "order_id": None, "coupon_code": "OLD20",
        "expected_outcome": "handoff",
        "expected_coupon_reason": "expired",
        "message": "The coupon OLD20 was rejected because it expired. "
                   "My account ID is ACCT-000001. Please help.",
    },
    {
        "id": "manage_cancel", "account_id": "ACCT-000300",
        "order_id": "0066041981", "coupon_code": None,
        "expected_outcome": "simulated",
        "message": "Please cancel the Tommy Hilfiger jacket in order 0066041981. "
                   "My account ID is ACCT-000300.",
    },
    {
        "id": "manage_upgrade", "account_id": "ACCT-000007",
        "order_id": "7027853016", "coupon_code": None,
        "expected_outcome": "simulated",
        "message": "Please upgrade delivery to tomorrow for order 7027853016. "
                   "My account ID is ACCT-000007.",
    },
    {
        "id": "manage_downgrade", "account_id": "ACCT-000300",
        "order_id": "0066041981", "coupon_code": None,
        "expected_outcome": "simulated",
        "message": "Please move delivery to next week for order 0066041981. "
                   "My account ID is ACCT-000300.",
    },
)


class ScriptedInterpreter:
    """Deterministic graph exercise; it does not measure LLM quality."""

    def extract(self, latest_message: str, state: dict) -> dict:
        case = next(case for case in CASES if case["message"] == latest_message)
        detail = {
            "return_size": "received size 8 instead of ordered size 7",
            "return_color": "received blue instead of ordered black",
            "return_stain": "dark stain on the front",
        }.get(case["id"])
        return {
            "account_identifier": {"type": "account_id", "value": case["account_id"]},
            "requests": [{
                "intent": case["id"], "order_id": case["order_id"],
                "coupon_code": case["coupon_code"], "problem_detail": detail,
            }],
        }

    def propose(self, request: dict, state: dict) -> dict:
        intent = request["intent"]
        if intent.startswith("return_"):
            return {"action_type": "initiate_return",
                    "payload": {"item_index": request["item_index"],
                                "return_method": "by_mail", "address_confirmed": True}}
        if intent.startswith("promo_"):
            return {"action_type": "issue_replacement_coupon", "payload": {}}
        if intent == "manage_cancel":
            return {"action_type": "cancel_item_and_refund",
                    "payload": {"item_index": request["item_index"], "amount_cents": 8400}}
        if intent == "manage_upgrade":
            return {"action_type": "reschedule_delivery",
                    "payload": {"delivery_window": "tomorrow"}}
        return {"action_type": "reschedule_delivery",
                "payload": {"delivery_window": "next_week"}}


def prepare_oracle(folder: Path):
    """Start the real local HTTP adapter with disposable coupon databases."""
    server = make_server(port=0, data_dir=folder, secret=b"retail-smoke-local-key")
    platform_db = folder / "platform_oracle.sqlite"
    with closing(sqlite3.connect(platform_db)) as db, db:
        db.execute(
            "INSERT INTO checkout_attempts VALUES (?,?,?,?,?,?,?)",
            ("ATT-INVALID-1", "M-101", "C0001", "FAKE20", 7500,
             "rejected", "COUPON_REJECTED"),
        )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, platform_db


def preflight_case(case: dict[str, Any], *, platform_db: Path,
                   oracle_url: str) -> dict[str, Any]:
    customer = lookup_customer("account_id", case["account_id"])
    result: dict[str, Any] = {
        "id": case["id"],
        "customer_lookup": customer["status"],
    }
    if customer["status"] != "found":
        result["passed"] = False
        return result
    if case["order_id"]:
        order = lookup_order(customer["customer_id"], case["order_id"])
        result["order_lookup"] = order["status"]
        result["passed"] = order["status"] == "found"
    else:
        coupon = check_coupon(
            customer["customer_id"], case["coupon_code"],
            platform_db=platform_db, oracle_url=oracle_url, as_of=DEMO_TIME,
        )
        result["coupon_lookup"] = coupon["status"]
        result["coupon_reason"] = coupon.get("reason_code")
        result["passed"] = (
            coupon["status"] == "resolved"
            and coupon.get("reason_code") == case["expected_coupon_reason"]
        )
    return result


def live_case(case: dict[str, Any], graph: Any, ledger: ActionLedger) -> dict[str, Any]:
    thread_id = f"smoke-{case['id']}"
    result = graph.invoke(
        {"messages": [HumanMessage(content=case["message"])]},
        {"configurable": {"thread_id": thread_id}},
    )
    cp1 = result.get("cp1_events", [])
    cp2 = result.get("cp2_events", [])
    planned_tools = [event["planned_tool"] for event in cp1 if event.get("planned_tool")]
    expected_tools = ["lookup_customer", "check_coupon" if case["coupon_code"] else "lookup_order"]
    expected_outcomes = case["expected_outcome"]
    if isinstance(expected_outcomes, str):
        expected_outcomes = [expected_outcomes]
    requests = result.get("requests", [])
    checks = {
        "recognized_intent": len(requests) == 1 and requests[0]["intent"] == case["id"],
        "query_plan": planned_tools == expected_tools,
        "cp2_present": len(cp2) == 1 and "gate_outcome" in cp2[0],
        "expected_outcome": len(cp2) == 1 and cp2[0].get("gate_outcome") in expected_outcomes,
    }
    return {
        "id": case["id"],
        "checks": checks,
        "passed": all(checks.values()),
        "request_status": requests[0]["status"] if len(requests) == 1 else None,
        "reply": result.get("reply_text"),
        "cp1_events": cp1,
        "cp2_events": cp2,
        "action_events": ledger.list_conversation(thread_id),
    }


def run(*, live: bool = False, scripted: bool = False,
        case_id: str | None = None) -> dict[str, Any]:
    if live and scripted:
        raise ValueError("Choose either live or scripted mode")
    if live and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Live mode requires OPENAI_API_KEY")
    selected_cases = [case for case in CASES if case_id is None or case["id"] == case_id]
    if not selected_cases:
        raise ValueError(f"Unknown case ID: {case_id}")
    with tempfile.TemporaryDirectory(prefix="retail-agent-smoke-") as temp:
        folder = Path(temp)
        server, server_thread, platform_db = prepare_oracle(folder)
        oracle_url = f"http://127.0.0.1:{server.server_port}"
        try:
            preflight = [preflight_case(case, platform_db=platform_db, oracle_url=oracle_url)
                         for case in selected_cases]
            report: dict[str, Any] = {
                "mode": "live_llm" if live else "scripted_graph" if scripted else "local_preflight",
                "case_count": len(selected_cases),
                "preflight": preflight,
                "preflight_passed": all(item["passed"] for item in preflight),
            }
            if not (live or scripted) or not report["preflight_passed"]:
                return report

            if live:
                from agent.retail_graph.llm_interpreter import OpenAIInterpreter
                interpreter = OpenAIInterpreter()
            else:
                interpreter = ScriptedInterpreter()
            ledger = ActionLedger(folder / "action_events.sqlite")
            gate = PolicyGate(ledger)
            with SqliteSaver.from_conn_string(str(folder / "chat_state.sqlite")) as saver:
                graph = build_graph(
                    interpreter, gate, saver,
                    coupon_lookup=lambda customer_id, code: check_coupon(
                        customer_id, code, platform_db=platform_db,
                        oracle_url=oracle_url, as_of=DEMO_TIME,
                    ),
                )
                cases = []
                for case in selected_cases:
                    try:
                        cases.append(live_case(case, graph, ledger))
                    except Exception as exc:
                        failure = {"id": case["id"], "passed": False,
                                   "error_type": type(exc).__name__}
                        if isinstance(exc, ValidationError):
                            failure["validation_errors"] = exc.errors(include_input=False)
                        cases.append(failure)
                report["graph_cases"] = cases
                report["graph_passed"] = all(case["passed"] for case in cases)
            return report
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run eight-intent retail agent smoke checks")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Call the configured LLM")
    mode.add_argument("--scripted", action="store_true", help="Run the graph with a deterministic interpreter")
    parser.add_argument("--case-id", choices=[case["id"] for case in CASES])
    parser.add_argument("--output", type=Path,
                        default=ROOT / "outputs" / "retail_graph_demo" / "smoke_report.json")
    args = parser.parse_args()
    try:
        report = run(live=args.live, scripted=args.scripted, case_id=args.case_id)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "case_count": report["case_count"],
                      "preflight_passed": report["preflight_passed"],
                      "graph_passed": report.get("graph_passed"),
                      "report": str(args.output)}, indent=2))
    if not report["preflight_passed"] or report.get("graph_passed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
