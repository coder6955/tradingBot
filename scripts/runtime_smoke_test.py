from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    name: str
    path: str
    timeout_seconds: float


ENDPOINTS = [
    Endpoint("health", "/health", 2.0),
    Endpoint("db_health", "/db/health", 3.0),
    Endpoint("runtime_status", "/runtime/status", 3.0),
    Endpoint("websocket_status", "/kite/websocket/status", 3.0),
    Endpoint(
        "scanner_opportunities",
        "/scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3",
        5.0,
    ),
    Endpoint("after_market_status", "/research/after-market/status", 3.0),
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test runtime responsiveness for the option app."
    )
    parser.add_argument(
        "--base-url", default="http://localhost:8000", help="API base URL"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Override per-endpoint timeout in seconds",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    failures = 0
    for endpoint in ENDPOINTS:
        timeout = float(args.timeout or endpoint.timeout_seconds)
        result = call_endpoint(f"{base_url}{endpoint.path}", timeout)
        status = "PASS" if result["ok"] else "FAIL"
        if not result["ok"]:
            failures += 1
        print(
            f"{status} {endpoint.name} status={result['status_code']} "
            f"elapsed_ms={result['elapsed_ms']} reason={result['reason']}"
        )
    return 1 if failures else 0


def call_endpoint(url: str, timeout: float) -> dict[str, object]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4000).decode("utf-8", errors="replace")
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            payload = parse_json(body)
            ok = response.status < 500 and elapsed_ms <= timeout * 1000
            reason = payload.get("status") or payload.get("reason") or "ok"
            return {
                "ok": ok,
                "status_code": response.status,
                "elapsed_ms": elapsed_ms,
                "reason": str(reason),
            }
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return {
            "ok": False,
            "status_code": exc.code,
            "elapsed_ms": elapsed_ms,
            "reason": str(exc),
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return {
            "ok": False,
            "status_code": "error",
            "elapsed_ms": elapsed_ms,
            "reason": str(exc),
        }


def parse_json(body: str) -> dict[str, object]:
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


if __name__ == "__main__":
    sys.exit(main())
