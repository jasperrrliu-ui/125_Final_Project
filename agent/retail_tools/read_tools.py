"""Two read-only business tools for the retail-agent demo.

These functions return facts. They do not classify intent, apply policy, or
authenticate a real person. The LangGraph workflow decides when to call
them and stores their results in conversation state.
"""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3


DEFAULT_DB = (
    Path(__file__).resolve().parents[2]
    / "processed_data"
    / "customer_simulation"
    / "synthetic_retail.sqlite"
)

IDENTIFIER_COLUMNS = {
    "account_id": "account_id",
    "full_name": "customer_name",
    "username": "username",
    "email": "email",
}


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def lookup_customer(identifier_type: str, identifier_value: str,
                    db_path: Path = DEFAULT_DB) -> dict:
    """Find a candidate account by one customer-provided identifier."""
    if identifier_type not in IDENTIFIER_COLUMNS:
        raise ValueError("identifier_type must be account_id, full_name, username, or email")
    if not isinstance(identifier_value, str) or not identifier_value.strip():
        raise ValueError("identifier_value must be non-empty")
    value = " ".join(identifier_value.strip().split())
    column = IDENTIFIER_COLUMNS[identifier_type]
    # The column name comes only from the fixed mapping above. The value is bound.
    with closing(_open_read_only(db_path)) as db:
        rows = db.execute(
            f"SELECT customer_id, member_level FROM customers "
            f"WHERE lower({column})=lower(?) LIMIT 2",
            (value,),
        ).fetchall()
    if not rows:
        return {"status": "not_found"}
    if len(rows) > 1:
        return {"status": "multiple_candidates"}
    return {
        "status": "found",
        "customer_id": rows[0]["customer_id"],
        "member_level": rows[0]["member_level"],
    }


VERIFY_FIELDS = {"email", "full_name", "zip_code", "account_id"}


def verify_customer(claims: dict[str, str], db_path: Path = DEFAULT_DB) -> dict:
    """Verify a customer from the identity fields of one policy method.

    Every supplied field must match the same account. ``zip_code`` matches
    when any of the customer's orders uses that zip code. The caller decides
    which fields are sufficient (policy identity methods); this function only
    checks that the supplied fields agree with one account.
    """
    fields = {k: " ".join(str(v).strip().split()) for k, v in claims.items()
              if k in VERIFY_FIELDS and isinstance(v, str) and v.strip()}
    if not fields:
        raise ValueError("at least one identity field is required")
    clauses, params = [], []
    for key, value in fields.items():
        if key == "zip_code":
            clauses.append("EXISTS (SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id "
                           "AND o.zip_code=?)")
        else:
            clauses.append(f"lower(c.{IDENTIFIER_COLUMNS[key]})=lower(?)")
        params.append(value)
    with closing(_open_read_only(db_path)) as db:
        rows = db.execute(
            "SELECT c.customer_id, c.member_level FROM customers c WHERE "
            + " AND ".join(clauses) + " LIMIT 2",
            params,
        ).fetchall()
    if not rows:
        return {"status": "not_found"}
    if len(rows) > 1:
        return {"status": "multiple_candidates"}
    return {"status": "found", "customer_id": rows[0]["customer_id"],
            "member_level": rows[0]["member_level"]}


def lookup_order(customer_id: str, order_id: str,
                 db_path: Path = DEFAULT_DB) -> dict:
    """Return one customer's order and items, without exposing other orders."""
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id must be non-empty")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id must be non-empty")
    with closing(_open_read_only(db_path)) as db:
        order = db.execute(
            "SELECT order_id, purchase_date, shipping_status, packaging, "
            "street_address, city, state, zip_code "
            "FROM orders WHERE customer_id=? AND order_id=?",
            (customer_id.strip(), order_id.strip()),
        ).fetchone()
        if order is None:
            # Deliberately identical for an unknown order and another customer's order.
            return {"status": "not_found"}
        rows = db.execute(
            "SELECT item_index, name, amount FROM items "
            "WHERE order_id=? ORDER BY item_index",
            (order_id.strip(),),
        ).fetchall()
    items = [
        {
            "item_index": row["item_index"],
            "name": row["name"],
            "amount_cents": (
                int(Decimal(str(row["amount"])) * 100)
                if row["amount"] is not None else None
            ),
        }
        for row in rows
    ]
    facts = {
        "order_id": order["order_id"],
        "purchase_date": order["purchase_date"],
        "shipping_status": order["shipping_status"],
        "packaging": order["packaging"],
        "street_address": order["street_address"],
        "city": order["city"],
        "state": order["state"],
        "zip_code": order["zip_code"],
        "items": items,
    }
    missing = [key for key in ("purchase_date", "shipping_status", "packaging")
               if facts[key] is None]
    return {"status": "found", "facts": facts, "missing_facts": missing}
