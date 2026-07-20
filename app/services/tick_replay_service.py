from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable

from app.services.kite_websocket_price_feed import WebSocketTick


class TickReplayService:
    """Deterministically replay captured ticks without wall-clock sleeps."""

    def __init__(self, handler: Callable[[WebSocketTick], Any]) -> None:
        self.handler = handler

    def replay(self, events: Iterable[dict[str, Any] | WebSocketTick]) -> dict[str, Any]:
        ticks = [self._tick(event) for event in events]
        ordered = sorted(ticks, key=lambda tick: (tick.receive_timestamp or tick.timestamp, tick.instrument_token))
        results: list[Any] = []
        for tick in ordered:
            results.append(self.handler(tick))
        return {
            "ticks_replayed": len(ordered),
            "first_timestamp": self._timestamp(ordered[0]) if ordered else None,
            "last_timestamp": self._timestamp(ordered[-1]) if ordered else None,
            "results": results,
        }

    def _tick(self, event: dict[str, Any] | WebSocketTick) -> WebSocketTick:
        if isinstance(event, WebSocketTick):
            return event
        timestamp = self._datetime(event.get("timestamp") or event.get("exchange_timestamp"))
        receive = self._datetime(event.get("receive_timestamp") or timestamp)
        return WebSocketTick(
            instrument_token=int(event["instrument_token"]),
            price=float(event["price"]),
            timestamp=timestamp,
            volume=float(event["volume"]) if event.get("volume") is not None else None,
            bid=float(event["bid"]) if event.get("bid") is not None else None,
            ask=float(event["ask"]) if event.get("ask") is not None else None,
            receive_timestamp=receive,
            timestamp_source=str(event.get("timestamp_source") or "replay"),
            packet_type=str(event.get("packet_type") or "replay"),
        )

    def _datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)

    def _timestamp(self, tick: WebSocketTick) -> str:
        return (tick.receive_timestamp or tick.timestamp).isoformat(sep=" ")
