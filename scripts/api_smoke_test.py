from __future__ import annotations

import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"

CHECKS = [
    ("GET", "/health", 2.0),
    ("GET", "/db/health", 2.0),
    ("GET", "/kite/websocket/status", 2.0),
    ("GET", "/automation/status", 2.0),
    ("GET", "/auto-trader/status", 2.0),
    ("GET", "/scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3", 3.0),
    ("GET", "/scanner/diagnostics?side=BUY&symbols=BANKNIFTY&limit=3", 10.0),
]


def check(method: str, path: str, timeout: float) -> dict[str, object]:
    url = f"{BASE_URL}{path}"
    started = time.perf_counter()
    try:
        with urlopen(url, timeout=timeout) as response:
            body = response.read(5000)
            elapsed = time.perf_counter() - started
            payload = {}
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {"raw": body.decode("utf-8", errors="replace")[:200]}
            return {
                "method": method,
                "path": path,
                "status": response.status,
                "seconds": round(elapsed, 3),
                "ok": response.status < 500 and elapsed <= timeout,
                "summary": payload.get("status")
                or payload.get("reason")
                or payload.get("message")
                or payload.get("count"),
            }
    except HTTPError as exc:
        return {
            "method": method,
            "path": path,
            "status": exc.code,
            "seconds": round(time.perf_counter() - started, 3),
            "ok": False,
            "summary": str(exc),
        }
    except URLError as exc:
        return {
            "method": method,
            "path": path,
            "status": "error",
            "seconds": round(time.perf_counter() - started, 3),
            "ok": False,
            "summary": str(exc.reason),
        }
    except TimeoutError:
        return {
            "method": method,
            "path": path,
            "status": "timeout",
            "seconds": round(time.perf_counter() - started, 3),
            "ok": False,
            "summary": f">{timeout}s",
        }


def main() -> int:
    rows = [check(method, path, timeout) for method, path, timeout in CHECKS]
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
