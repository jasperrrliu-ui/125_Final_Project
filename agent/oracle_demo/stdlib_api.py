"""Dependency-free HTTP fallback for local end-to-end testing."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets

from agent.oracle_demo.core import CheckRequest, OracleService, initialize_demo


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "outputs" / "oracle_demo"


def make_server(host: str = "127.0.0.1", port: int = 8000,
                data_dir: Path | None = None, secret: bytes | None = None) -> ThreadingHTTPServer:
    folder = data_dir or Path(os.getenv("ORACLE_DEMO_DIR", str(DEFAULT_DATA_DIR)))
    merchant_db, platform_db = folder / "merchant_coupons.sqlite", folder / "platform_oracle.sqlite"
    initialize_demo(merchant_db, platform_db)
    key = secret or os.getenv("ORACLE_CACHE_SECRET", "").encode() or secrets.token_bytes(32)
    oracle = OracleService(merchant_db, platform_db, key)

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_json(200, {"status": "ok", "merchant_calls": oracle.merchant_call_count()})
            else:
                self.send_json(404, {"detail": "not found"})

        def do_POST(self) -> None:
            if self.path != "/oracle/check":
                self.send_json(404, {"detail": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 16384:
                    self.send_json(413, {"detail": "request too large"})
                    return
                payload = json.loads(self.rfile.read(length))
                result = oracle.check(CheckRequest(**payload))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"detail": str(exc)})
                return
            self.send_json(200, result)

        def log_message(self, format: str, *args) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    server = make_server()
    print(f"Oracle demo listening on http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
