from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Iterator


@dataclass
class _PathCounter:
    path: str
    rest_calls: int = 0
    database_queries: int = 0
    details: list[str] = field(default_factory=list)


class IoCallMetricsService:
    """Count synchronous I/O performed inside one decision path."""

    def __init__(self) -> None:
        self._current: ContextVar[_PathCounter | None] = ContextVar("decision_io_counter", default=None)
        self._last: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    @contextmanager
    def measure(self, path: str) -> Iterator[_PathCounter]:
        counter = _PathCounter(path=str(path))
        token = self._current.set(counter)
        try:
            yield counter
        finally:
            self._current.reset(token)
            with self._lock:
                self._last[counter.path] = self._payload(counter)

    def record_rest(self, operation: str) -> None:
        counter = self._current.get()
        if counter is not None:
            counter.rest_calls += 1
            self._append_detail(counter, f"rest:{operation}")

    def record_database(self, operation: str = "query") -> None:
        counter = self._current.get()
        if counter is not None:
            counter.database_queries += 1
            self._append_detail(counter, f"db:{operation}")

    def current(self) -> dict[str, Any]:
        counter = self._current.get()
        if counter is None:
            return {"path": None, "rest_calls": 0, "database_queries": 0, "details": []}
        return self._payload(counter)

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {name: dict(payload) for name, payload in self._last.items()}

    def _payload(self, counter: _PathCounter) -> dict[str, Any]:
        return {
            "path": counter.path,
            "rest_calls": int(counter.rest_calls),
            "database_queries": int(counter.database_queries),
            "details": list(counter.details),
        }

    def _append_detail(self, counter: _PathCounter, detail: str) -> None:
        if len(counter.details) < 50:
            counter.details.append(detail)


io_call_metrics = IoCallMetricsService()
