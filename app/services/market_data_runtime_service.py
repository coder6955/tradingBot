from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.providers.token_store import load_access_token
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.banknifty_option_prewarm_service import BankNiftyOptionPrewarmService
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed


@dataclass(frozen=True)
class MarketDataRuntimeSnapshot:
    state: str
    reason: str | None
    websocket: dict[str, Any]
    banknifty_option_prewarm: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_data_state": self.state,
            "market_data_reason": self.reason,
            **self.websocket,
            "banknifty_option_prewarm": self.banknifty_option_prewarm,
        }


class MarketDataRuntimeService:
    """Supervise live market-data runtime state without owning trade logic."""

    def __init__(
        self,
        *,
        websocket_feed: KiteWebSocketPriceFeed,
        active_price_feed: ActiveTradePriceFeed,
        prewarm_service: BankNiftyOptionPrewarmService,
    ) -> None:
        self.websocket_feed = websocket_feed
        self.active_price_feed = active_price_feed
        self.prewarm_service = prewarm_service
        self.last_start_result: dict[str, Any] | None = None
        self.last_stop_result: dict[str, Any] | None = None

    def start(self) -> dict[str, Any]:
        self.last_start_result = self.active_price_feed.start()
        return dict(self.last_start_result)

    def stop(self) -> dict[str, Any]:
        self.last_stop_result = self.active_price_feed.stop()
        return dict(self.last_stop_result)

    def refresh_credentials(self, *, access_token: str | None = None, restart_if_enabled: bool = True) -> dict[str, Any]:
        self.websocket_feed.refresh_credentials(access_token=access_token)
        result: dict[str, Any] = {"credentials_refreshed": True, "restart_requested": False}
        if restart_if_enabled and settings.enable_kite_websocket:
            result["restart_requested"] = True
            result["start"] = self.start()
        return result

    def status(self) -> dict[str, Any]:
        websocket = self.active_price_feed.status()
        snapshot = MarketDataRuntimeSnapshot(
            state=self._normalized_state(websocket),
            reason=self._state_reason(websocket),
            websocket=websocket,
            banknifty_option_prewarm=self.prewarm_service.status(),
        )
        payload = snapshot.as_dict()
        payload["last_start_result"] = self.last_start_result
        payload["last_stop_result"] = self.last_stop_result
        return payload

    def control_status(self) -> dict[str, Any]:
        status = self.status()
        return {
            "state": status.get("market_data_state"),
            "reason": status.get("market_data_reason"),
            "websocket_status": status.get("websocket_status"),
            "running": status.get("running"),
            "connected": status.get("websocket_connected"),
            "market_session": status.get("market_session"),
            "duplicate_start_prevented_count": status.get("duplicate_start_prevented_count"),
            "reconnect_count": status.get("reconnect_count"),
            "disconnect_count": status.get("disconnect_count"),
            "last_error": status.get("last_error"),
        }

    def _normalized_state(self, websocket: dict[str, Any]) -> str:
        raw_status = str(websocket.get("websocket_status") or "").upper()
        connected = bool(websocket.get("websocket_connected"))
        running = bool(websocket.get("running"))
        session = str(websocket.get("market_session") or "")

        if not settings.enable_kite_websocket:
            return "DISABLED"
        if not settings.kite_api_key or not (self.websocket_feed.access_token or load_access_token() or settings.kite_access_token):
            return "AUTH_REQUIRED"
        if session != "REGULAR_MARKET" and not connected:
            return "MARKET_CLOSED"
        if raw_status == "AUTH_FAILED":
            return "AUTH_REQUIRED"
        if raw_status in {"CONNECTING", "RECONNECTING", "RECONNECT_COOLDOWN"}:
            return raw_status
        if raw_status in {"ERROR", "MAX_RETRIES_EXCEEDED"}:
            return "FAILED"
        if connected:
            max_tick_age = websocket.get("max_tick_age")
            has_tokens = bool(websocket.get("active_trade_tokens") or websocket.get("subscribed_tokens") or websocket.get("desired_tokens"))
            if has_tokens and isinstance(max_tick_age, (int, float)) and max_tick_age > settings.websocket_price_stale_seconds:
                return "STALE"
            return "CONNECTED"
        if running:
            return "CONNECTING"
        return "DISCONNECTED"

    def _state_reason(self, websocket: dict[str, Any]) -> str | None:
        state = self._normalized_state(websocket)
        if state == "DISABLED":
            return "websocket_disabled"
        if state == "AUTH_REQUIRED":
            return websocket.get("last_error") or "kite_login_required"
        if state == "MARKET_CLOSED":
            return str(websocket.get("market_session") or "market_closed").lower()
        if state == "STALE":
            return "websocket_tick_stale"
        if state == "FAILED":
            return websocket.get("last_error") or websocket.get("reconnect_skipped_reason") or "websocket_failed"
        return websocket.get("reconnect_skipped_reason") or websocket.get("last_reason")
