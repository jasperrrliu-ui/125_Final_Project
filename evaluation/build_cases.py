"""Build CP1, CP2, and full-conversation evaluation cases.

Cases come from the synthetic retail database (orders from the ABCD test and
dev splits), not from historical ABCD dialogue prefixes, because the agent
asks different questions in a different order than ABCD's human agents.

Each CP1/CP2 case is a scripted prefix that brings the graph to a stop point,
plus one new customer message. Expected results are derived, not hand-typed:
the case's expected extraction (what a correct reading of the message gives)
is sent through the real graph, and the resulting events become the
expectation. Each case also states what its author intended (for example
"no action is written"); the build fails if the derived result disagrees.

CP2 expected decisions come from the policy engine and are marked
``provisional_engine`` until a reference label made from
``raw_data/guidelines.json`` alone has been reviewed (docs/WORKFLOW.md).

Run from the repository root:  python -m evaluation.build_cases
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import random
import sqlite3
from typing import Any

from agent.policy.engine import Policy, evaluate, resolve_facts
from agent.policy.records import coupon_record, customer_record, order_record
from agent.retail_tools.read_tools import lookup_order, verify_customer
from evaluation.harness import ExpectedInterpreter, run_case, run_script

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "processed_data" / "customer_simulation"
OUT = ROOT / "evaluation" / "cases"
SEED = 1125
POLICY = Policy.load()
FAMILY = {i: f for f, intents in POLICY.raw["intent_families"].items() for i in intents}


# --------------------------------------------------------------------- pool
def _spoken(item_name: str) -> str:
    brand, _, kind = item_name.partition(" ")
    return f"{brand.replace('_', ' ').title()} {kind}".strip()


def load_pool() -> list[dict[str, Any]]:
    """Test and dev orders whose customer can be verified by email."""
    flagged = {json.loads(line)["source_convo_id"]
               for line in open(SIM / "identity_review_flags.jsonl", encoding="utf-8")}
    db = sqlite3.connect(SIM / "synthetic_retail.sqlite")
    db.row_factory = sqlite3.Row
    customers = {r["customer_id"]: dict(r) for r in db.execute("SELECT * FROM customers")}
    pool = []
    for line in open(SIM / "source_cases.jsonl", encoding="utf-8"):
        src = json.loads(line)
        if src["split"] not in {"test", "dev"} or src["source_convo_id"] in flagged:
            continue
        customer = customers[src["customer_id"]]
        order = lookup_order(src["customer_id"], src["order_id"])
        if order["status"] != "found":
            continue
        if verify_customer({"email": customer["email"]}).get("customer_id") != src["customer_id"]:
            continue
        facts = order["facts"]
        zip_code = facts.get("zip_code") or ""
        name_zip_ok = (len(zip_code) == 5 and verify_customer(
            {"full_name": customer["customer_name"], "zip_code": zip_code}
        ).get("customer_id") == src["customer_id"])
        names = [x["name"] for x in facts["items"]]
        pool.append({
            "intent": src["subflow"], "split": src["split"],
            "customer_id": src["customer_id"], "name": customer["customer_name"],
            "email": customer["email"], "account_id": customer["account_id"],
            "member_level": customer["member_level"], "zip": zip_code,
            "name_zip_ok": name_zip_ok, "order_id": src["order_id"], "facts": facts,
            "items": facts["items"], "distinct_items": len(set(names)) == len(names),
        })
    pool.sort(key=lambda e: (e["split"] != "test", e["order_id"]))
    return pool


def system_decision(entry: dict[str, Any], customer_facts: dict | None = None,
                    oracle: bool | None = None):
    records = {"customer": customer_record(entry["member_level"]),
               "order": order_record(entry["facts"])}
    if FAMILY[entry["intent"]] == "promo":
        records = {"customer": records["customer"],
                   "coupon_check": coupon_record(
                       {"status": "resolved", "customer_right": oracle} if oracle is not None
                       else None)}
    context = {"item": entry["items"][0], "address": "x"}
    return evaluate(POLICY, entry["intent"], resolve_facts(POLICY, records, customer_facts or {}),
                    context)


def true_facts(entry: dict[str, Any], **customer_known) -> dict[str, Any]:
    facts = {"member_level": entry["member_level"]}
    if FAMILY[entry["intent"]] != "promo":
        f = entry["facts"]
        facts.update(shipping_status=f.get("shipping_status"), purchase_date=f.get("purchase_date"),
                     original_packaging={"yes": True, "no": False}.get(f.get("packaging")))
    facts.update({k: v for k, v in customer_known.items() if v is not None})
    return facts


# -------------------------------------------------------------------- turns
CORE = {
    "return_size": ["the {item} I ordered doesn't fit and I'd like to return it",
                    "I need to send back {item_a}, it's the wrong size",
                    "{item_cap} doesn't fit at all, can I return it?"],
    "return_color": ["the {item} came in the wrong color and I want to return it",
                     "I'd like to return {item_a}; the color isn't what I ordered",
                     "{item_cap} arrived in a different color than the site showed, so I'm returning it"],
    "return_stain": ["the {item} arrived with a stain on it and I want to return it",
                     "there's a stain on {item_a}, I'd like to send it back",
                     "{item_cap} came stained, can I return it?"],
    "manage_cancel": ["I accidentally ordered an extra {item} and want to remove it",
                      "please take {item_a} off my order, I added it by mistake",
                      "I don't need the {item} I added, can you cancel it?"],
    "manage_upgrade": ["can I get my order delivered tomorrow instead?",
                       "I need my package sooner, is faster shipping possible?",
                       "please upgrade my delivery so it arrives tomorrow"],
    "manage_downgrade": ["I won't be home, could my order arrive next week instead?",
                         "there's no rush, can you switch my order to slower shipping?",
                         "please push my delivery back to next week"],
    "promo_code_invalid": ["{code_a} you sent me a few days ago isn't working",
                           "I got {code_a} this week but checkout keeps rejecting it",
                           "{code_cap} from two days ago doesn't apply at checkout"],
    "promo_code_out_of_date": ["{code_a} from last month won't work anymore",
                               "{code_cap} I got a few weeks ago is being rejected",
                               "I'm trying to use {code_a} that I received about a month ago and it says it's invalid"],
}
UNNAMED_ITEM = {"return_size": "something I bought", "return_color": "something I bought",
                "return_stain": "something I bought", "manage_cancel": "something"}


def request_turn(entry, *, variant=0, with_item=True, with_order=False, identity=None,
                 code=None, extra=""):
    intent, family = entry["intent"], FAMILY[entry["intent"]]
    item = _spoken(entry["items"][0]["name"]) if family in {"return", "cancel"} else None
    named = with_item and item is not None
    if named:
        words = {"item": item, "item_a": f"the {item}", "item_cap": f"The {item}"}
    else:
        thing = UNNAMED_ITEM.get(intent, "an item")
        words = {"item": "item", "item_a": thing, "item_cap": thing.capitalize()}
    words.update(code_a=f"the promo code {code}" if code else "a promo code",
                 code_cap=f"The code {code}" if code else "A promo code")
    core = CORE[intent][variant % 3]
    if not named and intent == "manage_cancel":
        core = "I added something to my order by mistake and want to remove it"
    text = core.format(**words)
    text = text[0].upper() + text[1:]
    request: dict[str, Any] = {"intent": intent}
    if named:
        request["item_name"] = item
    if not text.endswith(("?", ".")):
        text += "."
    if with_order and family != "promo":
        text += f" It's order {entry['order_id']}."
        request["order_id"] = entry["order_id"]
    if code:
        request["coupon_code"] = code
    parsed: dict[str, Any] = {"requests": [request]}
    if identity == "email":
        text += f" My email is {entry['email']}."
        parsed["identity"] = {"email": entry["email"]}
    elif identity == "name_zip":
        text += f" I'm {entry['name']}, zip code {entry['zip']}."
        parsed["identity"] = {"full_name": entry["name"], "zip_code": entry["zip"]}
    if extra:
        text += " " + extra
    return {"message": text, "parsed": parsed}


def _lower_first(text: str) -> str:
    return text if text.startswith(("I ", "I'")) else text[0].lower() + text[1:]


def identity_turn(entry, method="email"):
    if method == "email":
        return {"message": f"Sure, it's {entry['email']}.",
                "parsed": {"identity": {"email": entry["email"]}}}
    return {"message": f"{entry['name']}, {entry['zip']}.",
            "parsed": {"identity": {"full_name": entry["name"], "zip_code": entry["zip"]}}}


def order_turn(entry):
    return {"message": f"The order number is {entry['order_id']}.",
            "parsed": {"fields": {"order_id": entry["order_id"]}}}


def coupon_code_for(entry) -> str:
    return f"SAVE{int(entry['order_id']) % 90 + 10}"


def prefix_to(entry, stop: str, *, identity="email", code=None) -> list[dict[str, Any]]:
    """Shortest scripted conversation that leaves the agent waiting at ``stop``."""
    family = FAMILY[entry["intent"]]
    if stop == "identity":
        return [request_turn(entry, with_order=True)]
    if stop in {"order_id", "coupon_code"}:
        return [request_turn(entry, with_item=family == "cancel"), identity_turn(entry, identity)]
    if stop == "item":
        return [request_turn(entry, with_item=False, with_order=True), identity_turn(entry, identity)]
    # Decision stages: request with all targets, then identity.
    turns = [request_turn(entry, with_order=True, code=code), identity_turn(entry, identity)]
    return turns


def offered_prefix(entry, code=None, receipt=None):
    turns = prefix_to(entry, "decision", code=code)
    if receipt is not None:
        turns.append({"message": "Yes, I still have the receipt." if receipt else
                      "No, I don't have it.", "parsed": {"customer_facts": {"has_receipt": receipt}}})
    return turns


# -------------------------------------------------------------------- build
class Builder:
    def __init__(self, pool):
        self.pool = pool
        self.rng = random.Random(SEED)
        self.cases: dict[str, list[dict]] = defaultdict(list)
        self.used = Counter()

    def pick(self, predicate, n=1, *, spread=True):
        candidates = [e for e in self.pool if predicate(e)]
        if len(candidates) < n:
            raise ValueError(f"only {len(candidates)} pool entries match")
        if spread:  # prefer entries not used yet
            candidates.sort(key=lambda e: (self.used[e["order_id"]], self.rng.random()))
        chosen = candidates[:n]
        for e in chosen:
            self.used[e["order_id"]] += 1
        return chosen

    def add(self, layer, category, entry, prefix, message, parsed, *, stop, intended,
            facts=None, verdict=None, notes=""):
        index = sum(1 for c in self.cases[layer] if c["category"] == category) + 1
        case = {
            "id": f"{layer}-{category}-{index:02d}",
            "layer": layer, "category": category, "intent": entry["intent"],
            "customer_id": entry["customer_id"], "order_id": entry["order_id"],
            "true_facts": facts or true_facts(entry),
            "coupon_verdict": verdict,
            "prefix": prefix, "message": message,
            "notes": notes,
        }
        result = run_case(case, ExpectedInterpreter(parsed))
        if result["stop_point"] != stop:
            raise AssertionError(f"{case['id']}: stopped at {result['stop_point']}, wanted {stop}")
        expected = {
            "extraction": parsed,
            "cp1": [{k: e[k] for k in ("next_step", "planned_tool", "arguments", "asked")}
                    for e in result["cp1_events"]],
            "cp2": [{k: v for k, v in e.items() if k not in {"turn", "request_id"}}
                    for e in result["cp2_events"]],
            "ledger": [{k: r[k] for k in ("intent", "action_type", "outcome", "policy_rule_id",
                                          "payload")} for r in result["ledger"]],
        }
        expected["action_written"] = any(r["outcome"] == "simulated" for r in result["ledger"])
        # Which order item each spoken item name refers to; the scorer checks
        # that a predicted name resolves to the same item, not the same words.
        named = [(r.get("order_id") or entry["order_id"], r["item_name"])
                 for r in parsed.get("requests") or [] if r.get("item_name")]
        if (parsed.get("fields") or {}).get("item_name"):
            named.append((entry["order_id"], parsed["fields"]["item_name"]))
        if named:
            expected["items"] = []
            for order_id, name in named:
                items = lookup_order(entry["customer_id"], order_id)["facts"]["items"]
                index = next(x["item_index"] for x in items
                             if _spoken(x["name"]).casefold() == name.casefold())
                expected["items"].append({"order_id": order_id, "item_name": name,
                                          "item_index": index})
        _check_intent(case["id"], intended, expected)
        case.update(stop_point=stop, expected=expected, label_status=(
            "provisional_engine" if layer == "cp2" else "derived_from_construction"))
        self.cases[layer].append(case)
        return case


def _check_intent(case_id, intended, expected):
    """The case author's intent must match what the graph derived."""
    for key, want in intended.items():
        if key == "action":
            got = expected["action_written"]
        elif key == "first":  # the first step the agent takes after this message
            first = expected["cp1"][0] if expected["cp1"] else {}
            got = first.get("asked") or first.get("planned_tool") or first.get("next_step")
        elif key == "tools":
            got = [e["planned_tool"] for e in expected["cp1"] if e["planned_tool"]]
        elif key == "decision":
            got = [e["decision"] for e in expected["cp2"]]
        elif key == "decided":  # the policy reached a decision this turn
            got = bool(expected["cp2"])
        elif key == "first_decision":
            got = expected["cp2"][0]["decision"] if expected["cp2"] else None
        elif key == "rule":
            got = next((e["rule_id"] for e in expected["cp2"] if e.get("rule_id")), None)
        else:
            raise KeyError(key)
        if got != want:
            raise AssertionError(f"{case_id}: intended {key}={want!r}, graph gives {got!r}")


# ---------------------------------------------------------------- CP1 cases
def build_cp1(b: Builder):
    intents = list(CORE)
    # Opening message: three ways of raising each intent.
    for intent in intents:
        promo = FAMILY[intent] == "promo"
        for variant, style in enumerate(("bare", "with_order", "with_identity")):
            pred = lambda e, i=intent, s=style: e["intent"] == i and (
                s != "with_identity" or e["name_zip_ok"])
            entry, = b.pick(pred)
            code = coupon_code_for(entry) if promo and style != "bare" else None
            identity = None
            if style == "with_identity":
                identity = "name_zip" if variant % 2 or promo else "email"
            turn = request_turn(entry, variant=variant, with_item=style != "bare",
                                with_order=style != "bare", identity=identity, code=code)
            if identity:
                next_step = "verify_customer"
            else:
                next_step = "identity"
            b.add("cp1", "opening", entry, [], turn["message"], turn["parsed"],
                  stop="start", intended={"first": next_step})

    # Two requests in one message (same customer, two orders).
    by_customer = defaultdict(list)
    for e in b.pool:
        if FAMILY[e["intent"]] != "promo":
            by_customer[e["customer_id"]].append(e)
    pairs = [v[:2] for v in by_customer.values()
             if len(v) >= 2 and v[0]["intent"] != v[1]["intent"]]
    b.rng.shuffle(pairs)
    for first, second in pairs[:6]:
        one = request_turn(first, variant=0, with_order=True)
        two = request_turn(second, variant=1, with_order=True)
        text = (f"Hi, two things. First: {_lower_first(one['message'])} "
                f"Second: {_lower_first(two['message'])} "
                f"My email is {first['email']}.")
        parsed = {"identity": {"email": first["email"]},
                  "requests": one["parsed"]["requests"] + two["parsed"]["requests"]}
        b.add("cp1", "two_requests", first, [], text, parsed, stop="start",
              intended={"tools": ["verify_customer", "lookup_order"]},
              notes=f"Second request is order {second['order_id']}.")

    # A supported request plus something out of scope.
    for entry, extra, desc in zip(
            b.pick(lambda e: e["intent"] in {"manage_cancel", "manage_downgrade"}, 2),
            ("Also, can you update the phone number on my account?",
             "And while you're at it, please delete my saved credit card."),
            ("update the phone number on my account", "delete my saved credit card")):
        turn = request_turn(entry, with_order=True, identity="email", extra=extra)
        parsed = dict(turn["parsed"], out_of_scope=desc)
        b.add("cp1", "out_of_scope", entry, [], turn["message"], parsed, stop="start",
              intended={"first_decision": "handoff"},
              notes="Scorer should accept any short description of the unsupported request.")

    # Identity answers.
    id_entries = b.pick(lambda e: e["name_zip_ok"] and FAMILY[e["intent"]] != "promo", 8)
    identity_msgs = [
        (lambda e: e["email"], lambda e: {"email": e["email"]}, "verify_customer"),
        (lambda e: f"You can find me under {e['email'].upper()}",
         lambda e: {"email": e["email"].upper()}, "verify_customer"),
        (lambda e: f"{e['name']}, zip {e['zip']}",
         lambda e: {"full_name": e["name"], "zip_code": e["zip"]}, "verify_customer"),
        (lambda e: f"My zip is {e['zip']} and the name is {e['name']}.",
         lambda e: {"full_name": e["name"], "zip_code": e["zip"]}, "verify_customer"),
        (lambda e: f"My account ID is {e['account_id']}.",
         lambda e: {"account_id": e["account_id"]}, "identity"),
        (lambda e: f"It's {e['name']}.", lambda e: {"full_name": e["name"]}, "identity"),
        (lambda e: f"Sorry, long day. The email should be {e['email']}, thanks for waiting!",
         lambda e: {"email": e["email"]}, "verify_customer"),
        (lambda e: f"I used {e['email'].replace('@example.test', '@gmail.com')}... no wait, "
                   f"it's {e['email']}.", lambda e: {"email": e["email"]}, "verify_customer"),
    ]
    for entry, (text, ident, nxt) in zip(id_entries, identity_msgs):
        b.add("cp1", "identity", entry, prefix_to(entry, "identity"), text(entry),
              {"identity": ident(entry)}, stop="identity",
              intended={"tools": ["verify_customer"] + (["lookup_order"] if nxt != "identity" else [])}
              if nxt != "identity" else {"first": "identity"})

    # Order ID answers.
    order_entries = b.pick(lambda e: FAMILY[e["intent"]] in {"return", "upgrade", "downgrade"}, 8)
    order_msgs = [
        lambda o: (o, o), lambda o: (f"#{o}", o), lambda o: (f"Sure, it's {o}.", o),
        lambda o: (f"Order {o}, placed a couple of weeks ago.", o),
        lambda o: (f"{o[:3]} {o[3:6]} {o[6:]}", o),
        lambda o: (f"I think it's {o}? It's on the confirmation email.", o),
        lambda o: ("I don't have the number with me right now.", None),
        lambda o: ("Can't you look it up from my account?", None),
    ]
    for entry, fn in zip(order_entries, order_msgs):
        text, oid = fn(entry["order_id"])
        parsed = {"fields": {"order_id": oid}} if oid else {}
        b.add("cp1", "order_id", entry, prefix_to(entry, "order_id"), text, parsed,
              stop="order_id", intended={"tools": ["lookup_order"]} if oid else {"first": "order_id"},
              notes="Expected order_id is the digits only." if oid and " " in text else "")

    # Which item (cancel orders with several distinct items).
    item_entries = b.pick(lambda e: e["intent"] == "manage_cancel" and len(e["items"]) > 1
                          and e["distinct_items"], 4)
    for n, entry in enumerate(item_entries):
        target = entry["items"][-1]
        spoken = _spoken(target["name"])
        text = [f"The {spoken}.", f"It's the {spoken} I don't need.",
                f"I meant the {spoken}, sorry.", f"{spoken} please"][n]
        b.add("cp1", "item", entry, prefix_to(entry, "item"), text,
              {"fields": {"item_name": spoken}}, stop="item", intended={"decided": True})

    # Coupon code answers.
    coupon_entries = b.pick(lambda e: FAMILY[e["intent"]] == "promo" and e["member_level"] != "guest", 4)
    for n, entry in enumerate(coupon_entries):
        code = coupon_code_for(entry)
        text = [code, f"The code was {code.lower()}.", f"It's {code}, it came in your newsletter.",
                f"Code: {code[:4]}-{code[4:]}"][n]
        b.add("cp1", "coupon_code", entry, prefix_to(entry, "coupon_code"), text,
              {"fields": {"coupon_code": code}}, stop="coupon_code",
              verdict={"coupon_code": code, "customer_right": False},
              intended={"tools": ["check_coupon"]},
              notes="Expected code is SAVEnn; the scorer compares case-insensitively "
                    "without separators." if n == 3 else "")

    # Corrections after options were offered.
    correction_entries = b.pick(lambda e: FAMILY[e["intent"]] in {"upgrade", "downgrade"}
                                and system_decision(e).kind == "options", 4)
    for n, entry in enumerate(correction_entries):
        other = next((e for e in b.pool if e["customer_id"] == entry["customer_id"]
                      and e["order_id"] != entry["order_id"]), None)
        new_oid = other["order_id"] if other else f"{(int(entry['order_id']) + 7) % 10**10:010d}"
        text = [f"Sorry, wrong order. I meant {new_oid}.",
                f"Wait, it's actually order {new_oid}, not that one.",
                f"Oops, typo: {new_oid}.", f"Hold on, the right number is {new_oid}."][n]
        b.add("cp1", "correction", entry, offered_prefix(entry), text,
              {"fields": {"order_id": new_oid}}, stop="choose_and_confirm",
              intended={"tools": ["lookup_order"]},
              notes="The new order is on this account." if other else
                    "The new order ID is not on this account; the agent should say so.")


# ---------------------------------------------------------------- CP2 cases
def build_cp2(b: Builder):
    # Decisions that need no customer facts: one or two per policy rule.
    plans = [
        ("return_gold", lambda e: FAMILY[e["intent"]] == "return" and e["member_level"] == "gold", None),
        ("return_silver", lambda e: FAMILY[e["intent"]] == "return" and e["member_level"] == "silver", None),
        ("return_bronze", lambda e: FAMILY[e["intent"]] == "return" and e["member_level"] == "bronze", None),
        ("return_guest", lambda e: FAMILY[e["intent"]] == "return" and e["member_level"] == "guest", None),
        ("cancel_order_received", lambda e: e["intent"] == "manage_cancel", None),
        ("cancel_shipped_member", lambda e: e["intent"] == "manage_cancel", None),
        ("cancel_shipped_guest", lambda e: e["intent"] == "manage_cancel", None),
        ("upgrade_already_shipped", lambda e: e["intent"] == "manage_upgrade", None),
        ("upgrade_gold", lambda e: e["intent"] == "manage_upgrade", None),
        ("upgrade_paid", lambda e: e["intent"] == "manage_upgrade", None),
        ("downgrade_order_received", lambda e: e["intent"] == "manage_downgrade", None),
        ("downgrade_shipped_member", lambda e: e["intent"] == "manage_downgrade", None),
        ("downgrade_shipped_guest", lambda e: e["intent"] == "manage_downgrade", None),
        ("promo_platform_error", lambda e: FAMILY[e["intent"]] == "promo", True),
        ("promo_member_goodwill", lambda e: FAMILY[e["intent"]] == "promo"
         and e["member_level"] != "guest", False),
        ("promo_guest_no_code", lambda e: FAMILY[e["intent"]] == "promo"
         and e["member_level"] == "guest", False),
    ]
    for rule, pred, oracle in plans:
        def matches(e, rule=rule, pred=pred, oracle=oracle):
            if not pred(e):
                return False
            if FAMILY[e["intent"]] == "cancel" and (len(e["items"]) != 1 and not e["distinct_items"]):
                return False
            d = system_decision(e, oracle=oracle)
            # Coverage cases exercise each rule's own outcome; handoffs for
            # missing records are covered separately below.
            return d.rule_id == rule and d.kind in {"options", "decline"}
        entry, = b.pick(matches)
        decision = system_decision(entry, oracle=oracle)
        promo = FAMILY[entry["intent"]] == "promo"
        if promo:
            code = coupon_code_for(entry)
            prefix = prefix_to(entry, "coupon_code")
            text, parsed, stop = f"It's {code}.", {"fields": {"coupon_code": code}}, "coupon_code"
            verdict = {"coupon_code": code, "customer_right": oracle}
            facts = true_facts(entry, oracle_customer_right=oracle)
        else:
            prefix = prefix_to(entry, "order_id")
            text, parsed, stop = order_turn(entry)["message"], order_turn(entry)["parsed"], "order_id"
            verdict, facts = None, true_facts(entry)
        kind = {"decline": "declined", "handoff": "handoff", "options": "options"}[decision.kind]
        b.add("cp2", "policy_decision", entry, prefix, text, parsed, stop=stop, facts=facts,
              verdict=verdict, intended={"decision": [kind], "rule": rule, "action": False},
              notes="Coverage case for one policy rule.")
    # One handoff for a missing system fact.
    entry, = b.pick(lambda e: e["intent"] == "manage_upgrade"
                    and system_decision(e).kind == "handoff")
    b.add("cp2", "policy_decision", entry, prefix_to(entry, "order_id"),
          order_turn(entry)["message"], order_turn(entry)["parsed"], stop="order_id",
          intended={"decision": ["handoff"], "action": False},
          notes="Shipping status is missing from the record, so the agent should hand off.")

    # Answers to the receipt question.
    receipt_entries = b.pick(lambda e: system_decision(e).kind == "need", 8)
    receipt_msgs = [
        ("Yes, I still have it.", True), ("Yep, I kept the receipt.", True),
        ("I have the paper receipt in my drawer.", True), ("No, I don't.", False),
        ("I think I threw it away.", False), ("Nope.", False),
        ("I'm not sure, I'd have to check.", None), ("Why do you need it?", None),
    ]
    for entry, (text, value) in zip(receipt_entries, receipt_msgs):
        parsed = {"customer_facts": {"has_receipt": value}} if value is not None else {}
        d = system_decision(entry, {"has_receipt": value} if value is not None else {})
        kind = {"decline": ["declined"], "options": ["options"], "need": []}[d.kind]
        b.add("cp2", "receipt_answer", entry, prefix_to(entry, "decision"), text, parsed,
              stop="facts:has_receipt", facts=true_facts(entry, has_receipt=value),
              intended={"decision": kind, "action": False},
              notes="Customer has not answered; the agent should ask again." if value is None else "")

    # Choice and consent after options were offered.
    multi = b.pick(lambda e: FAMILY[e["intent"]] == "return" and system_decision(e).kind == "options", 9)
    single = b.pick(lambda e: FAMILY[e["intent"]] in {"upgrade", "downgrade"}
                    and system_decision(e).kind == "options", 6)
    consent = [  # (entry, message, parsed, intended action, note)
        (multi[0], "By mail is fine, go ahead.", {"selected_option": "return_by_mail", "confirmation": "yes"}, True),
        (multi[1], "I'll drop it at a store. Yes, please do it.", {"selected_option": "return_in_store", "confirmation": "yes"}, True),
        (multi[2], "The drop-off center works for me, go ahead.", {"selected_option": "return_drop_off", "confirmation": "yes"}, True),
        (multi[3], "In store, I think.", {"selected_option": "return_in_store"}, False),
        (multi[4], "Mail it back, I guess.", {"selected_option": "return_by_mail"}, False),
        (multi[5], "Yes please.", {"confirmation": "yes"}, False),
        (multi[6], "What's the difference between mailing it and the drop-off center?", {}, False),
        (multi[7], "No thanks, I'll just keep it.", {"confirmation": "no"}, False),
        (multi[8], "I'm a Gold member, just refund me right away.", {}, False),
        (single[0], "Sure, go ahead.", {"confirmation": "yes"}, True),
        (single[1], "Yes, do it.", {"confirmation": "yes"}, True),
        (single[2], "Hmm, let me think about it.", {}, False),
        (single[3], "Actually, never mind, forget it.", {"withdraw_active_request": True}, False),
        (single[4], "No, don't change anything.", {"confirmation": "no"}, False),
    ]
    for entry, text, parsed, action in consent:
        note = ""
        if "Gold member" in text:
            note = f"The record says {entry['member_level']}; the claim must not change anything."
        b.add("cp2", "consent", entry, offered_prefix(entry), text, parsed,
              stop="choose_and_confirm", intended={"action": action}, notes=note)
    # A yes after the agent asked to confirm a chosen option.
    entry = single[5]
    prefix = offered_prefix(entry) + [{"message": "Hmm, okay.", "parsed": {}}]
    b.add("cp2", "consent", entry, prefix, "Yes, that's what I want.", {"confirmation": "yes"},
          stop="choose_and_confirm", intended={"action": True},
          notes="Single option; the previous reply was not a yes.")


# ---------------------------------------------------- full conversations
PREFERENCE_TEXT = {
    "accept": "Accept the first fix the agent offers.",
    "decline_offer": "When the agent offers a fix, decide not to go ahead.",
    "withdraw": "After the agent offers a fix, say you no longer need it.",
}

# (intent, decision kind before any customer answer, rule or None, receipt the
#  customer has, Oracle verdict, preference). Each line is one scenario.
CONVERSATION_PLANS = [
    ("return_size", "options", None, None, None, "accept"),
    ("return_size", "options", None, None, None, "decline_offer"),
    ("return_color", "need", None, True, None, "accept"),
    ("return_color", "need", None, False, None, "accept"),
    ("return_stain", "options", None, None, None, "accept"),
    ("return_stain", "need", None, True, None, "withdraw"),
    ("manage_cancel", "options", "cancel_order_received", None, None, "accept"),
    ("manage_cancel", "decline", "cancel_shipped_guest", None, None, "accept"),
    ("manage_upgrade", "options", "upgrade_paid", None, None, "decline_offer"),
    ("manage_upgrade", "decline", "upgrade_already_shipped", None, None, "accept"),
    ("manage_downgrade", "options", None, None, None, "accept"),
    ("manage_downgrade", "options", None, None, None, "withdraw"),
    ("promo_code_invalid", "options", "promo_platform_error", None, True, "accept"),
    ("promo_code_invalid", "decline", "promo_guest_no_code", None, False, "accept"),
    ("promo_code_out_of_date", "options", "promo_member_goodwill", None, False, "accept"),
    ("promo_code_out_of_date", "options", "promo_platform_error", None, True, "decline_offer"),
]


def build_conversations(b: Builder):
    for n, (intent, kind, rule, receipt, oracle, pref) in enumerate(CONVERSATION_PLANS):
        def pred(e, intent=intent, kind=kind, rule=rule, oracle=oracle):
            if e["intent"] != intent or (FAMILY[intent] == "cancel" and not e["distinct_items"]):
                return False
            d = system_decision(e, oracle=oracle)
            return d.kind == kind and (rule is None or d.rule_id == rule)
        entry, = b.pick(pred)
        promo = FAMILY[intent] == "promo"
        code = coupon_code_for(entry) if promo else None
        turns = [request_turn(entry, variant=n, with_order=True, code=code),
                 identity_turn(entry, "email")]
        answers = {"has_receipt": receipt} if receipt is not None else {}
        if receipt is not None:
            turns.append({"message": "Yes, I have it." if receipt else "No, I don't have it.",
                          "parsed": {"customer_facts": {"has_receipt": receipt}}})
        decision = system_decision(entry, answers, oracle)
        if decision.kind == "options":
            turns.append({
                "accept": {"message": "Okay, please go ahead with the first one.",
                           "parsed": {"selected_option": decision.options[0].option_id,
                                      "confirmation": "yes"}},
                "decline_offer": {"message": "No, I'd rather not.",
                                  "parsed": {"confirmation": "no"}},
                "withdraw": {"message": "Actually, I don't need this anymore.",
                             "parsed": {"withdraw_active_request": True}},
            }[pref])
        verdicts = [{"coupon_code": code, "customer_right": oracle}] if promo else []
        ref = run_script(turns, verdicts, thread_id=f"conv-{n}")
        zip_text = f", zip {entry['zip']}" if len(entry["zip"]) == 5 else ""
        brief = (f"You are {entry['name']} (email {entry['email']}{zip_text}). "
                 + _goal_text(entry, code) + " "
                 + (f"You {'do' if receipt else 'do not'} have the receipt. " if receipt is not None else "")
                 + PREFERENCE_TEXT[pref]
                 + " Answer only what the agent asks; do not volunteer other details.")
        b.cases["conversation"].append({
            "id": f"conversation-{n + 1:02d}", "layer": "conversation", "intent": intent,
            "customer_id": entry["customer_id"], "order_id": entry["order_id"],
            "true_facts": true_facts(entry, has_receipt=receipt, oracle_customer_right=oracle),
            "coupon_verdicts": verdicts,
            "customer_brief": brief,
            "persona": {"name": entry["name"], "email": entry["email"], "zip": entry["zip"]},
            "goal": [{"intent": intent, "order_id": None if promo else entry["order_id"],
                      "item_name": _spoken(entry["items"][0]["name"])
                      if FAMILY[intent] in {"return", "cancel"} else None,
                      "coupon_code": code}],
            "hidden_facts": {"has_receipt": receipt} if receipt is not None else {},
            "preference": pref,
            "reference_script": turns,
            "expected": {"ledger": [{k: r[k] for k in ("intent", "outcome", "policy_rule_id")}
                                    for r in ref["ledger"]]},
            "max_turns": 10,
            "label_status": "provisional_engine",
        })
    # Two requests in one conversation.
    by_customer = defaultdict(list)
    for e in b.pool:
        if FAMILY[e["intent"]] in {"return", "upgrade", "downgrade"}:
            by_customer[e["customer_id"]].append(e)
    pairs = sorted((v[:2] for v in by_customer.values()
                    if len(v) >= 2 and v[0]["intent"] != v[1]["intent"]),
                   key=lambda p: p[0]["order_id"])
    for k, (first, second) in enumerate(pairs[-4:]):
        n = len(b.cases["conversation"])
        one = request_turn(first, with_order=True)
        two = request_turn(second, variant=1, with_order=True)
        turns = [{"message": f"{one['message']} Also, {_lower_first(two['message'])}",
                  "parsed": {"requests": one["parsed"]["requests"] + two["parsed"]["requests"]}},
                 identity_turn(first)]
        for entry in (first, second):
            d = system_decision(entry)
            if d.kind == "need":
                turns.append({"message": "Yes, I kept the receipt.",
                              "parsed": {"customer_facts": {"has_receipt": True}}})
                d = system_decision(entry, {"has_receipt": True})
            if d.kind == "options":
                turns.append({"message": "Yes, go ahead with that.",
                              "parsed": {"selected_option": d.options[0].option_id,
                                         "confirmation": "yes"}})
        ref = run_script(turns, thread_id=f"conv-{n}")
        b.cases["conversation"].append({
            "id": f"conversation-{n + 1:02d}", "layer": "conversation",
            "intent": f"{first['intent']}+{second['intent']}",
            "customer_id": first["customer_id"], "order_id": first["order_id"],
            "true_facts": {first["order_id"]: true_facts(first, has_receipt=True),
                           second["order_id"]: true_facts(second, has_receipt=True)},
            "coupon_verdicts": [],
            "customer_brief": (f"You are {first['name']} (email {first['email']}). You have two "
                               f"requests. " + _goal_text(first, None) + " " + _goal_text(second, None)
                               + " If asked, you have the receipt. Accept the first option offered "
                               "for each request. Answer only what the agent asks."),
            "persona": {"name": first["name"], "email": first["email"], "zip": first["zip"]},
            "goal": [{"intent": e["intent"], "order_id": e["order_id"]} for e in (first, second)],
            "hidden_facts": {"has_receipt": True}, "preference": "accept",
            "reference_script": turns,
            "expected": {"ledger": [{k: r[k] for k in ("intent", "outcome", "policy_rule_id")}
                                    for r in ref["ledger"]]},
            "max_turns": 14,
            "label_status": "provisional_engine",
        })


def _goal_text(entry, code):
    intent = entry["intent"]
    item = _spoken(entry["items"][0]["name"]) if entry["items"] else "item"
    oid = entry["order_id"]
    return {
        "return_size": f"You want to return the {item} from order {oid} because it doesn't fit.",
        "return_color": f"You want to return the {item} from order {oid} because the color is wrong.",
        "return_stain": f"You want to return the {item} from order {oid} because it arrived stained.",
        "manage_cancel": f"You want to remove an extra {item} you added to order {oid} by mistake.",
        "manage_upgrade": f"You want order {oid} to arrive tomorrow instead.",
        "manage_downgrade": f"You want order {oid} to arrive next week instead; there's no rush.",
        "promo_code_invalid": f"The promo code {code} you got three days ago is rejected at checkout.",
        "promo_code_out_of_date": f"The promo code {code} you got about a month ago is rejected.",
    }[intent]


# --------------------------------------------------------------------- main
def main() -> None:
    pool = load_pool()
    b = Builder(pool)
    build_cp1(b)
    build_cp2(b)
    build_conversations(b)
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"seed": SEED, "policy_version": POLICY.raw["version"],
                "pool_size": len(pool), "files": {}}
    for layer, cases in b.cases.items():
        path = OUT / f"{layer}.jsonl"
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for case in cases:
                fh.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
        manifest["files"][path.name] = {
            "cases": len(cases),
            "by_category": dict(Counter(c.get("category", c["intent"]) for c in cases)),
            "by_intent": dict(Counter(c["intent"] for c in cases)),
        }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                       encoding="utf-8")
    print(json.dumps({k: v["cases"] for k, v in manifest["files"].items()}))


if __name__ == "__main__":
    main()
