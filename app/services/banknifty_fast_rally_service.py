from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Callable

from app.config import settings
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


class BankNiftyFastRallyService:
    """Turn Bank Nifty websocket acceleration into a lightweight rescan request."""

    def __init__(self, callback: Callable[[dict[str, Any]], Any] | None = None, *, latency_metrics: Any | None = None) -> None:
        self.callback = callback
        self.latency_metrics = latency_metrics
        self.underlying_token: int | None = None
        self._ticks: deque[tuple[datetime, float]] = deque(maxlen=512)
        self._lock = RLock()
        self.last_event: dict[str, Any] | None = None
        self.last_observation: dict[str, Any] | None = None
        self.last_callback_result: dict[str, Any] | None = None
        self.last_callback_error: dict[str, Any] | None = None
        self.trigger_count = 0
        self.target_tick_count = 0
        self.evaluated_tick_count = 0
        self.invalid_price_count = 0
        self.callback_scheduled_count = 0
        self.callback_suppressed_count = 0
        self.callback_error_count = 0
        self.trigger_count_by_direction = {"bullish": 0, "bearish": 0}
        self.suppressed_reasons: dict[str, int] = {}

    def set_underlying_token(self, token: int | None) -> None:
        self.underlying_token = int(token) if token else None

    def on_tick(self, tick: WebSocketTick) -> dict[str, Any] | None:
        if self.underlying_token is None or int(tick.instrument_token) != self.underlying_token:
            return None
        now = (tick.receive_timestamp or tick.timestamp).replace(tzinfo=None)
        price = float(tick.price or 0.0)
        if price <= 0:
            with self._lock:
                self.invalid_price_count += 1
            return None
        window_seconds = max(1.0, float(settings.fast_rally_window_seconds))
        cutoff = now - timedelta(seconds=max(window_seconds * 6.0, 30.0))
        with self._lock:
            self.target_tick_count += 1
            self._ticks.append((now, price))
            while self._ticks and self._ticks[0][0] < cutoff:
                self._ticks.popleft()
            window = [(timestamp, value) for timestamp, value in self._ticks if timestamp >= now - timedelta(seconds=window_seconds)]
            if len(window) < 2:
                self.last_observation = {
                    "timestamp": now.isoformat(sep=" "),
                    "price": round(price, 2),
                    "window_samples": len(window),
                    "window_seconds": window_seconds,
                    "threshold_pct": max(0.01, float(settings.fast_rally_trigger_pct)),
                    "move_pct": 0.0,
                    "direction": None,
                    "reason": "insufficient_window_samples",
                }
                return None
            self.evaluated_tick_count += 1
            base = float(window[0][1])
            move_pct = ((price - base) / max(base, 0.01)) * 100.0
            threshold = max(0.01, float(settings.fast_rally_trigger_pct))
            direction = "bullish" if move_pct >= threshold else "bearish" if move_pct <= -threshold else None
            self.last_observation = {
                "timestamp": now.isoformat(sep=" "),
                "price": round(price, 2),
                "base_price": round(base, 2),
                "window_samples": len(window),
                "window_seconds": window_seconds,
                "threshold_pct": threshold,
                "move_pct": round(move_pct, 4),
                "threshold_progress_pct": round(min(100.0, abs(move_pct) / threshold * 100.0), 2),
                "direction": direction,
                "reason": "threshold_crossed" if direction else "below_threshold",
            }
            if direction is None:
                return None
            detected_at = ist_now_naive()
            event = {
                "type": "banknifty_fast_rally",
                "direction": direction,
                "instrument_token": self.underlying_token,
                "price": round(price, 2),
                "base_price": round(base, 2),
                "move_pct": round(move_pct, 4),
                "window_seconds": window_seconds,
                "timestamp": detected_at.isoformat(sep=" "),
                "exchange_timestamp": tick.timestamp.isoformat(sep=" "),
                "receive_timestamp": (tick.receive_timestamp or now).isoformat(sep=" "),
                "timestamp_source": tick.timestamp_source,
                **current_strategy_lineage(),
            }
            self.last_event = event
            self.trigger_count += 1
            self.trigger_count_by_direction[direction] = self.trigger_count_by_direction.get(direction, 0) + 1
        if self.callback is not None:
            try:
                callback_result = self.callback(dict(event))
                normalized_result = dict(callback_result) if isinstance(callback_result, dict) else {"result": callback_result}
                with self._lock:
                    self.last_callback_result = normalized_result
                    self.last_callback_error = None
                    if bool(normalized_result.get("scheduled")):
                        self.callback_scheduled_count += 1
                    else:
                        self.callback_suppressed_count += 1
                        reason = str(normalized_result.get("reason") or "callback_not_scheduled")
                        self.suppressed_reasons[reason] = self.suppressed_reasons.get(reason, 0) + 1
                    event["dispatch"] = normalized_result
                    self.last_event = dict(event)
            except Exception as exc:
                error = {
                    "time": ist_now_naive().isoformat(sep=" "),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
                with self._lock:
                    self.callback_error_count += 1
                    self.last_callback_error = error
                    event["dispatch"] = {"scheduled": False, "reason": "fast_rally_callback_failed", **error}
                    self.last_event = dict(event)
        if self.latency_metrics is not None:
            if tick.receive_timestamp is None:
                self.latency_metrics.record_missing("receive_to_fast_rally_detection", detail={"direction": direction, "reason": "receive_timestamp_missing"})
            else:
                self.latency_metrics.record_between(
                    "local_receipt_to_fast_rally_detection",
                    tick.receive_timestamp,
                    detected_at,
                    detail={"direction": direction, "move_pct": event["move_pct"]},
                )
                self.latency_metrics.record_between(
                    "receive_to_fast_rally_detection",
                    tick.receive_timestamp,
                    detected_at,
                    detail={"direction": direction, "move_pct": event["move_pct"]},
                )
        return event

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "underlying_token": self.underlying_token,
                "trigger_count": self.trigger_count,
                "trigger_count_by_direction": dict(self.trigger_count_by_direction),
                "target_tick_count": self.target_tick_count,
                "evaluated_tick_count": self.evaluated_tick_count,
                "invalid_price_count": self.invalid_price_count,
                "buffered_ticks": len(self._ticks),
                "window_seconds": max(1.0, float(settings.fast_rally_window_seconds)),
                "threshold_pct": max(0.01, float(settings.fast_rally_trigger_pct)),
                "last_observation": dict(self.last_observation) if self.last_observation else None,
                "last_event": dict(self.last_event) if self.last_event else None,
                "last_callback_result": dict(self.last_callback_result) if self.last_callback_result else None,
                "last_callback_error": dict(self.last_callback_error) if self.last_callback_error else None,
                "callback_scheduled_count": self.callback_scheduled_count,
                "callback_suppressed_count": self.callback_suppressed_count,
                "callback_error_count": self.callback_error_count,
                "suppressed_reasons": dict(self.suppressed_reasons),
            }
