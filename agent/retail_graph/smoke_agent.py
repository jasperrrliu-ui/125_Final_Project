"""Multi-turn smoke runner for the policy-driven retail agent.

Each case is a short scripted conversation against real synthetic records,
the real policy engine and gate, and a local Oracle HTTP server.

--scripted  uses a deterministic interpreter (the scripted extraction next
            to each customer turn). It checks graph wiring and policy
            outcomes, not language understanding.
--live      sends the same customer turns to the configured LLM.

These are smoke conversations, not reviewed gold labels or an accuracy
benchmark.
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

from agent.action_log.action_ledger import ActionLedger
from agent.action_log.policy_gate import PolicyGate
from agent.oracle_demo.core import DEMO_TIME
from agent.oracle_demo.stdlib_api import make_server
from agent.retail_graph.graph import build_graph
from agent.retail_tools.check_coupon import check_coupon


ROOT = Path(__file__).resolve().parents[2]

# Each turn: (customer message, scripted extraction). Each case lists the
# expected final ledger outcome per request, in order.
CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "return_size_gold_by_mail",
        "turns": [
            ("I need to return a jacket from order 7793529334, it's the wrong size.",
             {"requests": [{"intent": "return_size", "order_id": "7793529334"}]}),
            ("My email is jayden.parker.0403@example.test",
             {"identity": {"email": "jayden.parker.0403@example.test"}}),
            ("By mail, please go ahead.",
             {"selected_option": "return_by_mail", "confirmation": "yes"}),
        ],
        "expected": [("return_size", "simulated", "return_gold")],
    },
    {
        "id": "return_color_silver_no_receipt",
        "turns": [
            ("I'm Fiona Price, zip 60040. Order 5110435718 came in the wrong color and I want to return it.",
             {"identity": {"full_name": "Fiona Price", "zip_code": "60040"},
              "requests": [{"intent": "return_color", "order_id": "5110435718"}]}),
            ("No, I threw the receipt away.", {"customer_facts": {"has_receipt": False}}),
        ],
        "expected": [("return_color", "declined", "return_not_eligible")],
    },
    {
        "id": "return_stain_bronze_packaging",
        "turns": [
            ("Amir Green, 70556. The shirt in order 5867575994 has a stain, I'd like to return it.",
             {"identity": {"full_name": "Amir Green", "zip_code": "70556"},
              "requests": [{"intent": "return_stain", "order_id": "5867575994"}]}),
            ("In store works. Yes.", {"selected_option": "return_in_store", "confirmation": "yes"}),
        ],
        "expected": [("return_stain", "simulated", "return_bronze")],
    },
    {
        "id": "promo_invalid_guest_platform_error",
        "turns": [
            ("My promo code GUEST15 was rejected at checkout. felix.hayes.0300@example.test",
             {"identity": {"email": "felix.hayes.0300@example.test"},
              "requests": [{"intent": "promo_code_invalid", "coupon_code": "GUEST15"}]}),
            ("Yes please.", {"confirmation": "yes"}),
        ],
        "expected": [("promo_code_invalid", "simulated", "promo_platform_error")],
    },
    {
        "id": "promo_invalid_guest_declined",
        "turns": [
            ("Code FAKE20 doesn't work. I'm felix.hayes.0300@example.test",
             {"identity": {"email": "felix.hayes.0300@example.test"},
              "requests": [{"intent": "promo_code_invalid", "coupon_code": "FAKE20"}]}),
        ],
        "expected": [("promo_code_invalid", "declined", "promo_guest_no_code")],
    },
    {
        "id": "promo_out_of_date_member_goodwill",
        "turns": [
            ("The code OLD20 I got weeks ago no longer works. eli.gray.0001@example.test",
             {"identity": {"email": "eli.gray.0001@example.test"},
              "requests": [{"intent": "promo_code_out_of_date", "coupon_code": "OLD20"}]}),
            ("Sure, yes.", {"confirmation": "yes"}),
        ],
        "expected": [("promo_code_out_of_date", "simulated", "promo_member_goodwill")],
    },
    {
        "id": "cancel_multi_item_then_yes",
        "turns": [
            ("I ordered an extra item by mistake in order 0867025956. fiona.price.0107@example.test",
             {"identity": {"email": "fiona.price.0107@example.test"},
              "requests": [{"intent": "manage_cancel", "order_id": "0867025956"}]}),
            ("The jacket.", {"fields": {"item_name": "jacket"}}),
            ("Yes, go ahead.", {"confirmation": "yes"}),
        ],
        "expected": [("manage_cancel", "simulated", "cancel_order_received")],
    },
    {
        "id": "upgrade_paid_then_yes",
        "turns": [
            ("Can order 5221061290 arrive tomorrow? katherine.sanchez.0745@example.test",
             {"identity": {"email": "katherine.sanchez.0745@example.test"},
              "requests": [{"intent": "manage_upgrade", "order_id": "5221061290"}]}),
            ("Yes, charge the $20.", {"confirmation": "yes"}),
        ],
        "expected": [("manage_upgrade", "simulated", "upgrade_paid")],
    },
    {
        "id": "upgrade_already_shipped",
        "turns": [
            ("Please upgrade order 9722098994 to overnight. Maya Cooper, 98875.",
             {"identity": {"full_name": "Maya Cooper", "zip_code": "98875"},
              "requests": [{"intent": "manage_upgrade", "order_id": "9722098994"}]}),
        ],
        "expected": [("manage_upgrade", "declined", "upgrade_already_shipped")],
    },
    {
        "id": "downgrade_shipped_member_plus_out_of_scope",
        "turns": [
            ("Move order 5715577786 to next week, and also reset my password. Camila Jackson 71364.",
             {"identity": {"full_name": "Camila Jackson", "zip_code": "71364"},
              "requests": [{"intent": "manage_downgrade", "order_id": "5715577786"}],
              "out_of_scope": "reset my password"}),
            ("Okay, yes.", {"confirmation": "yes"}),
        ],
        "expected": [("out_of_scope", "handoff", None),
                     ("manage_downgrade", "simulated", "downgrade_shipped_member")],
    },
)


class ScriptedInterpreter:
    """Deterministic graph exercise; it does not measure LLM quality."""

    def __init__(self, cases: tuple[dict[str, Any], ...] = CASES):
        self.by_message = {msg: ext for case in cases for msg, ext in case["turns"]}

    def extract(self, latest_message: str, state: dict) -> dict:
        return self.by_message[latest_message]


def prepare_oracle(folder: Path):
    """Start the real local HTTP adapter with disposable coupon databases."""
    server = make_server(port=0, data_dir=folder, secret=b"retail-smoke-local-key")
    with closing(sqlite3.connect(folder / "merchant_coupons.sqlite")) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO coupons VALUES (?,?,?,?,?,?,?,?,?)",
            ("M-101", "GUEST15", "C0300", "2026-10-29T00:00:00+00:00",
             "2026-11-08T00:00:00+00:00", 0, "active", 15, None),
        )
    platform_db = folder / "platform_oracle.sqlite"
    with closing(sqlite3.connect(platform_db)) as db, db:
        db.executemany("INSERT OR IGNORE INTO checkout_attempts VALUES (?,?,?,?,?,?,?)", [
            ("ATT-GUEST-1", "M-101", "C0300", "GUEST15", 7500, "rejected", "COUPON_REJECTED"),
            ("ATT-INVALID-2", "M-101", "C0300", "FAKE20", 7500, "rejected", "COUPON_REJECTED"),
        ])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, platform_db


def run_case(case: dict[str, Any], graph: Any, ledger: ActionLedger) -> dict[str, Any]:
    thread_id = f"smoke-{case['id']}"
    transcript = []
    result: dict[str, Any] = {}
    for message, _ in case["turns"]:
        result = graph.invoke({"messages": [HumanMessage(content=message)]},
                              {"configurable": {"thread_id": thread_id}})
        transcript.append({"customer": message, "agent": result["reply_text"]})
    events = ledger.list_conversation(thread_id)
    actual = [(e["intent"], e["outcome"], e["policy_rule_id"]) for e in events]
    expected = [tuple(x) for x in case["expected"]]
    return {
        "id": case["id"],
        "passed": actual == expected,
        "expected": expected,
        "actual": actual,
        "transcript": transcript,
        "extract_events": result.get("extract_events", []),
        "cp1_events": result.get("cp1_events", []),
        "cp2_events": result.get("cp2_events", []),
    }


def run(*, live: bool = False, case_id: str | None = None) -> dict[str, Any]:
    if live and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Live mode requires OPENAI_API_KEY")
    selected = [c for c in CASES if case_id is None or c["id"] == case_id]
    if not selected:
        raise ValueError(f"Unknown case ID: {case_id}")
    with tempfile.TemporaryDirectory(prefix="retail-agent-smoke-") as temp:
        folder = Path(temp)
        server, server_thread, platform_db = prepare_oracle(folder)
        oracle_url = f"http://127.0.0.1:{server.server_port}"
        try:
            if live:
                from agent.retail_graph.llm_interpreter import OpenAIInterpreter
                interpreter: Any = OpenAIInterpreter()
            else:
                interpreter = ScriptedInterpreter(tuple(selected))
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
                for case in selected:
                    try:
                        cases.append(run_case(case, graph, ledger))
                    except Exception as exc:  # report and continue with other cases
                        cases.append({"id": case["id"], "passed": False,
                                      "error_type": type(exc).__name__, "error": str(exc)})
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
    return {
        "mode": "live_llm" if live else "scripted_graph",
        "policy": gate.version,
        "case_count": len(cases),
        "passed": all(c["passed"] for c in cases),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run multi-turn retail agent smoke conversations")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Call the configured LLM")
    mode.add_argument("--scripted", action="store_true",
                      help="Use the deterministic interpreter (default)")
    parser.add_argument("--case-id", choices=[case["id"] for case in CASES])
    parser.add_argument("--output", type=Path,
                        default=ROOT / "outputs" / "retail_graph_demo" / "smoke_report.json")
    args = parser.parse_args()
    try:
        report = run(live=args.live, case_id=args.case_id)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "mode": report["mode"], "case_count": report["case_count"], "passed": report["passed"],
        "failed": [c["id"] for c in report["cases"] if not c["passed"]],
        "report": str(args.output),
    }, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
