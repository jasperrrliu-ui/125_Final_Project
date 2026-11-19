"""Deterministic policy engine for the retail agent.

The engine is the only component that interprets the policy file. The graph
asks it what is still needed and what may be done; the policy gate asks it
again, with freshly re-read records, before anything is written to the
ledger. The LLM never decides eligibility, amounts, or payloads.

Evaluation model
----------------
* A fact has one of three states: known (value + source), pending (not asked
  or looked up yet), or unavailable (every allowed source was tried).
* Conditions use three-valued logic: TRUE, FALSE, or UNKNOWN. ``any`` is TRUE
  as soon as one branch is TRUE, so the customer is asked only for facts that
  can still change the result.
* Rules are ordered; the first TRUE rule wins. A rule that is UNKNOWN stops
  evaluation and returns the pending facts it needs. If it is UNKNOWN only
  because facts are unavailable, the rule's ``on_unavailable`` setting
  decides: ``skip`` moves to the next rule, ``handoff`` (default) stops.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import json
from pathlib import Path
from typing import Any, Literal


DEFAULT_POLICY_FILE = (
    Path(__file__).resolve().parents[2] / "processed_data" / "policy" / "policy_v1.json"
)

TRUE, FALSE, UNKNOWN = "true", "false", "unknown"


# ---------------------------------------------------------------- data types

@dataclass(frozen=True)
class Fact:
    name: str
    value: Any
    source: Literal["system", "customer"]


@dataclass(frozen=True)
class FactRequest:
    """A fact the engine needs before it can decide."""

    name: str
    source: Literal["system", "customer"]
    tool: str | None = None       # system tool family: customer, order, coupon_check
    question: str | None = None   # customer-facing question


@dataclass(frozen=True)
class Option:
    option_id: str
    rule_id: str
    action: str
    payload: dict[str, Any]
    summary: str
    requires_confirmation: bool = True


@dataclass(frozen=True)
class Decision:
    kind: Literal["need", "options", "decline", "handoff"]
    rule_id: str | None = None
    need: tuple[FactRequest, ...] = ()
    options: tuple[Option, ...] = ()
    message: str | None = None
    reason_code: str | None = None
    facts_used: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------- policy object

class Policy:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.policy_id = raw["policy_id"]
        self.version = raw["version"]
        self.as_of = date.fromisoformat(raw["as_of"])
        self.facts = raw["facts"]
        self.constants = raw["constants"]
        self.identity_methods = [list(m) for m in raw["identity"]["methods"]]
        self.identity_question = raw["identity"]["question"]
        self.option_templates = raw.get("option_templates", {})
        self.families = raw["families"]
        self.intent_family = {
            intent: family
            for family, intents in raw["intent_families"].items()
            for intent in intents
        }

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY_FILE) -> "Policy":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def intents(self) -> set[str]:
        return set(self.intent_family)

    def family(self, intent: str) -> str:
        if intent not in self.intent_family:
            raise ValueError(f"intent outside policy scope: {intent}")
        return self.intent_family[intent]

    def targets(self, intent: str) -> list[str]:
        return list(self.families[self.family(intent)]["targets"])

    def identity_method(self, claims: dict[str, str]) -> list[str] | None:
        """Return the first identity method whose fields the customer supplied."""
        for method in self.identity_methods:
            if all(isinstance(claims.get(f), str) and claims[f].strip() for f in method):
                return method
        return None

    def system_tools(self) -> set[str]:
        return {
            source.split(":", 1)[1]
            for spec in self.facts.values() for source in spec["sources"]
            if source.startswith("system:")
        }


# ------------------------------------------------------------ fact resolution

def resolve_facts(policy: Policy, system_records: dict[str, dict[str, Any] | None],
                  customer_facts: dict[str, Any]) -> dict[str, Any]:
    """Resolve each declared fact to Fact, FactRequest (pending), or None.

    ``system_records`` maps a system tool family (customer, order,
    coupon_check) to the record it returned. A missing key means the tool has
    not run yet. A record whose field is None means that source has no value,
    so the next allowed source is tried. None as the result means unavailable.
    """
    resolved: dict[str, Any] = {}
    for name, spec in policy.facts.items():
        result: Any = None
        for source in spec["sources"]:
            if source.startswith("system:"):
                tool = source.split(":", 1)[1]
                if tool not in system_records:
                    result = FactRequest(name, "system", tool=tool)
                    break
                record = system_records[tool] or {}
                value = record.get(name)
                if value is not None:
                    result = Fact(name, value, "system")
                    break
            elif source == "customer":
                value = customer_facts.get(name)
                if value is not None:
                    result = Fact(name, value, "customer")
                else:
                    result = FactRequest(name, "customer", question=spec.get("question"))
                break
            else:
                raise ValueError(f"unknown fact source {source!r} for {name}")
        resolved[name] = result
    return resolved


# ------------------------------------------------------- condition evaluation

def _norm(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def _months_before(day: date, months: int) -> date:
    month_index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(month_index, 12)
    month += 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    raise ValueError("cannot compute month offset")


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


class _Eval:
    def __init__(self, policy: Policy, facts: dict[str, Any]):
        self.policy = policy
        self.facts = facts
        self.pending: dict[str, FactRequest] = {}
        self.unavailable: set[str] = set()
        self.used: dict[str, Fact] = {}

    def run(self, cond: dict[str, Any]) -> str:
        if "all" in cond:
            results = [self.run(part) for part in cond["all"]]
            if FALSE in results:
                return FALSE
            return TRUE if all(r == TRUE for r in results) else UNKNOWN
        if "any" in cond:
            results = [self.run(part) for part in cond["any"]]
            if TRUE in results:
                return TRUE
            return FALSE if all(r == FALSE for r in results) else UNKNOWN
        if "not" in cond:
            inner = self.run(cond["not"])
            return {TRUE: FALSE, FALSE: TRUE}.get(inner, UNKNOWN)
        return self._leaf(cond)

    def _leaf(self, cond: dict[str, Any]) -> str:
        name = cond["fact"]
        if name not in self.policy.facts:
            raise ValueError(f"policy condition uses undeclared fact {name!r}")
        state = self.facts.get(name)
        if isinstance(state, FactRequest):
            self.pending.setdefault(name, state)
            return UNKNOWN
        if state is None:
            self.unavailable.add(name)
            return UNKNOWN
        self.used[name] = state
        value = state.value
        if "eq" in cond:
            return TRUE if _norm(value) == _norm(cond["eq"]) else FALSE
        if "in" in cond or "in_constant" in cond:
            choices = cond.get("in") or self.policy.constants[cond["in_constant"]]
            return TRUE if _norm(value) in {_norm(c) for c in choices} else FALSE
        if "within_days" in cond or "within_months" in cond:
            day = _parse_date(value)
            if day is None:
                self.unavailable.add(name)
                return UNKNOWN
            as_of = self.policy.as_of
            if "within_days" in cond:
                earliest = date.fromordinal(as_of.toordinal() - int(cond["within_days"]))
            else:
                earliest = _months_before(as_of, int(cond["within_months"]))
            return TRUE if earliest <= day <= as_of else FALSE
        raise ValueError(f"unsupported condition: {cond}")


# ------------------------------------------------------------ option rendering

def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _render_value(template: Any, context: dict[str, Any]) -> Any:
    if isinstance(template, str) and template.startswith("$"):
        scope, _, key = template[1:].partition(".")
        if scope == "const":
            return context["const"][key]
        if scope == "item":
            item = context.get("item")
            if not item or item.get(key) is None:
                raise ValueError(f"option needs item field {key!r}")
            return item[key]
        raise ValueError(f"unknown payload reference {template}")
    return template


def _text_context(policy: Policy, context: dict[str, Any],
                  facts: dict[str, Any]) -> dict[str, Any]:
    text: dict[str, Any] = {}
    for key, value in policy.constants.items():
        text[key] = value
        if key.endswith("_cents") and isinstance(value, int):
            text[key[:-len("_cents")]] = _money(value)
    for name, state in facts.items():
        if isinstance(state, Fact):
            text[name] = state.value
    item = context.get("item") or {}
    text["item_name"] = str(item.get("name", "item")).replace("_", " ")
    if item.get("amount_cents") is not None:
        text["amount"] = _money(item["amount_cents"])
    text["address"] = context.get("address") or "the address on your order"
    return text


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_options(policy: Policy, rule: dict[str, Any], context: dict[str, Any],
                    facts: dict[str, Any]) -> tuple[Option, ...]:
    outcome = rule["outcome"]
    templates = (policy.option_templates[outcome["options_from"]]
                 if "options_from" in outcome else outcome["options"])
    ctx = {"const": policy.constants, **context}
    text = _SafeDict(_text_context(policy, context, facts))
    options = []
    for template in templates:
        payload = {k: _render_value(v, ctx) for k, v in template["payload"].items()}
        options.append(Option(
            option_id=template["option_id"],
            rule_id=rule["id"],
            action=template["action"],
            payload=payload,
            summary=template["summary"].format_map(text),
            requires_confirmation=template.get("requires_confirmation", True),
        ))
    return tuple(options)


# ------------------------------------------------------------------ evaluate

def evaluate(policy: Policy, intent: str, facts: dict[str, Any],
             context: dict[str, Any] | None = None) -> Decision:
    """Decide for one request from resolved facts.

    ``context`` carries request targets that options need, such as the
    selected ``item`` (item_index, name, amount_cents) and the order
    ``address``. These come from system records, never from the model.
    """
    context = context or {}
    family = policy.families[policy.family(intent)]
    for rule in family["rules"]:
        ev = _Eval(policy, facts)
        result = ev.run(rule["when"])
        used = {n: {"value": f.value, "source": f.source} for n, f in ev.used.items()}
        if result == FALSE:
            continue
        if result == UNKNOWN:
            if ev.pending:
                # Ask in the order facts are declared in the policy.
                ordered = [ev.pending[n] for n in policy.facts if n in ev.pending]
                return Decision("need", rule_id=rule["id"], need=tuple(ordered),
                                facts_used=used)
            if rule.get("on_unavailable", "handoff") == "skip":
                continue
            return Decision("handoff", rule_id=rule["id"], facts_used=used,
                            reason_code="fact_unavailable:" + ",".join(sorted(ev.unavailable)))
        outcome = rule["outcome"]
        if outcome["type"] == "options":
            return Decision("options", rule_id=rule["id"], facts_used=used,
                            options=_render_options(policy, rule, context, facts))
        if outcome["type"] == "decline":
            text = _SafeDict(_text_context(policy, context, facts))
            return Decision("decline", rule_id=rule["id"], facts_used=used,
                            reason_code=outcome.get("reason_code"),
                            message=outcome["message"].format_map(text))
        if outcome["type"] == "handoff":
            return Decision("handoff", rule_id=rule["id"], facts_used=used,
                            reason_code=outcome.get("reason_code"))
        raise ValueError(f"unknown outcome type in rule {rule['id']}")
    return Decision("handoff", reason_code="no_rule_matched")
