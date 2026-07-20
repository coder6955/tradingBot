from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime
from threading import RLock
from typing import Any

from app.config import settings
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


class LatencyMetricsService:
    """In-memory bounded latency telemetry for the trading critical path."""

    def __init__(self, *, sample_limit: int | None = None) -> None:
        self.sample_limit = max(10, int(sample_limit or settings.latency_sample_limit))
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.sample_limit))
        self._last_events: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self.dropped_event_count = 0
        self.critical_dropped_event_count = 0

    def record(self, name: str, milliseconds: float, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        value = max(0.0, float(milliseconds))
        event = {
            "metric": str(name),
            "milliseconds": round(value, 4),
            "recorded_at": ist_now_naive().isoformat(sep=" "),
            "detail": dict(detail or {}),
            **current_strategy_lineage(),
        }
        with self._lock:
            self._samples[str(name)].append(value)
            self._last_events[str(name)] = event
        return event

    def record_between(
        self,
        name: str,
        start: datetime | None,
        end: datetime | None = None,
        *,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if start is None:
            return None
        finish = (end or ist_now_naive()).replace(tzinfo=None)
        beginning = start.replace(tzinfo=None)
        return self.record(name, max(0.0, (finish - beginning).total_seconds() * 1000.0), detail=detail)

    def record_queue_drop(self, *, critical: bool, detail: dict[str, Any]) -> None:
        with self._lock:
            self.dropped_event_count += 1
            if critical:
                self.critical_dropped_event_count += 1
        self.record("queue_event_drop", 0.0, detail={**detail, "critical": critical})

    def report(self) -> dict[str, Any]:
        with self._lock:
            metrics = {
                name: self._summary(list(values), self._last_events.get(name))
                for name, values in sorted(self._samples.items())
            }
            dropped = self.dropped_event_count
            critical = self.critical_dropped_event_count
        return {
            "status": "critical" if critical else "ok",
            "metrics": metrics,
            "dropped_event_count": dropped,
            "critical_dropped_event_count": critical,
            "sample_limit_per_metric": self.sample_limit,
            **current_strategy_lineage(),
        }

    def _summary(self, values: list[float], last_event: dict[str, Any] | None) -> dict[str, Any]:
        ordered = sorted(values)
        return {
            "sample_size": len(ordered),
            "p50_ms": self._percentile(ordered, 0.50),
            "p95_ms": self._percentile(ordered, 0.95),
            "p99_ms": self._percentile(ordered, 0.99),
            "max_ms": round(max(ordered), 4) if ordered else None,
            "last_event": dict(last_event) if last_event else None,
        }

    def _percentile(self, ordered: list[float], quantile: float) -> float | None:
        if not ordered:
            return None
        index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
        return round(float(ordered[index]), 4)
