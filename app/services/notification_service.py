from __future__ import annotations

from typing import Any

try:
    import requests
except ModuleNotFoundError:
    requests = None  # type: ignore[assignment]

from app.config import settings


class NotificationService:
    """Send operational alerts when credentials are configured."""

    def enabled(self) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    def send(self, message: str) -> dict[str, Any]:
        if not self.enabled():
            return {"status": "skipped", "reason": "telegram is not configured"}
        if requests is None:
            return {"status": "skipped", "reason": "requests is not installed"}
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        try:
            response = requests.post(
                url,
                json={"chat_id": settings.telegram_chat_id, "text": message},
                timeout=8,
            )
            response.raise_for_status()
            return {"status": "sent"}
        except Exception as exc:  # noqa: BLE001 - requests/provider boundary
            return {"status": "error", "error_type": type(exc).__name__}
