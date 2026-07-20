from __future__ import annotations

import logging
import queue
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event, RLock, Thread
from typing import Any

from app.config import settings
from app.services.database import Candle, get_session
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.market_session_service import MarketSessionService
from app.services.time_utils import ist_now_naive


logger = logging.getLogger(__name__)


@dataclass
class _BuildingCandle:
    timeframe: str
    timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    receive_timestamp: datetime | None
    is_generated: bool = False
    data_quality: str = "exchange_tick_complete"


class UnderlyingCandleService:
    """Build canonical BANKNIFTY 1m/5m candles from exchange-timestamped ticks."""

    def __init__(self, *, market_session_service: MarketSessionService | None = None) -> None:
        self.market_session_service = market_session_service or MarketSessionService()
        self.symbol = str(settings.underlying_candle_symbol or "BANKNIFTY").upper()
        self.instrument_token: int | None = None
        self._one_minute: _BuildingCandle | None = None
        self._five_minute: _BuildingCandle | None = None
        self._last_completed_1m: _BuildingCandle | None = None
        self._last_cumulative_volume: float | None = None
        self._persist_queue: queue.Queue[_BuildingCandle] = queue.Queue(maxsize=max(100, settings.websocket_candle_persist_queue_size))
        self._stop = Event()
        self._worker: Thread | None = None
        self._lock = RLock()
        self.accepted_ticks = 0
        self.rejected_ticks = 0
        self.persisted_candles = 0
        self.idempotent_skips = 0
        self.generated_continuity_candles = 0
        self.dropped_candles = 0
        self.last_rejection_reason: str | None = None
        self.last_completed: dict[str, dict[str, Any]] = {}

    def set_underlying_token(self, token: int | None) -> None:
        self.instrument_token = int(token) if token else None

    def start(self) -> dict[str, Any]:
        if not settings.enable_underlying_candle_pipeline:
            return {"started": False, "reason": "underlying_candle_pipeline_disabled"}
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return {"started": True, "already_running": True}
            self._recover_last_completed()
            self._stop.clear()
            self._worker = Thread(target=self._run, name="underlying-candle-persistence", daemon=True)
            self._worker.start()
        return {"started": True}

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5.0)
        return {"stopped": True, **self.status(include_coverage=False)}

    def on_tick(self, tick: WebSocketTick) -> dict[str, Any]:
        if not settings.enable_underlying_candle_pipeline:
            return {"accepted": False, "reason": "underlying_candle_pipeline_disabled"}
        if self.instrument_token is None or int(tick.instrument_token) != self.instrument_token:
            return {"accepted": False, "reason": "not_underlying_token"}
        if tick.timestamp_source not in {"exchange_timestamp", "last_trade_time"}:
            return self._reject("exchange_timestamp_provenance_required")
        exchange_time = tick.timestamp.replace(tzinfo=None)
        if not self.market_session_service.is_market_open(exchange_time):
            return self._reject("outside_configured_nse_session")
        minute = exchange_time.replace(second=0, microsecond=0)
        volume_increment = self._volume_increment(tick.volume)
        with self._lock:
            self._roll_one_minute(minute, tick, volume_increment)
            self.accepted_ticks += 1
        return {"accepted": True, "minute": minute.isoformat(sep=" ")}

    def status(self, *, include_coverage: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled": settings.enable_underlying_candle_pipeline,
            "symbol": self.symbol,
            "instrument_token": self.instrument_token,
            "running": bool(self._worker and self._worker.is_alive()),
            "accepted_ticks": self.accepted_ticks,
            "rejected_ticks": self.rejected_ticks,
            "last_rejection_reason": self.last_rejection_reason,
            "persist_queue_size": self._persist_queue.qsize(),
            "persisted_candles": self.persisted_candles,
            "idempotent_skips": self.idempotent_skips,
            "generated_continuity_candles": self.generated_continuity_candles,
            "dropped_candles": self.dropped_candles,
            "last_completed": dict(self.last_completed),
            "timestamp_authority": "exchange_timestamp",
        }
        if include_coverage:
            payload["coverage"] = self.coverage()
        return payload

    def coverage(self) -> dict[str, Any]:
        session = get_session()
        try:
            result: dict[str, Any] = {}
            for timeframe in ("1minute", "5minute"):
                query = session.query(Candle).filter(Candle.symbol == self.symbol, Candle.timeframe == timeframe)
                latest = query.order_by(Candle.timestamp.desc()).first()
                result[timeframe] = {
                    "count": int(query.count()),
                    "last_completed_candle": self._candle_dict(latest) if latest else None,
                    "timestamp_source": getattr(latest, "timestamp_source", None) if latest else None,
                    "data_quality": getattr(latest, "data_quality", None) if latest else "missing",
                }
            return result
        finally:
            session.close()

    def _roll_one_minute(self, minute: datetime, tick: WebSocketTick, volume_increment: float) -> None:
        if self._one_minute is not None and minute < self._one_minute.timestamp:
            self._reject("out_of_order_underlying_tick")
            return
        if self._one_minute is None:
            self._fill_restart_gaps(minute)
            self._one_minute = self._new_live_candle("1minute", minute, tick, volume_increment)
            return
        if minute == self._one_minute.timestamp:
            self._update(self._one_minute, tick, volume_increment)
            return
        completed = self._one_minute
        self._complete_one_minute(completed)
        cursor = completed.timestamp + timedelta(minutes=1)
        previous_close = completed.close_price
        while cursor < minute and self.market_session_service.is_market_open(cursor):
            generated = _BuildingCandle(
                timeframe="1minute",
                timestamp=cursor,
                open_price=previous_close,
                high_price=previous_close,
                low_price=previous_close,
                close_price=previous_close,
                volume=0.0,
                receive_timestamp=None,
                is_generated=True,
                data_quality="generated_session_continuity",
            )
            self.generated_continuity_candles += 1
            self._complete_one_minute(generated)
            cursor += timedelta(minutes=1)
        self._close_completed_five_minute_before(minute)
        self._one_minute = self._new_live_candle("1minute", minute, tick, volume_increment)

    def _close_completed_five_minute_before(self, minute: datetime) -> None:
        if self._five_minute is None:
            return
        current_bucket = minute.replace(minute=(minute.minute // 5) * 5, second=0, microsecond=0)
        if self._five_minute.timestamp >= current_bucket:
            return
        completed = self._five_minute
        self._queue_candle(completed)
        self.last_completed["5minute"] = self._building_dict(completed)
        self._five_minute = None

    def _complete_one_minute(self, candle: _BuildingCandle) -> None:
        self._last_completed_1m = candle
        self._queue_candle(candle)
        self._aggregate_five_minute(candle)
        self.last_completed["1minute"] = self._building_dict(candle)

    def _aggregate_five_minute(self, candle: _BuildingCandle) -> None:
        bucket_minute = (candle.timestamp.minute // 5) * 5
        bucket = candle.timestamp.replace(minute=bucket_minute, second=0, microsecond=0)
        if self._five_minute is None:
            self._five_minute = _BuildingCandle(
                timeframe="5minute",
                timestamp=bucket,
                open_price=candle.open_price,
                high_price=candle.high_price,
                low_price=candle.low_price,
                close_price=candle.close_price,
                volume=candle.volume,
                receive_timestamp=candle.receive_timestamp,
                is_generated=candle.is_generated,
                data_quality="generated_session_continuity" if candle.is_generated else "exchange_tick_complete",
            )
            return
        if bucket == self._five_minute.timestamp:
            self._five_minute.high_price = max(self._five_minute.high_price, candle.high_price)
            self._five_minute.low_price = min(self._five_minute.low_price, candle.low_price)
            self._five_minute.close_price = candle.close_price
            self._five_minute.volume += candle.volume
            self._five_minute.receive_timestamp = candle.receive_timestamp or self._five_minute.receive_timestamp
            self._five_minute.is_generated = self._five_minute.is_generated and candle.is_generated
            if not candle.is_generated:
                self._five_minute.data_quality = "exchange_tick_complete"
            return
        completed = self._five_minute
        self._queue_candle(completed)
        self.last_completed["5minute"] = self._building_dict(completed)
        self._five_minute = _BuildingCandle(
            timeframe="5minute",
            timestamp=bucket,
            open_price=candle.open_price,
            high_price=candle.high_price,
            low_price=candle.low_price,
            close_price=candle.close_price,
            volume=candle.volume,
            receive_timestamp=candle.receive_timestamp,
            is_generated=candle.is_generated,
            data_quality="generated_session_continuity" if candle.is_generated else "exchange_tick_complete",
        )

    def _fill_restart_gaps(self, minute: datetime) -> None:
        previous = self._last_completed_1m
        if previous is None or previous.timestamp.date() != minute.date():
            return
        cursor = previous.timestamp + timedelta(minutes=1)
        while cursor < minute and self.market_session_service.is_market_open(cursor):
            generated = _BuildingCandle(
                timeframe="1minute",
                timestamp=cursor,
                open_price=previous.close_price,
                high_price=previous.close_price,
                low_price=previous.close_price,
                close_price=previous.close_price,
                volume=0.0,
                receive_timestamp=None,
                is_generated=True,
                data_quality="generated_session_continuity_after_restart",
            )
            self.generated_continuity_candles += 1
            self._complete_one_minute(generated)
            previous = generated
            cursor += timedelta(minutes=1)

    def _queue_candle(self, candle: _BuildingCandle) -> None:
        try:
            self._persist_queue.put_nowait(candle)
        except queue.Full:
            self.dropped_candles += 1
            logger.critical("canonical_underlying_candle_queue_full timeframe=%s timestamp=%s", candle.timeframe, candle.timestamp)

    def _run(self) -> None:
        while not self._stop.is_set() or not self._persist_queue.empty():
            try:
                candle = self._persist_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._persist(candle)
            finally:
                self._persist_queue.task_done()

    def _persist(self, candle: _BuildingCandle) -> None:
        session = get_session()
        try:
            existing = (
                session.query(Candle)
                .filter(Candle.symbol == self.symbol, Candle.timeframe == candle.timeframe, Candle.timestamp == candle.timestamp)
                .first()
            )
            if existing is not None:
                self.idempotent_skips += 1
                return
            session.add(
                Candle(
                    symbol=self.symbol,
                    timeframe=candle.timeframe,
                    timestamp=candle.timestamp,
                    open_price=candle.open_price,
                    high_price=candle.high_price,
                    low_price=candle.low_price,
                    close_price=candle.close_price,
                    volume=candle.volume,
                    instrument_token=self.instrument_token,
                    receive_timestamp=candle.receive_timestamp,
                    timestamp_source="exchange_timestamp",
                    is_generated=1 if candle.is_generated else 0,
                    data_quality=candle.data_quality,
                )
            )
            session.commit()
            self.persisted_candles += 1
        except Exception:
            session.rollback()
            logger.exception("canonical_underlying_candle_persist_failed timeframe=%s timestamp=%s", candle.timeframe, candle.timestamp)
        finally:
            session.close()

    def _recover_last_completed(self) -> None:
        now = ist_now_naive()
        session = get_session()
        try:
            row = (
                session.query(Candle)
                .filter(Candle.symbol == self.symbol, Candle.timeframe == "1minute", Candle.timestamp >= now.replace(hour=0, minute=0, second=0, microsecond=0))
                .order_by(Candle.timestamp.desc())
                .first()
            )
            if row is not None:
                self._last_completed_1m = _BuildingCandle(
                    timeframe="1minute",
                    timestamp=row.timestamp.replace(tzinfo=None),
                    open_price=float(row.open_price),
                    high_price=float(row.high_price),
                    low_price=float(row.low_price),
                    close_price=float(row.close_price),
                    volume=float(row.volume or 0.0),
                    receive_timestamp=getattr(row, "receive_timestamp", None),
                    is_generated=bool(getattr(row, "is_generated", 0)),
                    data_quality=str(getattr(row, "data_quality", None) or "recovered_completed_candle"),
                )
                bucket = row.timestamp.replace(minute=(row.timestamp.minute // 5) * 5, second=0, microsecond=0)
                bucket_rows = (
                    session.query(Candle)
                    .filter(
                        Candle.symbol == self.symbol,
                        Candle.timeframe == "1minute",
                        Candle.timestamp >= bucket,
                        Candle.timestamp < bucket + timedelta(minutes=5),
                    )
                    .order_by(Candle.timestamp.asc())
                    .all()
                )
                if bucket_rows:
                    self._five_minute = _BuildingCandle(
                        timeframe="5minute",
                        timestamp=bucket,
                        open_price=float(bucket_rows[0].open_price),
                        high_price=max(float(item.high_price) for item in bucket_rows),
                        low_price=min(float(item.low_price) for item in bucket_rows),
                        close_price=float(bucket_rows[-1].close_price),
                        volume=sum(float(item.volume or 0.0) for item in bucket_rows),
                        receive_timestamp=getattr(bucket_rows[-1], "receive_timestamp", None),
                        is_generated=all(bool(getattr(item, "is_generated", 0)) for item in bucket_rows),
                        data_quality="recovered_completed_1m_aggregate",
                    )
        finally:
            session.close()

    def _volume_increment(self, cumulative: float | None) -> float:
        current = max(0.0, float(cumulative or 0.0))
        previous = self._last_cumulative_volume
        self._last_cumulative_volume = current
        if previous is None or current < previous:
            return 0.0
        return max(0.0, current - previous)

    def _new_live_candle(self, timeframe: str, timestamp: datetime, tick: WebSocketTick, volume: float) -> _BuildingCandle:
        return _BuildingCandle(
            timeframe=timeframe,
            timestamp=timestamp,
            open_price=float(tick.price),
            high_price=float(tick.price),
            low_price=float(tick.price),
            close_price=float(tick.price),
            volume=volume,
            receive_timestamp=(tick.receive_timestamp or ist_now_naive()).replace(tzinfo=None),
        )

    def _update(self, candle: _BuildingCandle, tick: WebSocketTick, volume: float) -> None:
        candle.high_price = max(candle.high_price, float(tick.price))
        candle.low_price = min(candle.low_price, float(tick.price))
        candle.close_price = float(tick.price)
        candle.volume += volume
        candle.receive_timestamp = (tick.receive_timestamp or ist_now_naive()).replace(tzinfo=None)

    def _reject(self, reason: str) -> dict[str, Any]:
        self.rejected_ticks += 1
        self.last_rejection_reason = reason
        return {"accepted": False, "reason": reason}

    def _building_dict(self, candle: _BuildingCandle) -> dict[str, Any]:
        return {
            "timeframe": candle.timeframe,
            "timestamp": candle.timestamp.isoformat(sep=" "),
            "close": candle.close_price,
            "volume": candle.volume,
            "is_generated": candle.is_generated,
            "data_quality": candle.data_quality,
            "timestamp_source": "exchange_timestamp",
        }

    def _candle_dict(self, candle: Candle) -> dict[str, Any]:
        return {
            "timestamp": candle.timestamp.isoformat(sep=" "),
            "open": candle.open_price,
            "high": candle.high_price,
            "low": candle.low_price,
            "close": candle.close_price,
            "volume": candle.volume,
            "receive_timestamp": candle.receive_timestamp.isoformat(sep=" ") if candle.receive_timestamp else None,
            "timestamp_source": candle.timestamp_source,
            "is_generated": bool(candle.is_generated),
            "data_quality": candle.data_quality,
        }
