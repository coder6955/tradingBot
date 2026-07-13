from __future__ import annotations

import logging
import json
import queue
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from threading import Event, RLock, Thread
from typing import Any, Callable

from app.config import settings
from app.providers.kite_auth_state import is_kite_token_exception, kite_auth_state
from app.providers.token_store import load_access_token
from app.services.database import Candle, get_session
from app.services.market_session_service import MarketSessionService
from app.services.time_utils import ist_now_naive

try:
    from kiteconnect import KiteTicker
except Exception:  # pragma: no cover - depends on local kiteconnect install
    KiteTicker = None  # type: ignore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebSocketTick:
    instrument_token: int
    price: float
    timestamp: datetime
    volume: float | None = None
    bid: float | None = None
    ask: float | None = None
    receive_timestamp: datetime | None = None
    timestamp_source: str = "local_receive_time"
    packet_type: str = "unknown"
    raw: dict[str, Any] | None = None


@dataclass
class WebSocketPremiumCandle:
    instrument_token: int
    timeframe: str
    timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float = 0.0
    tick_count: int = 0
    source: str = "websocket_builder"


class KiteWebSocketPriceFeed:
    """KiteTicker-backed tick store for active-trade monitoring only."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        access_token: str | None = None,
        ticker_factory: Callable[[str, str], Any] | None = None,
        order_update_handler: Callable[[dict[str, Any]], Any] | None = None,
        tick_handler: Callable[[WebSocketTick], Any] | None = None,
        gap_handler: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        market_session_service: MarketSessionService | None = None,
    ) -> None:
        self.api_key = api_key or settings.kite_api_key
        self.access_token = access_token or load_access_token() or settings.kite_access_token
        self.ticker_factory = ticker_factory
        self.order_update_handler = order_update_handler
        self.tick_handler = tick_handler
        self.gap_handler = gap_handler
        self.clock = clock or ist_now_naive
        self.market_session_service = market_session_service or MarketSessionService(clock=self.clock)
        self._ticker: Any | None = None
        self._ticks: dict[int, WebSocketTick] = {}
        self._subscribed_tokens: set[int] = set()
        self._desired_tokens: set[int] = set()
        self._subscription_errors: dict[int, str] = {}
        self._lock = RLock()
        self.running = False
        self.connected = False
        self.last_error: str | None = None
        self.reconnect_count = 0
        self.disconnect_count = 0
        self.subscription_failure_count = 0
        self.ignored_tick_count = 0
        self.order_update_count = 0
        self.text_message_count = 0
        self.connected_at: datetime | None = None
        self.last_disconnect_at: datetime | None = None
        self.last_reconnect_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_order_update: dict[str, Any] | None = None
        self.websocket_status = "DISCONNECTED"
        self.reconnect_skipped_reason: str | None = None
        self._tick_modes: dict[int, str] = {}
        self._premium_candles: dict[int, dict[datetime, WebSocketPremiumCandle]] = {}
        self._ticks_seen: dict[int, int] = {}
        self._rehydrated_tokens: set[int] = set()
        self._persisted_candle_writes = 0
        self._rehydrated_candle_count = 0
        self._last_candle_cleanup_date: date | None = None
        self._last_candle_cleanup_at: datetime | None = None
        self._last_candle_cleanup_deleted = 0
        self._total_candle_cleanup_deleted = 0
        self._gap_events: list[dict[str, Any]] = []
        self._active_gap_started_at: datetime | None = None
        self._gap_backfill_attempt_count = 0
        self._gap_backfill_success_count = 0
        self._gap_backfill_failure_count = 0
        self._start_stop_lock = RLock()
        self._event_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=max(1, settings.websocket_event_queue_size))
        self._event_worker: Thread | None = None
        self._event_stop = Event()
        self._candle_persist_queue: queue.Queue[tuple[int, str, datetime]] = queue.Queue(maxsize=max(1, settings.websocket_candle_persist_queue_size))
        self._candle_persist_worker: Thread | None = None
        self._candle_persist_stop = Event()
        self._pending_candle_persist: dict[tuple[int, str, datetime], WebSocketPremiumCandle] = {}
        self._queued_candle_persist_keys: set[tuple[int, str, datetime]] = set()
        self.event_queue_dropped_count = 0
        self.candle_persist_queue_dropped_count = 0
        self.duplicate_start_prevented_count = 0
        self.reconnect_request_count = 0
        self.last_error_code: int | None = None
        self.last_error_reason: str | None = None
        self.last_disconnect_reason: str | None = None
        self._last_auth_failed_access_token: str | None = None
        self._last_auth_failed_api_key: str | None = None
        self._reconnect_attempt_times: list[datetime] = []

    def start(self) -> dict[str, Any]:
        with self._start_stop_lock:
            self.cleanup_old_persisted_candles()
            self.refresh_credentials()
            if not settings.enable_kite_websocket:
                self.websocket_status = "DISABLED"
                return {"started": False, "reason": "websocket_disabled"}
            if kite_auth_state.relogin_required:
                self.running = False
                self.connected = False
                self.websocket_status = "AUTH_FAILED"
                self.reconnect_skipped_reason = "auth_failed"
                self.last_error = "kite_relogin_required"
                return {"started": False, "reason": "kite_relogin_required", "relogin_required": True}
            session = self.market_session()
            if session != "REGULAR_MARKET":
                self.running = False
                self.connected = False
                self.websocket_status = "DISABLED_OUTSIDE_MARKET_HOURS" if session == "WEEKEND" else "MARKET_CLOSED"
                self.reconnect_skipped_reason = "market_closed"
                self.last_error = None
                logger.info("Kite WebSocket not started outside market hours: %s", session)
                return {"started": False, "reason": "market_closed", "market_session": session}
            if self.running:
                self.duplicate_start_prevented_count += 1
                logger.info("Kite WebSocket duplicate start prevented")
                return {"started": True, "already_running": True, "duplicate_start_prevented": True}
            if not self.api_key or not self.access_token:
                self.last_error = "missing_kite_api_key_or_access_token"
                self.websocket_status = "AUTH_FAILED"
                kite_auth_state.mark_auth_failed(self.last_error)
                logger.warning("Kite WebSocket not started: %s", self.last_error)
                return {"started": False, "reason": self.last_error}
            factory = self.ticker_factory or KiteTicker
            if factory is None:
                self.last_error = "kite_ticker_unavailable"
                self.websocket_status = "ERROR"
                logger.warning("Kite WebSocket not started: kiteconnect.KiteTicker unavailable")
                return {"started": False, "reason": self.last_error}

            self._ensure_workers()
            self._ticker = factory(str(self.api_key), str(self.access_token))
            self._wire_callbacks(self._ticker)
            self.running = True
            self.websocket_status = "CONNECTING"
            try:
                self._ticker.connect(threaded=True)
            except TypeError:
                self._ticker.connect()
            except Exception as exc:
                self.running = False
                self.connected = False
                self.last_error = str(exc)
                self.last_error_reason = str(exc)
                self.websocket_status = "AUTH_FAILED" if self._is_auth_failure(None, str(exc), exc=exc) else "ERROR"
                self.reconnect_skipped_reason = "auth_failed" if self.websocket_status == "AUTH_FAILED" else None
                if self.websocket_status == "AUTH_FAILED":
                    self._mark_auth_failed(str(exc))
                self._stop_workers()
                logger.exception("Kite WebSocket connect failed")
                return {"started": False, "reason": self.last_error}
            logger.info("Kite WebSocket start requested")
            return {"started": True}

    def refresh_credentials(self, *, access_token: str | None = None) -> None:
        """Refresh credentials from the token store before creating a ticker."""
        self.api_key = settings.kite_api_key
        latest_token = access_token or load_access_token() or settings.kite_access_token
        token_changed = bool(latest_token and latest_token != self.access_token)
        api_key_changed = bool(self.api_key and self.api_key != self._last_auth_failed_api_key)
        if token_changed:
            logger.info("Kite WebSocket access token refreshed")
        self.access_token = latest_token
        if latest_token and self.websocket_status == "AUTH_FAILED" and (
            token_changed or api_key_changed or latest_token != self._last_auth_failed_access_token
        ):
            kite_auth_state.clear()
            self.websocket_status = "DISCONNECTED"
            self.last_error = None
            self.last_error_reason = None
            self.reconnect_skipped_reason = None
            self._last_auth_failed_access_token = None
            self._last_auth_failed_api_key = None

    def stop(self) -> dict[str, Any]:
        with self._start_stop_lock:
            self.running = False
            self.connected = False
            self.websocket_status = "STOPPED"
            ticker = self._ticker
            if ticker is not None:
                try:
                    close = getattr(ticker, "close", None) or getattr(ticker, "stop", None)
                    if close:
                        close()
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.warning("Kite WebSocket stop failed: %s", exc)
            self._stop_workers()
            return {"stopped": True}

    def subscribe(self, tokens: list[int] | set[int] | tuple[int, ...]) -> dict[str, Any]:
        clean_tokens = {int(token) for token in tokens if self._safe_int(token) is not None and int(token) > 0}
        with self._lock:
            self._desired_tokens.update(clean_tokens)
        self.cleanup_old_persisted_candles()
        self._rehydrate_premium_candles(clean_tokens)
        if not clean_tokens:
            return {"subscribed": [], "reason": "token_missing"}
        if not self.running and settings.enable_kite_websocket:
            start_result = self.start()
            if not start_result.get("started") and start_result.get("reason") == "market_closed":
                logger.info("Kite WebSocket subscription queued outside market hours: %s", sorted(clean_tokens))
                return {
                    "subscribed": [],
                    "queued": sorted(clean_tokens),
                    "reason": "market_closed",
                    "market_session": start_result.get("market_session"),
                }
        if not self.connected or self._ticker is None:
            logger.info("Kite WebSocket queued subscription while disconnected: %s", sorted(clean_tokens))
            return {"subscribed": [], "queued": sorted(clean_tokens), "reason": "websocket_disconnected"}
        return self._subscribe_connected(clean_tokens)

    def unsubscribe(self, tokens: list[int] | set[int] | tuple[int, ...]) -> dict[str, Any]:
        clean_tokens = {int(token) for token in tokens if self._safe_int(token) is not None and int(token) > 0}
        with self._lock:
            self._desired_tokens.difference_update(clean_tokens)
            self._subscribed_tokens.difference_update(clean_tokens)
        if self.connected and self._ticker is not None and clean_tokens:
            try:
                self._ticker.unsubscribe(list(clean_tokens))
                logger.info("Kite WebSocket unsubscribed tokens: %s", sorted(clean_tokens))
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning("Kite WebSocket unsubscribe failed: %s", exc)
                return {"unsubscribed": [], "reason": "subscription_failed", "message": str(exc)}
        return {"unsubscribed": sorted(clean_tokens)}

    def get_latest_price(self, instrument_token: int) -> float | None:
        tick = self.get_latest_tick(instrument_token)
        return tick.price if tick else None

    def get_latest_tick(self, instrument_token: int) -> WebSocketTick | None:
        with self._lock:
            return self._ticks.get(int(instrument_token))

    def is_fresh(self, instrument_token: int, max_age_seconds: int | None = None) -> bool:
        tick = self.get_latest_tick(instrument_token)
        if tick is None:
            return False
        max_age = max_age_seconds if max_age_seconds is not None else settings.websocket_price_stale_seconds
        return self._age_seconds(tick.timestamp) <= max_age

    def status(self, *, active_trade_tokens: set[int] | None = None, fallback_active: bool = False) -> dict[str, Any]:
        now = self._now()
        session = self.market_session(now)
        status_label = self.websocket_status
        if not settings.enable_kite_websocket:
            status_label = "DISABLED"
        elif session != "REGULAR_MARKET" and not self.connected:
            status_label = "DISABLED_OUTSIDE_MARKET_HOURS" if session == "WEEKEND" else "MARKET_CLOSED"
        with self._lock:
            latest_tick_age = {
                str(token): round(max(0.0, (now - tick.timestamp.replace(tzinfo=None)).total_seconds()), 3)
                for token, tick in self._ticks.items()
            }
            candle_counts = {str(token): len(self._current_session_candles_locked(token)) for token in set(self._premium_candles) | set(self._ticks)}
            return {
                "websocket_enabled": settings.enable_kite_websocket,
                "websocket_status": status_label,
                "relogin_required": kite_auth_state.relogin_required,
                "market_session": session,
                "websocket_connected": self.connected,
                "running": self.running,
                "subscribed_tokens": sorted(self._subscribed_tokens),
                "desired_tokens": sorted(self._desired_tokens),
                "latest_tick_age": latest_tick_age,
                "last_tick_timestamp": {
                    str(token): tick.timestamp.isoformat(sep=" ") for token, tick in self._ticks.items()
                },
                "tick_timestamp_source": {
                    str(token): tick.timestamp_source for token, tick in self._ticks.items()
                },
                "tick_packet_type": {
                    str(token): tick.packet_type for token, tick in self._ticks.items()
                },
                "tick_mode_per_token": {str(token): mode for token, mode in self._tick_modes.items()},
                "active_trade_tokens": sorted(active_trade_tokens or set()),
                "fallback_active": fallback_active,
                "reconnect_count": self.reconnect_count,
                "disconnect_count": self.disconnect_count,
                "connected_duration_seconds": self._connected_duration_seconds(),
                "max_tick_age": max(latest_tick_age.values()) if latest_tick_age else None,
                "subscription_failure_count": self.subscription_failure_count,
                "ignored_tick_count": self.ignored_tick_count,
                "ticks_seen_by_token": {str(token): count for token, count in self._ticks_seen.items()},
                "premium_candle_count_by_token": candle_counts,
                "premium_candle_builder": {
                    str(token): self._premium_candle_status_locked(token, now)
                    for token in sorted(set(self._premium_candles) | set(self._ticks) | set(active_trade_tokens or set()))
                },
                "candle_persistence": {
                    "enabled": settings.enable_websocket_candle_persistence,
                    "storage_prefix": settings.websocket_candle_storage_prefix,
                    "persisted_writes": self._persisted_candle_writes,
                    "rehydrated_tokens": sorted(self._rehydrated_tokens),
                    "rehydrated_candle_count": self._rehydrated_candle_count,
                    "daily_cleanup_enabled": settings.enable_websocket_candle_daily_cleanup,
                    "pending_coalesced_writes": len(self._pending_candle_persist),
                    "queued_coalesced_keys": len(self._queued_candle_persist_keys),
                    "last_cleanup_date": self._last_candle_cleanup_date.isoformat() if self._last_candle_cleanup_date else None,
                    "last_cleanup_at": self._last_candle_cleanup_at.isoformat(sep=" ") if self._last_candle_cleanup_at else None,
                    "last_cleanup_deleted": self._last_candle_cleanup_deleted,
                    "total_cleanup_deleted": self._total_candle_cleanup_deleted,
                },
                "data_gap": self._gap_status_locked(now),
                "order_update_count": self.order_update_count,
                "text_message_count": self.text_message_count,
                "last_disconnect_at": self.last_disconnect_at.isoformat(sep=" ") if self.last_disconnect_at else None,
                "last_reconnect_at": self.last_reconnect_at.isoformat(sep=" ") if self.last_reconnect_at else None,
                "last_order_update": self.last_order_update,
                "last_error": self.last_error,
                "last_error_code": self.last_error_code,
                "last_error_reason": self.last_error_reason,
                "last_disconnect_reason": self.last_disconnect_reason,
                "access_token_present": bool(self.access_token),
                "auth_failure_token_still_loaded": bool(
                    self._last_auth_failed_access_token
                    and self.access_token == self._last_auth_failed_access_token
                    and self.api_key == self._last_auth_failed_api_key
                ),
                "auth_recovery_hint": self._auth_recovery_hint(),
                "reconnect_request_count": self.reconnect_request_count,
                "reconnect_skipped_reason": self.reconnect_skipped_reason,
                "duplicate_start_prevented_count": self.duplicate_start_prevented_count,
                "event_queue_size": self._event_queue.qsize(),
                "event_queue_capacity": self._event_queue.maxsize,
                "event_queue_dropped_count": self.event_queue_dropped_count,
                "candle_persist_queue_size": self._candle_persist_queue.qsize(),
                "candle_persist_queue_capacity": self._candle_persist_queue.maxsize,
                "candle_persist_queue_dropped_count": self.candle_persist_queue_dropped_count,
                "subscription_errors": dict(self._subscription_errors),
            }

    def _wire_callbacks(self, ticker: Any) -> None:
        ticker.on_connect = self._on_connect
        ticker.on_ticks = self._on_ticks
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_reconnect = self._on_reconnect
        ticker.on_noreconnect = self._on_noreconnect
        if hasattr(ticker, "on_order_update"):
            ticker.on_order_update = self._on_order_update
        if hasattr(ticker, "on_message"):
            ticker.on_message = self._on_message

    def _on_connect(self, ws: Any, response: Any) -> None:
        self.connected = True
        self.connected_at = self._now()
        self.last_error = None
        self.last_error_code = None
        self.last_error_reason = None
        self.last_disconnect_reason = None
        self.reconnect_skipped_reason = None
        self.websocket_status = "CONNECTED"
        logger.info("Kite WebSocket connected")
        with self._lock:
            desired = set(self._desired_tokens)
        if desired:
            self._subscribe_connected(desired)

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        parsed_ticks: list[WebSocketTick] = []
        gap_events: list[dict[str, Any]] = []
        with self._lock:
            for payload in ticks or []:
                tick = self._parse_tick(payload)
                if tick:
                    previous = self._ticks.get(tick.instrument_token)
                    gap_event = self._detect_gap_locked(tick, previous)
                    if gap_event:
                        gap_events.append(gap_event)
                    self._ticks[tick.instrument_token] = tick
                    self._ticks_seen[tick.instrument_token] = self._ticks_seen.get(tick.instrument_token, 0) + 1
                    self._update_premium_candle(tick)
                    self.last_tick_at = self._now()
                    parsed_ticks.append(tick)
                else:
                    self.ignored_tick_count += 1
        for event in gap_events:
            self._dispatch_gap(event)
        for tick in parsed_ticks:
            self._dispatch_tick(tick)

    def _on_close(self, ws: Any, code: int | None, reason: str | None) -> None:
        self.connected = False
        self.disconnect_count += 1
        self.last_disconnect_at = self._now()
        self.last_error_code = code
        self.last_error_reason = reason
        self.last_disconnect_reason = reason or f"closed:{code}"
        session = self.market_session()
        if session != "REGULAR_MARKET":
            self.websocket_status = "DISABLED_OUTSIDE_MARKET_HOURS" if session == "WEEKEND" else "MARKET_CLOSED"
            self.reconnect_skipped_reason = "market_closed"
            self.last_error = None
            logger.info("Kite WebSocket closed outside market hours; reconnect skipped: code=%s reason=%s", code, reason)
            return
        if self._is_auth_failure(code, reason):
            self.running = False
            self.websocket_status = "AUTH_FAILED"
            self.reconnect_skipped_reason = "auth_failed"
            self.last_error = reason or f"closed:{code}"
            self._mark_auth_failed(self.last_error)
            self._signal_workers_to_stop()
            logger.error("Kite WebSocket authentication failed; Kite re-login required: code=%s reason=%s", code, reason)
            return
        if self._is_rate_limited(code, reason):
            self.running = False
            self.websocket_status = "MAX_RETRIES_EXCEEDED"
            self.reconnect_skipped_reason = "rate_limited"
            self.last_error = reason or f"closed:{code}"
            self._signal_workers_to_stop()
            logger.error("Kite WebSocket reconnect skipped due to broker rate limit: code=%s reason=%s", code, reason)
            return
        self.websocket_status = "DISCONNECTED"
        self.last_error = reason or f"closed:{code}"
        self._start_global_gap("websocket_disconnected")
        logger.warning("Kite WebSocket disconnected: code=%s reason=%s", code, reason)
        if self._request_reconnect(ws):
            self.websocket_status = "RECONNECTING"

    def _on_error(self, ws: Any, code: int | None, reason: str | None) -> None:
        self.last_error_code = code
        self.last_error_reason = reason
        session = self.market_session()
        if session != "REGULAR_MARKET":
            self.connected = False
            self.websocket_status = "DISABLED_OUTSIDE_MARKET_HOURS" if session == "WEEKEND" else "MARKET_CLOSED"
            self.reconnect_skipped_reason = "market_closed"
            self.last_error = None
            logger.info("Kite WebSocket error ignored outside market hours: code=%s reason=%s session=%s", code, reason, session)
            return
        self.last_error = reason or f"error:{code}"
        self.connected = False
        if self._is_auth_failure(code, reason):
            self.running = False
            self.websocket_status = "AUTH_FAILED"
            self.reconnect_skipped_reason = "auth_failed"
            self._mark_auth_failed(self.last_error)
            self._signal_workers_to_stop()
            logger.error("Kite WebSocket authentication failed; Kite re-login required: code=%s reason=%s", code, reason)
            return
        if self._is_rate_limited(code, reason):
            self.running = False
            self.websocket_status = "MAX_RETRIES_EXCEEDED"
            self.reconnect_skipped_reason = "rate_limited"
            self._signal_workers_to_stop()
            logger.error("Kite WebSocket reconnect skipped due to broker rate limit: code=%s reason=%s", code, reason)
            return
        self.websocket_status = "ERROR"
        self._start_global_gap("websocket_error")
        logger.warning("Kite WebSocket error: code=%s reason=%s", code, reason)
        if self._request_reconnect(ws):
            self.websocket_status = "RECONNECTING"

    def _request_reconnect(self, ws: Any) -> bool:
        if not settings.websocket_reconnect_enabled:
            self.running = False
            self.reconnect_skipped_reason = "reconnect_disabled"
            self._signal_workers_to_stop()
            return False
        reconnect = getattr(ws, "reconnect", None)
        if not callable(reconnect):
            self.running = False
            self.reconnect_skipped_reason = "reconnect_unavailable"
            self._signal_workers_to_stop()
            return False
        now = self._now()
        min_gap = max(0, int(settings.websocket_reconnect_min_gap_seconds))
        if self.last_reconnect_at is not None and (now - self.last_reconnect_at).total_seconds() < min_gap:
            self.reconnect_skipped_reason = "reconnect_backoff"
            self.websocket_status = "RECONNECT_COOLDOWN"
            logger.warning("Kite WebSocket reconnect skipped during backoff window")
            return False
        window_seconds = max(1, int(settings.websocket_reconnect_window_seconds))
        self._reconnect_attempt_times = [
            attempt for attempt in self._reconnect_attempt_times if (now - attempt).total_seconds() <= window_seconds
        ]
        max_attempts = max(1, int(settings.websocket_reconnect_max_attempts_per_window))
        if len(self._reconnect_attempt_times) >= max_attempts:
            self.running = False
            self.websocket_status = "MAX_RETRIES_EXCEEDED"
            self.reconnect_skipped_reason = "max_reconnect_attempts"
            self._signal_workers_to_stop()
            logger.error("Kite WebSocket reconnect max attempts exceeded in %ss window", window_seconds)
            return False
        try:
            self.websocket_status = "RECONNECTING"
            self.reconnect_skipped_reason = None
            self.reconnect_request_count += 1
            self.last_reconnect_at = now
            self._reconnect_attempt_times.append(now)
            reconnect()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.websocket_status = "ERROR"
            logger.warning("Kite WebSocket reconnect request failed: %s", exc)
            return False

    def _on_reconnect(self, ws: Any, attempts_count: int | None) -> None:
        session = self.market_session()
        if session != "REGULAR_MARKET":
            self.connected = False
            self.websocket_status = "DISABLED_OUTSIDE_MARKET_HOURS" if session == "WEEKEND" else "MARKET_CLOSED"
            self.reconnect_skipped_reason = "market_closed"
            self.last_error = None
            logger.info("Kite WebSocket reconnect skipped outside market hours: %s", session)
            return
        self.reconnect_count += 1
        self.last_reconnect_at = self._now()
        self._ensure_workers()
        self.reconnect_skipped_reason = None
        self.websocket_status = "RECONNECTING"
        self._finish_global_gap("websocket_reconnect")
        logger.info("Kite WebSocket reconnect attempt: %s", attempts_count)
        with self._lock:
            desired = set(self._desired_tokens)
        if self.connected and desired:
            self._subscribe_connected(desired)

    def _on_noreconnect(self, ws: Any) -> None:
        self.connected = False
        session = self.market_session()
        if session != "REGULAR_MARKET":
            self.websocket_status = "DISABLED_OUTSIDE_MARKET_HOURS" if session == "WEEKEND" else "MARKET_CLOSED"
            self.reconnect_skipped_reason = "market_closed"
            self.last_error = None
            logger.info("Kite WebSocket reconnect exhausted callback ignored outside market hours: %s", session)
            return
        self.last_error = "websocket_reconnect_exhausted"
        self.running = False
        self.websocket_status = "MAX_RETRIES_EXCEEDED"
        self.reconnect_skipped_reason = "kite_noreconnect"
        self._signal_workers_to_stop()
        logger.error("Kite WebSocket reconnect exhausted")

    def _on_order_update(self, ws: Any, data: dict[str, Any]) -> None:
        self.order_update_count += 1
        self.last_order_update = dict(data or {})
        logger.info("Kite WebSocket order update received: %s", self.last_order_update.get("order_id"))
        self._dispatch_order_update(self.last_order_update)

    def _on_message(self, ws: Any, payload: Any, is_binary: bool | None = None) -> None:
        if not is_binary:
            self.text_message_count += 1
            parsed = self._parse_text_message(payload)
            if parsed:
                self.last_order_update = parsed
                logger.info("Kite WebSocket text order message received: %s", parsed.get("order_id"))
                self._dispatch_order_update(parsed)
            else:
                logger.info("Kite WebSocket text message ignored")

    def _dispatch_order_update(self, payload: dict[str, Any]) -> None:
        if self.order_update_handler is None or not payload:
            return
        self._queue_event("order_update", dict(payload))

    def _dispatch_tick(self, tick: WebSocketTick) -> None:
        if self.tick_handler is None:
            return
        self._queue_event("tick", tick)

    def _dispatch_gap(self, event: dict[str, Any]) -> None:
        if self.gap_handler is None and not self._gap_backfill_queued(event):
            return
        self._queue_event("gap", dict(event))

    def _ensure_workers(self) -> None:
        self._ensure_event_worker()
        if settings.enable_websocket_candle_persistence:
            self._ensure_candle_persist_worker()

    def _ensure_event_worker(self) -> None:
        with self._start_stop_lock:
            if self._event_worker is not None and self._event_worker.is_alive():
                self._event_stop.clear()
                return
            self._event_stop.clear()
            self._event_worker = Thread(target=self._event_loop, name="kite-websocket-event-worker", daemon=True)
            self._event_worker.start()

    def _ensure_candle_persist_worker(self) -> None:
        with self._start_stop_lock:
            if self._candle_persist_worker is not None and self._candle_persist_worker.is_alive():
                self._candle_persist_stop.clear()
                return
            self._candle_persist_stop.clear()
            self._candle_persist_worker = Thread(
                target=self._candle_persist_loop,
                name="kite-websocket-candle-persist-worker",
                daemon=True,
            )
            self._candle_persist_worker.start()

    def _stop_workers(self) -> None:
        self._signal_workers_to_stop()
        for worker in (self._event_worker, self._candle_persist_worker):
            if worker is not None and worker.is_alive():
                worker.join(timeout=1.0)

    def _signal_workers_to_stop(self) -> None:
        self._event_stop.set()
        self._candle_persist_stop.set()

    def _event_loop(self) -> None:
        while not self._event_stop.is_set() or not self._event_queue.empty():
            try:
                event_type, payload = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle_queued_event(event_type, payload)
            except Exception:
                logger.exception("Kite WebSocket queued event failed: %s", event_type)
            finally:
                self._event_queue.task_done()

    def _candle_persist_loop(self) -> None:
        while not self._candle_persist_stop.is_set() or not self._candle_persist_queue.empty():
            try:
                key = self._candle_persist_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    candle = self._pending_candle_persist.pop(key, None)
                    self._queued_candle_persist_keys.discard(key)
                if candle is not None:
                    self._persist_premium_candle(candle)
            finally:
                self._candle_persist_queue.task_done()

    def _queue_event(self, event_type: str, payload: Any) -> None:
        if not self.running:
            self._handle_queued_event(event_type, payload)
            return
        self._ensure_event_worker()
        try:
            self._event_queue.put_nowait((event_type, payload))
        except queue.Full:
            self.event_queue_dropped_count += 1
            logger.warning("Kite WebSocket event queue full; dropped event=%s", event_type)

    def _handle_queued_event(self, event_type: str, payload: Any) -> None:
        if event_type == "order_update":
            self._run_order_update_handler(payload)
        elif event_type == "tick":
            self._run_tick_handler(payload)
        elif event_type == "gap":
            self._run_gap_handler(payload)
            self._run_gap_backfill(payload)

    def _run_order_update_handler(self, payload: dict[str, Any]) -> None:
        if self.order_update_handler is None or not payload:
            return
        try:
            self.order_update_handler(dict(payload))
        except Exception:
            logger.exception("Kite WebSocket order update handler failed")

    def _run_tick_handler(self, tick: WebSocketTick) -> None:
        if self.tick_handler is None:
            return
        try:
            self.tick_handler(tick)
        except Exception:
            logger.exception("Kite WebSocket tick handler failed")

    def _run_gap_handler(self, event: dict[str, Any]) -> None:
        if self.gap_handler is None:
            return
        try:
            self.gap_handler(dict(event))
        except Exception:
            logger.exception("Kite WebSocket gap handler failed")

    def _gap_backfill_queued(self, event: dict[str, Any] | None) -> bool:
        return bool(event and event.get("backfill_status") == "queued")

    def _run_gap_backfill(self, event: Any) -> None:
        if not isinstance(event, dict) or not self._gap_backfill_queued(event):
            return
        token = self._safe_int(event.get("instrument_token"))
        start = self._safe_datetime(event.get("gap_start"))
        end = self._safe_datetime(event.get("gap_end"))
        if token is None or start is None or end is None:
            self._update_gap_backfill_status(event, "invalid_gap_payload")
            return
        status = self._backfill_gap(token, start, end)
        self._update_gap_backfill_status(event, status)

    def _update_gap_backfill_status(self, event: dict[str, Any], status: str) -> None:
        event["backfill_status"] = status
        with self._lock:
            for recorded in reversed(self._gap_events):
                if (
                    recorded.get("type") == event.get("type")
                    and recorded.get("instrument_token") == event.get("instrument_token")
                    and recorded.get("gap_start") == event.get("gap_start")
                    and recorded.get("gap_end") == event.get("gap_end")
                ):
                    recorded["backfill_status"] = status
                    break

    def _is_auth_failure(self, code: int | None, reason: str | None, *, exc: BaseException | None = None) -> bool:
        if exc is not None and is_kite_token_exception(exc):
            return True
        if code in {401, 403}:
            return True
        text = str(reason or "").lower()
        return any(
            marker in text
            for marker in (
                "403",
                "401",
                "forbidden",
                "unauthorized",
                "invalid token",
                "expired token",
                "access token",
                "api key",
                "authentication",
            )
        )

    def _mark_auth_failed(self, message: str | None = None) -> None:
        self._last_auth_failed_access_token = self.access_token
        self._last_auth_failed_api_key = self.api_key
        kite_auth_state.mark_auth_failed(message)

    def _auth_recovery_hint(self) -> str | None:
        if self.websocket_status != "AUTH_FAILED" and not kite_auth_state.relogin_required:
            return None
        if not self.api_key or not self.access_token:
            return "Configure KITE_API_KEY and complete Kite login to create today's KITE_ACCESS_TOKEN."
        if (
            self._last_auth_failed_access_token
            and self.access_token == self._last_auth_failed_access_token
            and self.api_key == self._last_auth_failed_api_key
        ):
            return "The currently loaded Kite access token was rejected by WebSocket. Open /kite/auth or POST /kite/session with today's request_token; restarting is not required after a new token is saved."
        return "Kite login is required. Open /kite/auth or POST /kite/session with today's request_token."

    def _is_rate_limited(self, code: int | None, reason: str | None) -> bool:
        if code == 429:
            return True
        text = str(reason or "").lower()
        return "429" in text or "rate limit" in text or "too many requests" in text

    def _parse_text_message(self, payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except Exception:
                return None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return None
        if not isinstance(payload, dict):
            return None
        order_payload = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        if not isinstance(order_payload, dict):
            return None
        if order_payload.get("order_id") or order_payload.get("order_id_value"):
            return dict(order_payload)
        return None

    def _subscribe_connected(self, tokens: set[int]) -> dict[str, Any]:
        if self._ticker is None:
            return {"subscribed": [], "reason": "websocket_disconnected"}
        try:
            self._ticker.subscribe(list(tokens))
            mode_full = getattr(self._ticker, "MODE_FULL", None)
            set_mode = getattr(self._ticker, "set_mode", None)
            if mode_full is not None and callable(set_mode):
                set_mode(mode_full, list(tokens))
            with self._lock:
                self._subscribed_tokens.update(tokens)
                for token in tokens:
                    self._tick_modes[token] = "full"
                for token in tokens:
                    self._subscription_errors.pop(token, None)
            logger.info("Kite WebSocket subscribed tokens: %s", sorted(tokens))
            return {"subscribed": sorted(tokens)}
        except Exception as exc:
            self.last_error = str(exc)
            self.subscription_failure_count += 1
            with self._lock:
                for token in tokens:
                    self._subscription_errors[token] = str(exc)
            logger.warning("Kite WebSocket subscribe failed: %s", exc)
            return {"subscribed": [], "reason": "subscription_failed", "message": str(exc)}

    def _parse_tick(self, payload: dict[str, Any]) -> WebSocketTick | None:
        token = self._safe_int(payload.get("instrument_token"))
        price = self._safe_float(payload.get("last_price") or payload.get("last_traded_price"))
        if token is None or price is None or price <= 0:
            return None
        depth = payload.get("depth") if isinstance(payload.get("depth"), dict) else {}
        buy_depth = depth.get("buy", []) if isinstance(depth, dict) else []
        sell_depth = depth.get("sell", []) if isinstance(depth, dict) else []
        bid = self._safe_float(buy_depth[0].get("price")) if buy_depth else None
        ask = self._safe_float(sell_depth[0].get("price")) if sell_depth else None
        timestamp_value = payload.get("exchange_timestamp") or payload.get("last_trade_time")
        timestamp = self._safe_datetime(timestamp_value)
        timestamp_source = "exchange_timestamp" if timestamp is not None and payload.get("exchange_timestamp") else "last_trade_time" if timestamp is not None else "local_receive_time"
        packet_type = "option_full" if bid is not None or ask is not None else "index_or_ltp"
        return WebSocketTick(
            instrument_token=token,
            price=price,
            timestamp=(timestamp or self._now()).replace(tzinfo=None),
            volume=self._safe_float(payload.get("volume") or payload.get("volume_traded")),
            bid=bid,
            ask=ask,
            receive_timestamp=self._now(),
            timestamp_source=timestamp_source,
            packet_type=packet_type,
            raw=dict(payload),
        )

    def get_recent_premium_candles(self, instrument_token: int, limit: int = 10) -> list[WebSocketPremiumCandle]:
        with self._lock:
            rows = list(self._premium_candles.get(int(instrument_token), {}).values())
        rows = sorted(rows, key=lambda candle: candle.timestamp)
        return rows[-limit:]

    def get_current_session_premium_candles(self, instrument_token: int, limit: int | None = None) -> list[WebSocketPremiumCandle]:
        with self._lock:
            rows = self._current_session_candles_locked(int(instrument_token))
        rows = sorted(rows, key=lambda candle: candle.timestamp)
        return rows[-limit:] if limit else rows

    def is_subscribed(self, instrument_token: int) -> bool:
        with self._lock:
            return int(instrument_token) in self._subscribed_tokens or int(instrument_token) in self._desired_tokens

    def tick_count(self, instrument_token: int) -> int:
        with self._lock:
            return int(self._ticks_seen.get(int(instrument_token), 0))

    def premium_candle_status(self, instrument_token: int) -> dict[str, Any]:
        with self._lock:
            return self._premium_candle_status_locked(int(instrument_token), ist_now_naive())

    def _update_premium_candle(self, tick: WebSocketTick) -> None:
        if not settings.enable_websocket_premium_candle_builder:
            return
        minute = tick.timestamp.replace(second=0, microsecond=0)
        token = int(tick.instrument_token)
        bucket = self._premium_candles.setdefault(token, {})
        candle = bucket.get(minute)
        if candle is None:
            candle = WebSocketPremiumCandle(
                instrument_token=token,
                timeframe=settings.websocket_premium_candle_timeframe,
                timestamp=minute,
                open_price=tick.price,
                high_price=tick.price,
                low_price=tick.price,
                close_price=tick.price,
                volume=float(tick.volume or 0.0),
                tick_count=1,
            )
            bucket[minute] = candle
        else:
            candle.high_price = max(candle.high_price, tick.price)
            candle.low_price = min(candle.low_price, tick.price)
            candle.close_price = tick.price
            candle.volume = max(float(candle.volume or 0.0), float(tick.volume or 0.0))
            candle.tick_count += 1
        self._queue_premium_candle_persist(self._copy_premium_candle(candle))
        self._trim_premium_candles(token)

    def _copy_premium_candle(self, candle: WebSocketPremiumCandle) -> WebSocketPremiumCandle:
        return WebSocketPremiumCandle(
            instrument_token=int(candle.instrument_token),
            timeframe=str(candle.timeframe),
            timestamp=candle.timestamp.replace(tzinfo=None),
            open_price=float(candle.open_price),
            high_price=float(candle.high_price),
            low_price=float(candle.low_price),
            close_price=float(candle.close_price),
            volume=float(candle.volume or 0.0),
            tick_count=int(candle.tick_count or 0),
            source=str(candle.source),
        )

    def _queue_premium_candle_persist(self, candle: WebSocketPremiumCandle) -> None:
        if not settings.enable_websocket_candle_persistence:
            return
        if not self.running:
            self._persist_premium_candle(candle)
            return
        self._ensure_candle_persist_worker()
        key = self._candle_persist_key(candle)
        should_enqueue = False
        with self._lock:
            self._pending_candle_persist[key] = candle
            if key not in self._queued_candle_persist_keys:
                self._queued_candle_persist_keys.add(key)
                should_enqueue = True
        if not should_enqueue:
            return
        try:
            self._candle_persist_queue.put_nowait(key)
        except queue.Full:
            with self._lock:
                self._queued_candle_persist_keys.discard(key)
            self.candle_persist_queue_dropped_count += 1
            logger.warning("Kite WebSocket candle persistence queue full; dropped token=%s", candle.instrument_token)

    def _storage_symbol(self, token: int) -> str:
        return f"{settings.websocket_candle_storage_prefix}:{int(token)}".upper()

    def _candle_persist_key(self, candle: WebSocketPremiumCandle) -> tuple[int, str, datetime]:
        return (
            int(candle.instrument_token),
            str(candle.timeframe),
            candle.timestamp.replace(tzinfo=None),
        )

    def _persist_premium_candle(self, candle: WebSocketPremiumCandle) -> None:
        if not settings.enable_websocket_candle_persistence:
            return
        session = get_session()
        try:
            symbol = self._storage_symbol(candle.instrument_token)
            row = (
                session.query(Candle)
                .filter(Candle.symbol == symbol)
                .filter(Candle.timeframe == candle.timeframe)
                .filter(Candle.timestamp == candle.timestamp.replace(tzinfo=None))
                .first()
            )
            if row is None:
                row = Candle(
                    symbol=symbol,
                    timeframe=candle.timeframe,
                    timestamp=candle.timestamp.replace(tzinfo=None),
                    open_price=float(candle.open_price),
                    high_price=float(candle.high_price),
                    low_price=float(candle.low_price),
                    close_price=float(candle.close_price),
                    volume=float(candle.volume or 0.0),
                )
                session.add(row)
            else:
                row.open_price = float(candle.open_price)
                row.high_price = float(candle.high_price)
                row.low_price = float(candle.low_price)
                row.close_price = float(candle.close_price)
                row.volume = float(candle.volume or 0.0)
            session.commit()
            self._persisted_candle_writes += 1
        except Exception as exc:
            logger.warning("WebSocket premium candle persistence failed token=%s error=%s", candle.instrument_token, exc)
        finally:
            session.close()

    def cleanup_old_persisted_candles(self, *, force: bool = False) -> dict[str, Any]:
        now = self._now()
        cleanup_day = now.date()
        if not settings.enable_websocket_candle_persistence:
            return {"cleanup_enabled": False, "deleted": 0, "reason": "websocket_candle_persistence_disabled"}
        if not settings.enable_websocket_candle_daily_cleanup:
            return {"cleanup_enabled": False, "deleted": 0, "reason": "websocket_candle_daily_cleanup_disabled"}
        prefix = str(settings.websocket_candle_storage_prefix or "").strip().upper()
        if not prefix:
            return {"cleanup_enabled": False, "deleted": 0, "reason": "websocket_candle_storage_prefix_missing"}
        with self._lock:
            if not force and self._last_candle_cleanup_date == cleanup_day:
                return {
                    "cleanup_enabled": True,
                    "deleted": 0,
                    "skipped": True,
                    "reason": "already_cleaned_today",
                    "cleanup_date": cleanup_day.isoformat(),
                }
        storage_prefix = f"{prefix}:"
        escaped_prefix = (
            storage_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        cutoff = datetime.combine(cleanup_day, time.min)
        session = get_session()
        deleted = 0
        try:
            deleted = (
                session.query(Candle)
                .filter(Candle.symbol.like(f"{escaped_prefix}%", escape="\\"))
                .filter(Candle.timeframe == settings.websocket_premium_candle_timeframe)
                .filter(Candle.timestamp < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.warning("WebSocket old candle cleanup failed error=%s", exc)
            return {"cleanup_enabled": True, "deleted": 0, "reason": "cleanup_failed", "message": str(exc)}
        finally:
            session.close()
        with self._lock:
            for bucket in self._premium_candles.values():
                for timestamp in list(bucket.keys()):
                    if timestamp.replace(tzinfo=None).date() < cleanup_day:
                        bucket.pop(timestamp, None)
            self._last_candle_cleanup_date = cleanup_day
            self._last_candle_cleanup_at = now
            self._last_candle_cleanup_deleted = int(deleted or 0)
            self._total_candle_cleanup_deleted += int(deleted or 0)
        logger.info(
            "WebSocket old candle cleanup completed date=%s deleted=%s prefix=%s timeframe=%s",
            cleanup_day.isoformat(),
            int(deleted or 0),
            storage_prefix,
            settings.websocket_premium_candle_timeframe,
        )
        return {
            "cleanup_enabled": True,
            "deleted": int(deleted or 0),
            "cleanup_date": cleanup_day.isoformat(),
            "cutoff": cutoff.isoformat(sep=" "),
            "storage_prefix": storage_prefix,
            "timeframe": settings.websocket_premium_candle_timeframe,
        }

    def _rehydrate_premium_candles(self, tokens: set[int]) -> None:
        if not settings.enable_websocket_candle_persistence or not settings.enable_websocket_premium_candle_builder:
            return
        pending = {int(token) for token in tokens if int(token) not in self._rehydrated_tokens}
        if not pending:
            return
        session = get_session()
        try:
            since = self._now().replace(second=0, microsecond=0) - timedelta(minutes=max(5, settings.websocket_premium_candle_retention_minutes))
            for token in pending:
                rows = (
                    session.query(Candle)
                    .filter(Candle.symbol == self._storage_symbol(token))
                    .filter(Candle.timeframe == settings.websocket_premium_candle_timeframe)
                    .filter(Candle.timestamp >= since)
                    .order_by(Candle.timestamp.asc())
                    .all()
                )
                bucket = self._premium_candles.setdefault(token, {})
                for row in rows:
                    timestamp = row.timestamp.replace(tzinfo=None)
                    if timestamp not in bucket:
                        bucket[timestamp] = WebSocketPremiumCandle(
                            instrument_token=token,
                            timeframe=row.timeframe,
                            timestamp=timestamp,
                            open_price=float(row.open_price),
                            high_price=float(row.high_price),
                            low_price=float(row.low_price),
                            close_price=float(row.close_price),
                            volume=float(row.volume or 0.0),
                            tick_count=0,
                            source="websocket_builder_rehydrated",
                        )
                        self._rehydrated_candle_count += 1
                self._trim_premium_candles(token)
                self._rehydrated_tokens.add(token)
        except Exception as exc:
            logger.warning("WebSocket premium candle rehydrate failed tokens=%s error=%s", sorted(pending), exc)
        finally:
            session.close()

    def _detect_gap_locked(self, tick: WebSocketTick, previous: WebSocketTick | None) -> dict[str, Any] | None:
        if not settings.enable_market_data_gap_detection or self.market_session() != "REGULAR_MARKET":
            return None
        max_gap = max(settings.websocket_price_stale_seconds + 1, settings.max_websocket_gap_seconds)
        if previous is None:
            return None
        previous_time = (previous.receive_timestamp or previous.timestamp).replace(tzinfo=None)
        current_time = (tick.receive_timestamp or tick.timestamp).replace(tzinfo=None)
        gap_seconds = max(0.0, (current_time - previous_time).total_seconds())
        if gap_seconds <= max_gap:
            return None
        event = {
            "type": "tick_gap",
            "instrument_token": int(tick.instrument_token),
            "gap_start": previous_time.isoformat(sep=" "),
            "gap_end": current_time.isoformat(sep=" "),
            "gap_duration_seconds": round(gap_seconds, 3),
            "reason": "tick_gap_detected",
            "backfill_status": "queued" if settings.enable_websocket_gap_backfill else "disabled",
        }
        self._record_gap_locked(event)
        return event

    def _start_global_gap(self, reason: str) -> None:
        if not settings.enable_market_data_gap_detection or self.market_session() != "REGULAR_MARKET":
            return
        with self._lock:
            self._active_gap_started_at = self._active_gap_started_at or self._now()
            event = {
                "type": "websocket_gap_started",
                "instrument_token": None,
                "gap_start": self._active_gap_started_at.isoformat(sep=" "),
                "gap_end": None,
                "gap_duration_seconds": None,
                "reason": reason,
                "backfill_status": "pending",
            }
            self._record_gap_locked(event)
        self._dispatch_gap(event)

    def _finish_global_gap(self, reason: str) -> None:
        if not settings.enable_market_data_gap_detection:
            return
        with self._lock:
            if self._active_gap_started_at is None:
                return
            end = self._now()
            gap_seconds = max(0.0, (end - self._active_gap_started_at).total_seconds())
            event = {
                "type": "websocket_gap_finished",
                "instrument_token": None,
                "gap_start": self._active_gap_started_at.isoformat(sep=" "),
                "gap_end": end.isoformat(sep=" "),
                "gap_duration_seconds": round(gap_seconds, 3),
                "reason": reason,
                "backfill_status": "not_applicable_global_gap",
            }
            self._active_gap_started_at = None
            self._record_gap_locked(event)
        self._dispatch_gap(event)

    def _record_gap_locked(self, event: dict[str, Any]) -> None:
        self._gap_events.append(dict(event))
        self._gap_events = self._gap_events[-50:]

    def _backfill_gap(self, token: int, start: datetime, end: datetime) -> str:
        if not settings.enable_websocket_gap_backfill:
            return "disabled"
        self._gap_backfill_attempt_count += 1
        try:
            from app.providers.kite_provider import KiteProvider

            provider = KiteProvider()
            fetched = provider.historical_data(int(token), start, end, settings.websocket_premium_candle_timeframe)
            inserted = 0
            for item in fetched or []:
                timestamp = self._safe_datetime(item.get("date") or item.get("timestamp"))
                close = self._safe_float(item.get("close"))
                if timestamp is None or close is None or close <= 0:
                    continue
                candle = WebSocketPremiumCandle(
                    instrument_token=int(token),
                    timeframe=settings.websocket_premium_candle_timeframe,
                    timestamp=timestamp.replace(second=0, microsecond=0, tzinfo=None),
                    open_price=float(item.get("open") or close),
                    high_price=float(item.get("high") or close),
                    low_price=float(item.get("low") or close),
                    close_price=close,
                    volume=float(item.get("volume") or 0.0),
                    tick_count=0,
                    source="kite_historical_backfill",
                )
                with self._lock:
                    self._premium_candles.setdefault(int(token), {})[candle.timestamp] = candle
                self._persist_premium_candle(candle)
                inserted += 1
            if inserted:
                self._gap_backfill_success_count += 1
                return "success"
            self._gap_backfill_failure_count += 1
            return "partial_or_empty"
        except Exception as exc:
            self._gap_backfill_failure_count += 1
            logger.warning("WebSocket gap backfill failed token=%s error=%s", token, exc)
            return "failed"

    def _gap_status_locked(self, now: datetime) -> dict[str, Any]:
        active_seconds = (
            round(max(0.0, (now - self._active_gap_started_at).total_seconds()), 3)
            if self._active_gap_started_at is not None
            else None
        )
        return {
            "enabled": settings.enable_market_data_gap_detection,
            "data_gap_detected": bool(self._gap_events),
            "active_gap": self._active_gap_started_at is not None,
            "active_gap_started_at": self._active_gap_started_at.isoformat(sep=" ") if self._active_gap_started_at else None,
            "active_gap_duration_seconds": active_seconds,
            "max_gap_seconds": settings.max_websocket_gap_seconds,
            "gap_count": len(self._gap_events),
            "latest_gap": self._gap_events[-1] if self._gap_events else None,
            "recent_gaps": list(self._gap_events[-10:]),
            "backfill_enabled": settings.enable_websocket_gap_backfill,
            "backfill_attempt_count": self._gap_backfill_attempt_count,
            "backfill_success_count": self._gap_backfill_success_count,
            "backfill_failure_count": self._gap_backfill_failure_count,
        }

    def _trim_premium_candles(self, token: int) -> None:
        retention = max(5, settings.websocket_premium_candle_retention_minutes)
        bucket = self._premium_candles.get(token)
        if not bucket or len(bucket) <= retention:
            return
        keep = set(sorted(bucket.keys())[-retention:])
        for key in list(bucket.keys()):
            if key not in keep:
                bucket.pop(key, None)

    def _current_session_candles_locked(self, token: int) -> list[WebSocketPremiumCandle]:
        today = self._now().date()
        rows = list(self._premium_candles.get(int(token), {}).values())
        return [candle for candle in rows if candle.timestamp.replace(tzinfo=None).date() == today]

    def _premium_candle_status_locked(self, token: int, now: datetime) -> dict[str, Any]:
        candles = sorted(self._current_session_candles_locked(int(token)), key=lambda candle: candle.timestamp)
        current = candles[-1] if candles else None
        completed = candles[-2] if len(candles) >= 2 else None
        age = max(0.0, (now - current.timestamp.replace(tzinfo=None)).total_seconds()) if current else None
        return {
            "subscribed": int(token) in self._subscribed_tokens or int(token) in self._desired_tokens,
            "ticks_seen": int(self._ticks_seen.get(int(token), 0)),
            "current_session_candle_count": len(candles),
            "current_building_candle": self._candle_payload(current),
            "last_completed_candle": self._candle_payload(completed),
            "last_candle_age_seconds": round(age, 3) if age is not None else None,
            "minimum_required_premium_candles": settings.min_websocket_premium_candles,
        }

    def _candle_payload(self, candle: WebSocketPremiumCandle | None) -> dict[str, Any] | None:
        if candle is None:
            return None
        return {
            "instrument_token": candle.instrument_token,
            "timeframe": candle.timeframe,
            "timestamp": candle.timestamp.isoformat(sep=" "),
            "open": round(candle.open_price, 2),
            "high": round(candle.high_price, 2),
            "low": round(candle.low_price, 2),
            "close": round(candle.close_price, 2),
            "volume": candle.volume,
            "tick_count": candle.tick_count,
            "source": candle.source,
        }

    def _age_seconds(self, timestamp: datetime) -> float:
        return max(0.0, (self._now() - timestamp.replace(tzinfo=None)).total_seconds())

    def _safe_int(self, value: Any) -> int | None:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            return None
        return None

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            return None
        return None

    def _safe_datetime(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).replace(tzinfo=None)
            except ValueError:
                return None
        return None

    def _connected_duration_seconds(self) -> float | None:
        if not self.connected or self.connected_at is None:
            return None
        return round(max(0.0, (self._now() - self.connected_at).total_seconds()), 3)

    def market_session(self, now: datetime | None = None) -> str:
        current = (now or self._now()).replace(tzinfo=None)
        mode = self.market_session_service.current_runtime_mode(current)
        if mode in {"MARKET_OPEN", "MARKET_CLOSING", "MANUAL_OVERRIDE"}:
            return "REGULAR_MARKET"
        if mode == "PRE_MARKET":
            return "PRE_MARKET"
        if mode == "HOLIDAY":
            return "HOLIDAY"
        if current.weekday() >= 5:
            return "WEEKEND"
        if current.time() > self._parse_time(settings.runtime_market_close_time):
            return "AFTER_MARKET"
        return "MARKET_CLOSED"

    def _now(self) -> datetime:
        return self.clock().replace(tzinfo=None)

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
