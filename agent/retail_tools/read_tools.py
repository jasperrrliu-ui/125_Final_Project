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


def lookup_order(customer_id: str, order_id: str,
                 db_path: Path = DEFAULT_DB) -> dict:
    """Return one customer's order and items, without exposing other orders."""
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id must be non-empty")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id must be non-empty")
    with closing(_open_read_only(db_path)) as db:
        order = db.execute(
            "SELECT order_id, purchase_date, shipping_status, packaging "
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
        "items": items,
    }
    missing = [key for key in ("purchase_date", "shipping_status", "packaging")
               if facts[key] is None]
    return {"status": "found", "facts": facts, "missing_facts": missing}
