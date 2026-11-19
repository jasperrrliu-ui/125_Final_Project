"""Map tool results to the fact names declared in the policy.

Both the graph and the policy gate use these adapters, so a fact means the
same thing at proposal time and at authorization time.
"""

from __future__ import annotations

from typing import Any


def customer_record(member_level: str | None) -> dict[str, Any]:
    return {"member_level": member_level.casefold() if isinstance(member_level, str) else None}


def order_record(order_facts: dict[str, Any] | None) -> dict[str, Any]:
    """Convert lookup_order facts. Missing values stay None (no system value)."""
    facts = order_facts or {}
    packaging = facts.get("packaging")
    if isinstance(packaging, str) and packaging.strip().casefold() in {"yes", "no"}:
        original_packaging: bool | None = packaging.strip().casefold() == "yes"
    else:
        original_packaging = None
    return {
        "shipping_status": facts.get("shipping_status"),
        "purchase_date": facts.get("purchase_date"),
        "original_packaging": original_packaging,
    }


def coupon_record(coupon_result: dict[str, Any] | None) -> dict[str, Any]:
    """Only a resolved Oracle verdict counts; anything else is 'no value'."""
    result = coupon_result or {}
    right = result.get("customer_right")
    if result.get("status") == "resolved" and type(right) is bool:
        return {"oracle_customer_right": right}
    return {"oracle_customer_right": None}


def order_address(order_facts: dict[str, Any] | None) -> str | None:
    facts = order_facts or {}
    parts = [facts.get("street_address"), facts.get("city"), facts.get("state"), facts.get("zip_code")]
    if not all(isinstance(p, str) and p.strip() for p in parts):
        return None
    street, city, state, zip_code = (p.strip() for p in parts)
    return f"{street}, {city}, {state.upper()} {zip_code}"
