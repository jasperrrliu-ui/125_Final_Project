"""Render the human-readable policy document from the policy JSON.

The JSON is the single source of truth. Edit the JSON, then run:
  python scripts/render_policy_doc.py
which rewrites docs/POLICY_V1.md.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IN = ROOT / "processed_data" / "policy" / "policy_v1.json"
DEFAULT_OUT = ROOT / "docs" / "POLICY_V1.md"


def condition(cond: dict, constants: dict) -> str:
    if "all" in cond:
        return " AND ".join(_wrap(c, constants) for c in cond["all"])
    if "any" in cond:
        return " OR ".join(_wrap(c, constants) for c in cond["any"])
    if "not" in cond:
        return "NOT " + _wrap(cond["not"], constants)
    fact = f"`{cond['fact']}`"
    if "eq" in cond:
        return f"{fact} = {json.dumps(cond['eq'])}"
    if "in" in cond:
        return f"{fact} in {cond['in']}"
    if "in_constant" in cond:
        return f"{fact} in {cond['in_constant']} {constants[cond['in_constant']]}"
    if "within_days" in cond:
        return f"{fact} within {cond['within_days']} days of as_of"
    if "within_months" in cond:
        return f"{fact} within {cond['within_months']} months of as_of"
    return json.dumps(cond)


def _wrap(cond: dict, constants: dict) -> str:
    text = condition(cond, constants)
    return f"({text})" if ("all" in cond or "any" in cond) else text


def outcome(rule: dict, policy: dict) -> str:
    out = rule["outcome"]
    if out["type"] == "options":
        options = (policy["option_templates"][out["options_from"]]
                   if "options_from" in out else out["options"])
        lines = [f"`{o['option_id']}` → `{o['action']}` {json.dumps(o['payload'])}"
                 for o in options]
        return "Offer, then require an explicit yes: " + "; ".join(lines)
    if out["type"] == "decline":
        return f"Decline (`{out.get('reason_code')}`): \"{out['message']}\""
    return f"Hand off (`{out.get('reason_code')}`)"


def render(policy: dict) -> str:
    c = policy["constants"]
    lines = [
        f"# Retail policy {policy['version']} ({policy['status']})",
        "",
        "Generated from `processed_data/policy/policy_v1.json` by "
        "`scripts/render_policy_doc.py`. Edit the JSON, not this file.",
        "",
        f"Evaluation clock (`as_of`): **{policy['as_of']}**. {policy['as_of_note']}",
        "",
        "Provenance tags: " + "; ".join(f"`{k}`: {v}" for k, v in policy["provenance_legend"].items()),
        "",
        "## General rules",
        "",
    ]
    for rule in policy["general_rules"]:
        lines.append(f"- **{rule['id']}**: {rule['text']} _({', '.join(rule['provenance'])})_")
    methods = " OR ".join(" + ".join(f"`{f}`" for f in m) for m in policy["identity"]["methods"])
    lines += ["", "## Identity", "", f"Accepted methods: {methods}.", "",
              "## Facts", "", "| Fact | Sources, in priority order | Question to customer |",
              "| --- | --- | --- |"]
    for name, spec in policy["facts"].items():
        lines.append(f"| `{name}` | {', '.join(spec['sources'])} | {spec.get('question', '')} |")
    lines += ["", "## Constants", ""]
    lines += [f"- `{k}`: {json.dumps(v)}" for k, v in c.items()]
    lines += ["", "## Rules by request family", "",
              "Rules are checked in order. The first rule that is true decides. If a rule "
              "cannot be decided yet, the agent gets the missing fact (system first, then "
              "the customer) and checks again. If a needed fact cannot be obtained, the "
              "request is handed off unless the rule says `on_unavailable: skip`."]
    for family, spec in policy["families"].items():
        intents = ", ".join(f"`{i}`" for i in policy["intent_families"][family])
        lines += ["", f"### {family}", "", f"Intents: {intents}. Required targets: "
                  + ", ".join(f"`{t}`" for t in spec["targets"]) + ".", ""]
        for i, rule in enumerate(spec["rules"], 1):
            lines.append(f"{i}. **{rule['id']}**")
            lines.append(f"   - When: {condition(rule['when'], c)}")
            if rule.get("on_unavailable"):
                lines.append(f"   - If a fact is unavailable: {rule['on_unavailable']}")
            lines.append(f"   - Then: {outcome(rule, policy)}")
            if rule.get("text"):
                lines.append(f"   - Note: {rule['text']}")
            lines.append(f"   - Provenance: {', '.join(rule['provenance'])}")
    return "\n".join(lines) + "\n"


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_IN
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    target.write_text(render(json.loads(source.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
