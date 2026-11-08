"""Create the two local SQLite databases without requiring FastAPI."""

from pathlib import Path

from agent.oracle_demo.core import initialize_demo


folder = Path(__file__).resolve().parents[2] / "outputs" / "oracle_demo"
initialize_demo(folder / "merchant_coupons.sqlite", folder / "platform_oracle.sqlite")
print(folder)
