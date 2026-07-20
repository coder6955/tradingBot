from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Callable, Iterable

from app.services.database import RawTickRecord, get_session
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.strategy_lineage_service import current_strategy_lineage


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
            **current_strategy_lineage(),
        }

    def replay_persisted(
        self,
        *,
        session_date: str,
        instrument_tokens: Iterable[int] | None = None,
        config_hash: str | None = None,
    ) -> dict[str, Any]:
        session = get_session()
        try:
            query = session.query(RawTickRecord).filter(RawTickRecord.session_date == str(session_date))
            tokens = {int(token) for token in instrument_tokens or []}
            if tokens:
                query = query.filter(RawTickRecord.instrument_token.in_(tokens))
            if config_hash:
                query = query.filter(RawTickRecord.config_hash == str(config_hash))
            rows = query.order_by(RawTickRecord.sequence.asc(), RawTickRecord.id.asc()).all()
            events = [
                {
                    "instrument_token": row.instrument_token,
                    "price": row.last_price,
                    "bid": row.bid,
                    "ask": row.ask,
                    "depth": json.loads(row.depth_json or "{}"),
                    "volume": row.cumulative_volume,
                    "timestamp": row.exchange_timestamp or row.receive_timestamp,
                    "receive_timestamp": row.receive_timestamp,
                    "timestamp_source": row.timestamp_source,
                    "packet_type": row.packet_type,
                }
                for row in rows
            ]
            lineage_values = sorted({(str(row.strategy_version), str(row.config_hash)) for row in rows})
        finally:
            session.close()
        ordered_ticks = [self._tick(event) for event in events]
        replay_results = [self.handler(tick) for tick in ordered_ticks]
        result = {
            "ticks_replayed": len(ordered_ticks),
            "first_timestamp": self._timestamp(ordered_ticks[0]) if ordered_ticks else None,
            "last_timestamp": self._timestamp(ordered_ticks[-1]) if ordered_ticks else None,
            "results": replay_results,
            **current_strategy_lineage(),
        }
        result.update(
            {
                "session_date": str(session_date),
                "persisted_tick_count": len(events),
                "lineages": [
                    {"strategy_version": strategy_version, "config_hash": hash_value}
                    for strategy_version, hash_value in lineage_values
                ],
            "mixed_lineage": len(lineage_values) > 1,
                "sequence_gap_count": self._sequence_gap_count(rows) if not tokens else None,
            }
        )
        return result

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
            buy_depth=tuple(dict(level) for level in event.get("depth", {}).get("buy", []) if isinstance(level, dict)),
            sell_depth=tuple(dict(level) for level in event.get("depth", {}).get("sell", []) if isinstance(level, dict)),
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

    def _sequence_gap_count(self, rows: list[RawTickRecord]) -> int:
        if len(rows) < 2:
            return 0
        return sum(1 for previous, current in zip(rows, rows[1:]) if int(current.sequence) != int(previous.sequence) + 1)
