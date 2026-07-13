from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any

try:
    from kiteconnect.exceptions import TokenException
except Exception:  # pragma: no cover - optional dependency
    TokenException = None  # type: ignore


@dataclass
class KiteAuthSnapshot:
    status: str = "UNKNOWN"
    relogin_required: bool = False
    last_error: str | None = None


class KiteAuthState:
    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = KiteAuthSnapshot()

    def mark_auth_failed(self, message: str | None = None) -> None:
        with self._lock:
            self._snapshot = KiteAuthSnapshot(
                status="AUTH_FAILED",
                relogin_required=True,
                last_error=message or "kite_auth_failed",
            )

    def clear(self) -> None:
        with self._lock:
            self._snapshot = KiteAuthSnapshot(status="OK", relogin_required=False, last_error=None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot
            return {
                "status": snapshot.status,
                "relogin_required": snapshot.relogin_required,
                "last_error": snapshot.last_error,
            }

    @property
    def relogin_required(self) -> bool:
        with self._lock:
            return self._snapshot.relogin_required


kite_auth_state = KiteAuthState()


def is_kite_token_exception(exc: BaseException) -> bool:
    if TokenException is not None and isinstance(exc, TokenException):
        return True
    return exc.__class__.__name__ == "TokenException"
