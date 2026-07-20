from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Callable

from app.config import settings
from app.services.kite_websocket_price_feed import WebSocketTick


class BankNiftyFastRallyService:
    """Turn Bank Nifty websocket acceleration into a lightweight rescan request."""

    def __init__(self, callback: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.callback = callback
        self.underlying_token: int | None = None
        self._ticks: deque[tuple[datetime, float]] = deque(maxlen=512)
        self._lock = RLock()
        self.last_event: dict[str, Any] | None = None
        self.trigger_count = 0

    def set_underlying_token(self, token: int | None) -> None:
        self.underlying_token = int(token) if token else None

    def on_tick(self, tick: WebSocketTick) -> dict[str, Any] | None:
        if self.underlying_token is None or int(tick.instrument_token) != self.underlying_token:
            return None
        now = (tick.receive_timestamp or tick.timestamp).replace(tzinfo=None)
        price = float(tick.price or 0.0)
        if price <= 0:
            return None
        window_seconds = max(1.0, float(settings.fast_rally_window_seconds))
        cutoff = now - timedelta(seconds=max(window_seconds * 6.0, 30.0))
        with self._lock:
            self._ticks.append((now, price))
            while self._ticks and self._ticks[0][0] < cutoff:
                self._ticks.popleft()
            window = [(timestamp, value) for timestamp, value in self._ticks if timestamp >= now - timedelta(seconds=window_seconds)]
            if len(window) < 2:
                return None
            base = float(window[0][1])
            move_pct = ((price - base) / max(base, 0.01)) * 100.0
            threshold = max(0.01, float(settings.fast_rally_trigger_pct))
            direction = "bullish" if move_pct >= threshold else "bearish" if move_pct <= -threshold else None
            if direction is None:
                return None
            event = {
                "type": "banknifty_fast_rally",
                "direction": direction,
                "instrument_token": self.underlying_token,
                "price": round(price, 2),
                "base_price": round(base, 2),
                "move_pct": round(move_pct, 4),
                "window_seconds": window_seconds,
                "timestamp": now.isoformat(sep=" "),
            }
            self.last_event = event
            self.trigger_count += 1
        if self.callback is not None:
            self.callback(dict(event))
        return event

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "underlying_token": self.underlying_token,
                "trigger_count": self.trigger_count,
                "buffered_ticks": len(self._ticks),
                "last_event": dict(self.last_event) if self.last_event else None,
            }
