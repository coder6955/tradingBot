from __future__ import annotations

import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

try:
    import requests
except ModuleNotFoundError:
    requests = None  # type: ignore[assignment]

from app.config import settings


class NotificationService:
    """Send operational alerts when credentials are configured."""

    def __init__(self) -> None:
        self._status_lock = threading.Lock()
        self._last_attempt: dict[str, Any] | None = None

    def enabled(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            last_attempt = (
                dict(self._last_attempt) if self._last_attempt is not None else None
            )
        return {
            "status": "configured" if self.enabled() else "not_configured",
            "configured": self.enabled(),
            "provider": "telegram",
            "last_attempt": last_attempt,
        }

    def send(self, message: str, *, kind: str = "operational") -> dict[str, Any]:
        attempted_at = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
        if not self.enabled():
            return self._record_attempt(
                {
                    "status": "skipped",
                    "reason": "telegram is not configured",
                    "kind": kind,
                    "attempted_at": attempted_at,
                }
            )
        if requests is None:
            return self._record_attempt(
                {
                    "status": "skipped",
                    "reason": "requests is not installed",
                    "kind": kind,
                    "attempted_at": attempted_at,
                }
            )
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        try:
            response = requests.post(
                url,
                json={"chat_id": settings.telegram_chat_id, "text": message},
                timeout=8,
            )
            response.raise_for_status()
            return self._record_attempt(
                {"status": "sent", "kind": kind, "attempted_at": attempted_at}
            )
        except Exception as exc:  # noqa: BLE001 - requests/provider boundary
            return self._record_attempt(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "kind": kind,
                    "attempted_at": attempted_at,
                }
            )

    def _record_attempt(self, result: dict[str, Any]) -> dict[str, Any]:
        safe_result = dict(result)
        with self._status_lock:
            self._last_attempt = safe_result
        return safe_result
