from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from datetime import datetime, time
from threading import RLock
from typing import Any, Callable

from app.config import settings
from app.providers.token_store import load_access_token
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.api_key = api_key or settings.kite_api_key
        self.access_token = access_token or load_access_token() or settings.kite_access_token
        self.ticker_factory = ticker_factory
        self.order_update_handler = order_update_handler
        self.clock = clock or ist_now_naive
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

    def start(self) -> dict[str, Any]:
        if not settings.enable_kite_websocket:
            self.websocket_status = "DISABLED"
            return {"started": False, "reason": "websocket_disabled"}
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
            return {"started": True, "already_running": True}
        if not self.api_key or not self.access_token:
            self.last_error = "missing_kite_api_key_or_access_token"
            self.websocket_status = "ERROR"
            logger.warning("Kite WebSocket not started: %s", self.last_error)
            return {"started": False, "reason": self.last_error}
        factory = self.ticker_factory or KiteTicker
        if factory is None:
            self.last_error = "kite_ticker_unavailable"
            self.websocket_status = "ERROR"
            logger.warning("Kite WebSocket not started: kiteconnect.KiteTicker unavailable")
            return {"started": False, "reason": self.last_error}

        self._ticker = factory(str(self.api_key), str(self.access_token))
        self._wire_callbacks(self._ticker)
        self.running = True
        try:
            self._ticker.connect(threaded=True)
        except TypeError:
            self._ticker.connect()
        except Exception as exc:
            self.running = False
            self.connected = False
            self.last_error = str(exc)
            self.websocket_status = "ERROR"
            logger.exception("Kite WebSocket connect failed")
            return {"started": False, "reason": self.last_error}
        logger.info("Kite WebSocket start requested")
        return {"started": True}

    def stop(self) -> dict[str, Any]:
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
        return {"stopped": True}

    def subscribe(self, tokens: list[int] | set[int] | tuple[int, ...]) -> dict[str, Any]:
        clean_tokens = {int(token) for token in tokens if self._safe_int(token) is not None and int(token) > 0}
        with self._lock:
            self._desired_tokens.update(clean_tokens)
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
                "order_update_count": self.order_update_count,
                "text_message_count": self.text_message_count,
                "last_disconnect_at": self.last_disconnect_at.isoformat(sep=" ") if self.last_disconnect_at else None,
                "last_reconnect_at": self.last_reconnect_at.isoformat(sep=" ") if self.last_reconnect_at else None,
                "last_order_update": self.last_order_update,
                "last_error": self.last_error,
                "reconnect_skipped_reason": self.reconnect_skipped_reason,
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
        self.reconnect_skipped_reason = None
        self.websocket_status = "CONNECTED"
        logger.info("Kite WebSocket connected")
        with self._lock:
            desired = set(self._desired_tokens)
        if desired:
            self._subscribe_connected(desired)

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        with self._lock:
            for payload in ticks or []:
                tick = self._parse_tick(payload)
                if tick:
                    self._ticks[tick.instrument_token] = tick
                    self._ticks_seen[tick.instrument_token] = self._ticks_seen.get(tick.instrument_token, 0) + 1
                    self._update_premium_candle(tick)
                    self.last_tick_at = self._now()
                else:
                    self.ignored_tick_count += 1

    def _on_close(self, ws: Any, code: int | None, reason: str | None) -> None:
        self.connected = False
        self.disconnect_count += 1
        self.last_disconnect_at = self._now()
        session = self.market_session()
        if session != "REGULAR_MARKET":
            self.websocket_status = "DISABLED_OUTSIDE_MARKET_HOURS" if session == "WEEKEND" else "MARKET_CLOSED"
            self.reconnect_skipped_reason = "market_closed"
            self.last_error = None
            logger.info("Kite WebSocket closed outside market hours; reconnect skipped: code=%s reason=%s", code, reason)
            return
        self.websocket_status = "DISCONNECTED"
        self.last_error = reason or f"closed:{code}"
        logger.warning("Kite WebSocket disconnected: code=%s reason=%s", code, reason)
        if settings.websocket_reconnect_enabled:
            reconnect = getattr(ws, "reconnect", None)
            if callable(reconnect):
                try:
                    reconnect()
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.warning("Kite WebSocket reconnect request failed: %s", exc)

    def _on_error(self, ws: Any, code: int | None, reason: str | None) -> None:
        self.last_error = reason or f"error:{code}"
        self.websocket_status = "ERROR"
        logger.warning("Kite WebSocket error: code=%s reason=%s", code, reason)

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
        self.reconnect_skipped_reason = None
        self.websocket_status = "RECONNECTING"
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
        self.websocket_status = "RECONNECT_EXHAUSTED"
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
        try:
            self.order_update_handler(dict(payload))
        except Exception:
            logger.exception("Kite WebSocket order update handler failed")

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
        self._trim_premium_candles(token)

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
            return value
        return None

    def _connected_duration_seconds(self) -> float | None:
        if not self.connected or self.connected_at is None:
            return None
        return round(max(0.0, (self._now() - self.connected_at).total_seconds()), 3)

    def market_session(self, now: datetime | None = None) -> str:
        now = (now or self._now()).replace(tzinfo=None)
        if now.weekday() >= 5:
            return "WEEKEND"
        start = self._parse_time(settings.market_open_time)
        end = self._parse_time(settings.market_close_time)
        if start <= now.time() <= end:
            return "REGULAR_MARKET"
        return "PRE_MARKET" if now.time() < start else "AFTER_MARKET"

    def _now(self) -> datetime:
        return self.clock().replace(tzinfo=None)

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
