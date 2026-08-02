from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services.decision_evidence_repository import DecisionEvidenceRepository
from app.services.episode_reservation_service import EpisodeReservationService


@dataclass(frozen=True)
class EvidenceQueueItem:
    event_id: str
    episode_key: str | None
    payload: dict[str, Any]
    attempt: int = 0


class EvidencePersistenceQueue:
    """Bounded non-critical persistence worker with explicit failure accounting.

    Safety decisions and atomic order intent are persisted before this queue is
    used. Queue rejection is visible to callers; required audit evidence may be
    used by the caller as a fail-closed reason before any order is submitted.
    """

    def __init__(
        self,
        *,
        repository: DecisionEvidenceRepository | None = None,
        episode_service: EpisodeReservationService | None = None,
        max_size: int | None = None,
        max_retries: int | None = None,
        start_worker: bool = True,
    ) -> None:
        self.repository = repository or DecisionEvidenceRepository()
        self.episode_service = episode_service or EpisodeReservationService()
        self.max_size = max(1, int(max_size or settings.evidence_queue_max_size))
        self.max_retries = max(
            0,
            int(
                max_retries
                if max_retries is not None
                else settings.evidence_queue_max_retries
            ),
        )
        self._queue: queue.Queue[EvidenceQueueItem] = queue.Queue(maxsize=self.max_size)
        self._dead_letters: deque[dict[str, Any]] = deque(
            maxlen=max(100, self.max_size)
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.enqueued_count = 0
        self.persisted_count = 0
        self.retry_count = 0
        self.queue_full_count = 0
        self.failure_count = 0
        self.recovered_count = 0
        self.last_error: str | None = None
        if start_worker:
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._run, name="decision-evidence-writer", daemon=True
            )
            self._worker.start()

    def enqueue_decision(
        self,
        payload: dict[str, Any],
        *,
        episode_key: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        item = EvidenceQueueItem(
            event_id=str(event_id or uuid.uuid4().hex),
            episode_key=episode_key,
            payload=dict(payload),
        )
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self.queue_full_count += 1
                self.last_error = "EVIDENCE_QUEUE_FULL"
            return {
                "accepted": False,
                "event_id": item.event_id,
                "decision_id": item.event_id,
                "reason": "EVIDENCE_QUEUE_FULL",
            }
        with self._lock:
            self.enqueued_count += 1
        return {
            "accepted": True,
            "event_id": item.event_id,
            "decision_id": item.event_id,
            "reason": None,
        }

    def recover_pending_order_evidence(self) -> int:
        recovered = 0
        for intent in self.episode_service.recoverable_order_intents():
            event_id = str(intent.get("evidence_event_id") or "")
            if not event_id or str(intent.get("evidence_status") or "") == "PERSISTED":
                continue
            receipt = self.enqueue_decision(
                {
                    "decision_type": "pre_order_recovered",
                    "final_state": str(
                        intent.get("canonical_state") or "ORDER_PENDING"
                    ),
                    "episode_key": intent.get("episode_key"),
                    "context": {
                        "recovered_order_intent": intent.get("order_intent") or {}
                    },
                    "gate_results": {"recovered_after_process_interruption": True},
                },
                episode_key=str(intent.get("episode_key") or "") or None,
                event_id=event_id,
            )
            if receipt["accepted"]:
                recovered += 1
        with self._lock:
            self.recovered_count += recovered
        return recovered

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=max(0.0, float(timeout)))

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "queue_size": self._queue.qsize(),
                "queue_capacity": self.max_size,
                "unfinished_tasks": self._queue.unfinished_tasks,
                "enqueued_count": self.enqueued_count,
                "persisted_count": self.persisted_count,
                "retry_count": self.retry_count,
                "queue_full_count": self.queue_full_count,
                "failure_count": self.failure_count,
                "recovered_count": self.recovered_count,
                "dead_letter_count": len(self._dead_letters),
                "dead_letters": list(self._dead_letters)[-10:],
                "last_error": self.last_error,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
            }

    def _run(self) -> None:
        while not self._stop.is_set() or self._queue.unfinished_tasks:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._persist(item)
            finally:
                self._queue.task_done()

    def _persist(self, item: EvidenceQueueItem) -> None:
        try:
            self.repository.record_decision(**item.payload, decision_id=item.event_id)
            if item.episode_key:
                self.episode_service.mark_evidence_status(
                    item.episode_key, item.event_id, "PERSISTED"
                )
            with self._lock:
                self.persisted_count += 1
                self.last_error = None
        except Exception as exc:
            with self._lock:
                self.failure_count += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            if item.attempt < self.max_retries and not self._stop.is_set():
                retry = EvidenceQueueItem(
                    item.event_id, item.episode_key, item.payload, item.attempt + 1
                )
                try:
                    self._queue.put_nowait(retry)
                    with self._lock:
                        self.retry_count += 1
                    return
                except queue.Full:
                    with self._lock:
                        self.queue_full_count += 1
            with self._lock:
                self._dead_letters.append(
                    {
                        "event_id": item.event_id,
                        "episode_key": item.episode_key,
                        "attempts": item.attempt + 1,
                        "error": self.last_error,
                    }
                )
            if item.episode_key:
                try:
                    self.episode_service.mark_evidence_status(
                        item.episode_key, item.event_id, "DEAD_LETTER"
                    )
                except Exception:
                    pass
