"""Prepare unlabeled ABCD checkpoints and case-isolated SQLite fixtures.

Run from anywhere with: python scripts/prepare_test_cases.py
The generated cases contain cut positions, never reference answers. The future
agent harness must pass only materialize_agent_input(...) to the agent.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RAW_PATH = ROOT / "raw_data" / "abcd_v1.1.json.gz"
KB_PATH = ROOT / "raw_data" / "kb.json"
SCHEMA_PATH = ROOT / "sql" / "scenario_schema.sql"
OUTPUT_DIR = ROOT / "prepared_data"
SPLITS = ("train", "dev", "test")
INTENTS = (
    "return_size",
    "return_color",
    "return_stain",
    "promo_code_invalid",
    "promo_code_out_of_date",
    "manage_cancel",
    "manage_upgrade",
    "manage_downgrade",
)
RETURN_INTENTS = frozenset(INTENTS[:3])
PROMO_INTENTS = frozenset(INTENTS[3:5])
ORDER_INTENTS = frozenset(INTENTS[5:])
RETURN_EVIDENCE = re.compile(
    r"receipt|packag|purchas|bought|\bago\b|\b\d+\s*(?:days?|weeks?|months?|years?)\b|\b20\d\d\b",
    re.IGNORECASE,
)
RETURN_WRITE_ACTIONS = frozenset({"enter-details", "update-order", "offer-refund"})


def read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalized_conversation(convo: dict, split: str) -> dict:
    original = convo["original"]
    delexed = convo["delexed"]
    if len(original) != len(delexed):
        raise ValueError(f"unaligned turns in conversation {convo['convo_id']}")
    messages = []
    for number, (raw_turn, annotated_turn) in enumerate(zip(original, delexed), 1):
        if raw_turn[0].lower() != annotated_turn["speaker"]:
            raise ValueError(f"speaker mismatch in conversation {convo['convo_id']} turn {number}")
        if number > 1 and annotated_turn["turn_count"] <= delexed[number - 2]["turn_count"]:
            raise ValueError(f"nonmonotonic source turn count in conversation {convo['convo_id']}")
        messages.append({
            "turn": number,
            "source_turn_count": annotated_turn["turn_count"],
            "speaker": raw_turn[0].lower(),
            "text": raw_turn[1],
        })
    return {
        "convo_id": convo["convo_id"],
        "split": split,
        "flow": convo["scenario"]["flow"],
        "subflow": convo["scenario"]["subflow"],
        "messages": messages,
    }


def normalized_fixture(convo: dict, split: str) -> dict:
    scenario = convo["scenario"]
    personal = scenario["personal"]
    order = scenario["order"]
    product = scenario["product"]
    order_products = ast.literal_eval(order["products"])
    if not isinstance(order_products, list) or not all(isinstance(item, dict) for item in order_products):
        raise ValueError(f"invalid order products in conversation {convo['convo_id']}")
    count = int(order["num_products"])
    if count != len(order_products) or count != len(product["names"]) or count != len(product["amounts"]):
        raise ValueError(f"product count mismatch in conversation {convo['convo_id']}")
    items = []
    for index, (item, name, amount) in enumerate(
        zip(order_products, product["names"], product["amounts"])
    ):
        if item.get("amount") != amount:
            raise ValueError(f"product amount mismatch in conversation {convo['convo_id']}")
        items.append({
            "item_index": index,
            "name": name,
            "brand": item.get("brand"),
            "product_type": item.get("product_type"),
            "amount": amount,
            "image_url": item.get("image_url"),
        })
    return {
        "convo_id": convo["convo_id"],
        "split": split,
        "flow": scenario["flow"],
        "subflow": scenario["subflow"],
        "account": {key: personal.get(key) for key in (
            "customer_name", "account_id", "username", "email", "phone", "member_level"
        )},
        "order": {**{key: order.get(key) for key in (
            "order_id", "purchase_date", "shipping_status", "packaging", "payment_method",
            "street_address", "full_address", "city", "state", "zip_code"
        )}, "num_products": count},
        "products": items,
    }


def tool_cases(convo: dict, split: str, allowed_actions: set[str]) -> tuple[list[dict], int]:
    cases = []
    skipped = 0
    for index, turn in enumerate(convo["delexed"]):
        if turn["speaker"] != "action":
            continue
        action = turn["targets"][2]
        if action not in allowed_actions:
            skipped += 1
            continue
        target_turn = index + 1
        cases.append({
            "case_id": f"tool-{split}-{convo['convo_id']}-t{target_turn}",
            "kind": "tool_call",
            "convo_id": convo["convo_id"],
            "split": split,
            "fixture_id": convo["convo_id"],
            "cutoff_after_turn": index,
            "source_event_turn": target_turn,
            "reference_label_status": "not_created",
        })
    return cases, skipped


def _action_positions(convo: dict, name: str) -> list[int]:
    return [index for index, turn in enumerate(convo["delexed"])
            if turn["speaker"] == "action" and turn["targets"][2] == name]


def policy_case(convo: dict, split: str) -> dict | None:
    """Select a policy-sensitive prefix, without claiming its correct outcome."""
    intent = convo["scenario"]["subflow"]
    flags: list[str] = []
    if intent in RETURN_INTENTS:
        memberships = _action_positions(convo, "membership")
        if not memberships:
            return None
        start = memberships[0]
        later_writes = [index for index, turn in enumerate(convo["delexed"])
                        if index > start and turn["speaker"] == "action"
                        and turn["targets"][2] in RETURN_WRITE_ACTIONS]
        stop = min(later_writes) if later_writes else len(convo["delexed"])
        evidence = [index for index in range(0, stop)
                    if convo["delexed"][index]["speaker"] == "customer"
                    and RETURN_EVIDENCE.search(convo["original"][index][1])]
        anchor = max(start, evidence[-1]) if evidence else start
        if evidence and evidence[-1] > start:
            selection_rule = "return_evidence_after_membership"
        elif evidence:
            selection_rule = "membership_with_prior_return_evidence"
        else:
            selection_rule = "membership_stage"
        if not evidence:
            flags.append("return_evidence_not_detected")
    elif intent in PROMO_INTENTS:
        oracle = _action_positions(convo, "ask-the-oracle")
        if not oracle:
            return None
        memberships = [index for index in _action_positions(convo, "membership") if index > oracle[0]]
        anchor = memberships[0] if memberships else oracle[0]
        selection_rule = "promo_membership_stage" if memberships else "promo_oracle_stage"
        flags.append("oracle_result_not_structured_in_scenario")
    elif intent in ORDER_INTENTS:
        shipping = _action_positions(convo, "shipping-status")
        if not shipping:
            return None
        memberships = [index for index in _action_positions(convo, "membership") if index > shipping[0]]
        anchor = memberships[0] if memberships else shipping[0]
        selection_rule = "order_membership_stage" if memberships else "order_shipping_stage"
    else:
        return None
    cutoff = anchor + 1
    if cutoff >= len(convo["original"]):
        return None
    return {
        "case_id": f"policy-{split}-{convo['convo_id']}-t{cutoff}",
        "kind": "policy_decision",
        "convo_id": convo["convo_id"],
        "split": split,
        "fixture_id": convo["convo_id"],
        "cutoff_after_turn": cutoff,
        "selection_rule": selection_rule,
        "review_flags": flags,
        "reference_label_status": "not_created",
    }


def materialize_agent_input(case: dict, conversation: dict) -> dict:
    """The only case payload a future harness should hand to an agent."""
    if case["convo_id"] != conversation["convo_id"]:
        raise ValueError("case and conversation do not match")
    cutoff = case["cutoff_after_turn"]
    if not 0 <= cutoff <= len(conversation["messages"]):
        raise ValueError("cutoff is outside the conversation")
    return {"messages": conversation["messages"][:cutoff]}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_sqlite(path: Path, fixtures: list[dict]) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="scenario-", suffix=".sqlite", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with closing(sqlite3.connect(temp_path)) as connection:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            for fixture in fixtures:
                cid = fixture["convo_id"]
                connection.execute(
                    "INSERT INTO scenarios VALUES (?, ?, ?, ?)",
                    (cid, fixture["split"], fixture["flow"], fixture["subflow"]),
                )
                account = fixture["account"]
                connection.execute(
                    "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cid, *(account.get(key) for key in (
                        "customer_name", "account_id", "username", "email", "phone", "member_level"
                    ))),
                )
                order = fixture["order"]
                connection.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, *(order.get(key) for key in (
                        "order_id", "num_products", "purchase_date", "shipping_status", "packaging",
                        "payment_method", "street_address", "full_address", "city", "state", "zip_code"
                    ))),
                )
                connection.executemany(
                    "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(cid, *(item.get(key) for key in (
                        "item_index", "name", "brand", "product_type", "amount", "image_url"
                    ))) for item in fixture["products"]],
                )
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("SQLite foreign key check failed")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def build(output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(RAW_PATH, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    kb = read_json(KB_PATH)
    output_dir.mkdir(parents=True, exist_ok=True)
    conversations: list[dict] = []
    fixtures: list[dict] = []
    tools: list[dict] = []
    policies: list[dict] = []
    skipped_actions = 0
    seen_ids: set[int] = set()
    for split in SPLITS:
        for convo in data[split]:
            intent = convo["scenario"]["subflow"]
            if intent not in INTENTS:
                continue
            cid = convo["convo_id"]
            if cid in seen_ids:
                raise ValueError(f"duplicate conversation id {cid}")
            seen_ids.add(cid)
            conversations.append(normalized_conversation(convo, split))
            fixtures.append(normalized_fixture(convo, split))
            candidates, skipped = tool_cases(convo, split, set(kb[intent]))
            tools.extend(candidates)
            skipped_actions += skipped
            candidate = policy_case(convo, split)
            if candidate is not None:
                policies.append(candidate)
    write_jsonl(output_dir / "conversations.jsonl", conversations)
    write_jsonl(output_dir / "scenario_fixtures.jsonl", fixtures)
    write_jsonl(output_dir / "tool_cases.jsonl", tools)
    write_jsonl(output_dir / "policy_cases.jsonl", policies)
    build_sqlite(output_dir / "scenario_fixtures.sqlite", fixtures)
    with closing(sqlite3.connect(output_dir / "scenario_fixtures.sqlite")) as connection:
        db_counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                     for name in ("scenarios", "accounts", "orders", "products")}
    by_id = {row["convo_id"]: row for row in conversations}
    for case in [*tools, *policies]:
        visible = materialize_agent_input(case, by_id[case["convo_id"]])["messages"]
        if visible and visible[-1]["turn"] != case["cutoff_after_turn"]:
            raise ValueError(f"visible history is misaligned in {case['case_id']}")
        if case["kind"] == "tool_call" and case["source_event_turn"] != case["cutoff_after_turn"] + 1:
            raise ValueError(f"future action leaked in {case['case_id']}")
    manifest = {
        "source": str(RAW_PATH.relative_to(ROOT)).replace("\\", "/"),
        "source_sha256": hashlib.sha256(RAW_PATH.read_bytes()).hexdigest(),
        "selected_intents": list(INTENTS),
        "reference_labels_created": False,
        "conversations": len(conversations),
        "conversations_by_split": dict(Counter(row["split"] for row in conversations)),
        "conversations_with_source_turn_gaps": sum(
            any(message["source_turn_count"] != message["turn"] for message in row["messages"])
            for row in conversations
        ),
        "tool_cases": len(tools),
        "tool_cases_by_split": dict(Counter(row["split"] for row in tools)),
        "tool_cases_by_intent": dict(Counter(by_id[row["convo_id"]]["subflow"] for row in tools)),
        "policy_cases": len(policies),
        "policy_cases_by_split": dict(Counter(row["split"] for row in policies)),
        "policy_cases_by_intent": dict(Counter(by_id[row["convo_id"]]["subflow"] for row in policies)),
        "policy_selection_rules": dict(Counter(row["selection_rule"] for row in policies)),
        "policy_review_flags": dict(Counter(flag for row in policies for flag in row["review_flags"])),
        "actions_outside_selected_kb": skipped_actions,
        "sqlite_rows": db_counts,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir), ensure_ascii=False, indent=2))
