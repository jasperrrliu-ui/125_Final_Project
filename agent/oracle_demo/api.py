"""Optional FastAPI wrapper. Install requirements.txt to run the HTTP service."""

from __future__ import annotations

import os
from pathlib import Path
import secrets

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.oracle_demo.core import CheckRequest, OracleService, initialize_demo


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "outputs" / "oracle_demo"


class OracleInput(BaseModel):
    merchant_id: str
    customer_id: str
    coupon_code: str
    cart_total_cents: int = Field(ge=0)
    attempt_id: str | None = None
    as_of: str | None = None


def create_app(data_dir: Path | None = None, secret: bytes | None = None) -> FastAPI:
    folder = data_dir or Path(os.getenv("ORACLE_DEMO_DIR", str(DEFAULT_DATA_DIR)))
    merchant_db = folder / "merchant_coupons.sqlite"
    platform_db = folder / "platform_oracle.sqlite"
    initialize_demo(merchant_db, platform_db)
    # Local demo: ephemeral key if unset. Set env var for a persistent cache key.
    key = secret or os.getenv("ORACLE_CACHE_SECRET", "").encode() or secrets.token_bytes(32)
    oracle = OracleService(merchant_db, platform_db, key)
    app = FastAPI(title="Mock Marketplace Coupon Oracle", version="0.1.0")
    app.state.oracle = oracle

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "merchant_calls": oracle.merchant_call_count()}

    @app.post("/oracle/check")
    def check_coupon(payload: OracleInput) -> dict:
        try:
            return oracle.check(CheckRequest(**payload.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


app = create_app()
