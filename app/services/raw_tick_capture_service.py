from __future__ import annotations

import json
import logging
import queue
import time
from datetime import datetime, timedelta
from itertools import count
from threading import Event, RLock, Thread
from typing import Any

from app.config import settings
from app.services.database import RawTickRecord, get_session
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


logger = logging.getLogger(__name__)


class RawTickCaptureService:
    """Bounded asynchronous persistence for replay-relevant market ticks."""

    RISK_OWNER_PREFIXES = ("armed:", "active_trade")
    CAPTURE_OWNER_PREFIXES = (
        "core_market",
        "banknifty_prewarm",
        "armed:",
        "active_trade",
    )

    def __init__(self, *, queue_size: int | None = None) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(1, int(queue_size or settings.raw_tick_queue_size))
        )
        self._stop = Event()
        self._worker: Thread | None = None
        self._sequence = count(time.time_ns())
        self._lock = RLock()
        self.captured_count = 0
        self.persisted_count = 0
        self.dropped_warm_count = 0
        self.dropped_risk_count = 0
        self.evicted_warm_count = 0
        self.persist_failure_count = 0
        self.last_capture_gap: dict[str, Any] | None = None
        self.last_persisted_at: datetime | None = None
        self.last_cleanup_at: datetime | None = None
        self.last_cleanup_deleted = 0

    def start(self) -> dict[str, Any]:
        if not settings.enable_raw_tick_capture:
            return {"started": False, "reason": "raw_tick_capture_disabled"}
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return {"started": True, "already_running": True}
            self._stop.clear()
            self._worker = Thread(
                target=self._run, name="raw-tick-persistence", daemon=True
            )
            self._worker.start()
        return {"started": True}

    def stop(self, *, flush_timeout_seconds: float = 5.0) -> dict[str, Any]:
        self.flush(timeout_seconds=flush_timeout_seconds)
        self._stop.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=max(0.1, flush_timeout_seconds))
        return {"stopped": True, **self.status()}

    def capture(
        self,
        tick: WebSocketTick,
        *,
        symbol: str | None,
        owners: list[str] | tuple[str, ...] | set[str],
    ) -> dict[str, Any]:
        if not settings.enable_raw_tick_capture:
            return {"captured": False, "reason": "raw_tick_capture_disabled"}
        owner_list = sorted({str(owner) for owner in owners if str(owner)})
        if not self._capture_relevant(owner_list):
            return {"captured": False, "reason": "tick_context_not_relevant"}
        if self._worker is None or not self._worker.is_alive():
            self.start()
        risk_sensitive = any(
            owner.startswith(self.RISK_OWNER_PREFIXES) for owner in owner_list
        )
        lineage = current_strategy_lineage()
        receive = (tick.receive_timestamp or ist_now_naive()).replace(tzinfo=None)
        exchange = (
            tick.timestamp.replace(tzinfo=None)
            if tick.timestamp_source != "local_receive_time"
            else None
        )
        item = {
            "sequence": next(self._sequence),
            "session_date": receive.date().isoformat(),
            "instrument_token": int(tick.instrument_token),
            "symbol": str(symbol or f"TOKEN:{int(tick.instrument_token)}").upper(),
            "last_price": float(tick.price),
            "bid": float(tick.bid) if tick.bid is not None else None,
            "ask": float(tick.ask) if tick.ask is not None else None,
            "depth_json": json.dumps(
                {"buy": list(tick.buy_depth), "sell": list(tick.sell_depth)},
                default=str,
            ),
            "cumulative_volume": float(tick.volume)
            if tick.volume is not None
            else None,
            "exchange_timestamp": exchange,
            "receive_timestamp": receive,
            "timestamp_source": str(tick.timestamp_source),
            "packet_type": str(tick.packet_type),
            "owners_json": json.dumps(owner_list),
            "capture_context": "risk_sensitive" if risk_sensitive else "warm_market",
            "strategy_version": lineage["strategy_version"],
            "config_hash": lineage["config_hash"],
            "risk_sensitive": risk_sensitive,
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            if risk_sensitive and self._evict_one_warm_item():
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    return self._record_drop(item, risk_sensitive=True)
            else:
                return self._record_drop(item, risk_sensitive=risk_sensitive)
        with self._lock:
            self.captured_count += 1
        return {
            "captured": True,
            "sequence": item["sequence"],
            "risk_sensitive": risk_sensitive,
        }

    def flush(self, *, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def cleanup_retention(self, *, now: datetime | None = None) -> dict[str, Any]:
        cutoff = (now or ist_now_naive()).replace(tzinfo=None) - timedelta(
            days=max(1, int(settings.raw_tick_retention_days))
        )
        session = get_session()
        try:
            deleted = int(
                session.query(RawTickRecord)
                .filter(RawTickRecord.receive_timestamp < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
        finally:
            session.close()
        self.last_cleanup_at = now or ist_now_naive()
        self.last_cleanup_deleted = deleted
        return {"deleted": deleted, "cutoff": cutoff.isoformat(sep=" ")}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": settings.enable_raw_tick_capture,
            "running": bool(self._worker and self._worker.is_alive()),
            "queue_size": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "captured_count": self.captured_count,
            "persisted_count": self.persisted_count,
            "dropped_warm_count": self.dropped_warm_count,
            "dropped_risk_count": self.dropped_risk_count,
            "evicted_warm_count": self.evicted_warm_count,
            "persist_failure_count": self.persist_failure_count,
            "capture_gap_critical": self.dropped_risk_count > 0,
            "last_capture_gap": dict(self.last_capture_gap)
            if self.last_capture_gap
            else None,
            "last_persisted_at": self.last_persisted_at.isoformat(sep=" ")
            if self.last_persisted_at
            else None,
            "last_cleanup_at": self.last_cleanup_at.isoformat(sep=" ")
            if self.last_cleanup_at
            else None,
            "last_cleanup_deleted": self.last_cleanup_deleted,
            "retention_days": settings.raw_tick_retention_days,
        }

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            batch: list[dict[str, Any]] = []
            try:
                batch.append(self._queue.get(timeout=0.2))
            except queue.Empty:
                continue
            while len(batch) < max(1, int(settings.raw_tick_batch_size)):
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._persist(batch)
            for _ in batch:
                self._queue.task_done()

    def _persist(self, batch: list[dict[str, Any]]) -> None:
        session = get_session()
        try:
            for item in batch:
                payload = {
                    key: value for key, value in item.items() if key != "risk_sensitive"
                }
                session.add(RawTickRecord(**payload))
            session.commit()
            with self._lock:
                self.persisted_count += len(batch)
                self.last_persisted_at = ist_now_naive()
        except Exception:
            session.rollback()
            with self._lock:
                self.persist_failure_count += len(batch)
            logger.exception("raw_tick_batch_persistence_failed count=%s", len(batch))
        finally:
            session.close()

    def _capture_relevant(self, owners: list[str]) -> bool:
        return any(
            any(owner.startswith(prefix) for prefix in self.CAPTURE_OWNER_PREFIXES)
            for owner in owners
        )

    def _evict_one_warm_item(self) -> bool:
        with self._queue.mutex:
            for index, queued in enumerate(self._queue.queue):
                if not bool(queued.get("risk_sensitive")):
                    del self._queue.queue[index]
                    self._queue.unfinished_tasks = max(
                        0, self._queue.unfinished_tasks - 1
                    )
                    self._queue.not_full.notify()
                    with self._lock:
                        self.evicted_warm_count += 1
                        self.dropped_warm_count += 1
                    return True
        return False

    def _record_drop(
        self, item: dict[str, Any], *, risk_sensitive: bool
    ) -> dict[str, Any]:
        with self._lock:
            if risk_sensitive:
                self.dropped_risk_count += 1
            else:
                self.dropped_warm_count += 1
            self.last_capture_gap = {
                "instrument_token": item["instrument_token"],
                "symbol": item["symbol"],
                "receive_timestamp": item["receive_timestamp"].isoformat(sep=" "),
                "risk_sensitive": risk_sensitive,
                "reason": "raw_tick_queue_full",
            }
        if risk_sensitive:
            logger.critical("risk_sensitive_raw_tick_dropped %s", self.last_capture_gap)
        return {
            "captured": False,
            "reason": "raw_tick_queue_full",
            "risk_sensitive": risk_sensitive,
        }
