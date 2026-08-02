from __future__ import annotations

import json
import queue
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import settings
from app.services.database import EpisodeObservationRecord, get_session
from app.services.decision_evidence_repository import DecisionEvidenceRepository
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.time_utils import ist_now_naive


TRACKED_STATES = {
    "OBSERVE",
    "PREPARED",
    "ARMED",
    "TRIGGERED",
    "REJECTED",
    "TOO_LATE",
    "ORDER_PENDING",
    "OPEN",
    "INVALIDATED",
    "EXPIRED",
}


@dataclass(frozen=True)
class MarketPathEvent:
    instrument_token: int
    exchange_timestamp: datetime
    receive_timestamp: datetime
    bid: float | None = None
    ask: float | None = None
    ltp: float | None = None
    bid_depth: int | None = None
    ask_depth: int | None = None
    source: str = "websocket"
    provenance: str = "genuine"
    candle_state: str = "tick"
    feed_gap: bool = False
    reconnect: bool = False


@dataclass
class EpisodePathState:
    episode_key: str
    context: dict[str, Any]
    state: str
    underlying_token: int | None
    option_token: int | None
    first_observed_at: datetime
    transition_times: dict[str, datetime] = field(default_factory=dict)
    option_events: list[MarketPathEvent] = field(default_factory=list)
    underlying_events: list[MarketPathEvent] = field(default_factory=list)
    event_keys: set[tuple[Any, ...]] = field(default_factory=set)
    completed_horizons: set[str] = field(default_factory=set)
    first_executable_at: datetime | None = None
    hypothetical_entry: float | None = None
    first_executable_ask: float | None = None
    first_target_at: datetime | None = None
    first_stop_at: datetime | None = None
    dropped_event_count: int = 0
    missing_intervals: list[dict[str, Any]] = field(default_factory=list)
    last_option_event_at: datetime | None = None


class EpisodeOutcomeCollector:
    """Asynchronously follows one executable market path per unique episode."""

    def __init__(
        self,
        *,
        repository: DecisionEvidenceRepository | None = None,
        max_queue_size: int | None = None,
        start_worker: bool = True,
        persist_observations: bool = True,
        latency_metrics: Any | None = None,
        shadow_policy_service: Any | None = None,
    ) -> None:
        self.repository = repository or DecisionEvidenceRepository()
        self.max_queue_size = max(1, int(max_queue_size or settings.outcome_queue_max_size))
        self.horizons = self._configured_horizons()
        self.persist_observations = bool(persist_observations)
        self.latency_metrics = latency_metrics
        self.shadow_policy_service = shadow_policy_service
        self._queue: queue.Queue[MarketPathEvent] = queue.Queue(maxsize=self.max_queue_size)
        self._trackers: dict[str, EpisodePathState] = {}
        self._token_index: dict[int, set[str]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.enqueued_count = 0
        self.processed_count = 0
        self.queue_full_count = 0
        self.persistence_failure_count = 0
        self.persistence_retry_count = 0
        self.dead_letter_count = 0
        self._dead_letters: list[dict[str, Any]] = []
        self.last_error: str | None = None
        if start_worker:
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="episode-outcome-collector", daemon=True)
            self._worker.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=max(0.0, float(timeout)))

    def flush(self, timeout: float = 5.0) -> bool:
        import time as time_module

        deadline = time_module.monotonic() + max(0.0, float(timeout))
        while self._queue.unfinished_tasks and time_module.monotonic() < deadline:
            time_module.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def register_episode(
        self,
        episode_key: str,
        *,
        context: dict[str, Any],
        state: str,
        observed_at: datetime | None = None,
        persist_immediately: bool = False,
    ) -> dict[str, Any]:
        normalized_state = str(state).upper()
        if normalized_state not in TRACKED_STATES:
            raise ValueError(f"unsupported outcome-collector state: {normalized_state}")
        timestamp = self._naive(observed_at or ist_now_naive())
        underlying_token = self._optional_int(context.get("underlying_token"))
        option_token = self._optional_int(context.get("option_token") or context.get("instrument_token"))
        with self._lock:
            tracker = self._trackers.get(episode_key)
            created = tracker is None
            if tracker is None:
                tracker = EpisodePathState(
                    episode_key=episode_key,
                    context=dict(context),
                    state=normalized_state,
                    underlying_token=underlying_token,
                    option_token=option_token,
                    first_observed_at=timestamp,
                )
                self._trackers[episode_key] = tracker
                for token in {underlying_token, option_token} - {None}:
                    self._token_index.setdefault(int(token), set()).add(episode_key)
            else:
                tracker.context.update(context)
                tracker.state = normalized_state
            self._record_transition_locked(tracker, normalized_state, timestamp)
        if self.persist_observations and persist_immediately:
            self._persist_observation(tracker)
        return {"episode_key": episode_key, "created": created, "state": normalized_state}

    def transition(self, episode_key: str, state: str, *, timestamp: datetime | None = None) -> bool:
        normalized = str(state).upper()
        if normalized not in TRACKED_STATES:
            raise ValueError(f"unsupported outcome-collector state: {normalized}")
        with self._lock:
            tracker = self._trackers.get(episode_key)
            if tracker is None:
                return False
            tracker.state = normalized
            self._record_transition_locked(tracker, normalized, self._naive(timestamp or ist_now_naive()))
        if self.persist_observations:
            self._persist_observation(tracker)
        return True

    def on_tick(self, tick: WebSocketTick) -> dict[str, Any]:
        event = MarketPathEvent(
            instrument_token=int(tick.instrument_token),
            exchange_timestamp=self._naive(tick.timestamp),
            receive_timestamp=self._naive(tick.receive_timestamp or ist_now_naive()),
            bid=self._positive(tick.bid),
            ask=self._positive(tick.ask),
            ltp=self._positive(tick.price),
            bid_depth=sum(int(item.get("quantity") or 0) for item in tick.buy_depth if isinstance(item, dict)) or None,
            ask_depth=sum(int(item.get("quantity") or 0) for item in tick.sell_depth if isinstance(item, dict)) or None,
            source=str(tick.timestamp_source or "websocket"),
            provenance="websocket",
            candle_state="tick",
        )
        return self.enqueue(event)

    def enqueue(self, event: MarketPathEvent) -> dict[str, Any]:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self.queue_full_count += 1
                for episode_key in self._token_index.get(int(event.instrument_token), set()):
                    tracker = self._trackers.get(episode_key)
                    if tracker is not None:
                        tracker.dropped_event_count += 1
                self.last_error = "OUTCOME_QUEUE_FULL"
            return {"accepted": False, "reason": "OUTCOME_QUEUE_FULL"}
        with self._lock:
            self.enqueued_count += 1
        return {"accepted": True, "reason": None}

    def process_event(self, event: MarketPathEvent) -> int:
        started = time_module.perf_counter()
        with self._lock:
            episode_keys = list(self._token_index.get(int(event.instrument_token), set()))
        processed = 0
        for episode_key in episode_keys:
            with self._lock:
                tracker = self._trackers.get(episode_key)
                if tracker is None or not self._append_event_locked(tracker, event):
                    continue
                due = self._due_horizons_locked(tracker, event.exchange_timestamp)
            for horizon in due:
                if self.persist_observations:
                    self._persist_horizon(tracker, horizon)
            self._compare_shadow_policies(tracker)
            if self.persist_observations:
                self._persist_observation(tracker)
            processed += 1
        with self._lock:
            self.processed_count += processed
        if self.latency_metrics is not None:
            self.latency_metrics.record(
                "outcome_collector_processing_duration",
                (time_module.perf_counter() - started) * 1000.0,
                detail={"instrument_token": event.instrument_token, "matched_episodes": processed},
            )
        return processed

    def _compare_shadow_policies(self, tracker: EpisodePathState) -> None:
        payload = tracker.context.get("shadow_policy_context")
        if self.shadow_policy_service is None or not isinstance(payload, dict):
            return
        try:
            from app.services.entry_policy_shadow_service import EntryPolicyContext

            context_values = dict(payload)
            observed_at = context_values.get("observed_at")
            if not isinstance(observed_at, datetime):
                context_values["observed_at"] = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00")).replace(tzinfo=None)
            events = sorted(
                [*tracker.underlying_events, *tracker.option_events],
                key=lambda item: (item.exchange_timestamp, item.receive_timestamp),
            )
            self.shadow_policy_service.compare(EntryPolicyContext(**context_values), events, persist=self.persist_observations)
        except Exception as exc:
            with self._lock:
                self.persistence_failure_count += 1
                self.last_error = f"shadow_policy_comparison:{type(exc).__name__}: {exc}"

    def snapshot(self, episode_key: str) -> dict[str, Any] | None:
        with self._lock:
            tracker = self._trackers.get(episode_key)
            return self._summary_locked(tracker, None) if tracker is not None else None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tracked_episodes": len(self._trackers),
                "queue_size": self._queue.qsize(),
                "queue_capacity": self.max_queue_size,
                "enqueued_count": self.enqueued_count,
                "processed_count": self.processed_count,
                "queue_full_count": self.queue_full_count,
                "persistence_failure_count": self.persistence_failure_count,
                "persistence_retry_count": self.persistence_retry_count,
                "dead_letter_count": self.dead_letter_count,
                "dead_letters": list(self._dead_letters[-20:]),
                "last_error": self.last_error,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
            }

    def _run(self) -> None:
        while not self._stop.is_set() or self._queue.unfinished_tasks:
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self.process_event(event)
            except Exception as exc:
                with self._lock:
                    self.persistence_failure_count += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()

    def _append_event_locked(self, tracker: EpisodePathState, event: MarketPathEvent) -> bool:
        key = (
            event.instrument_token,
            event.exchange_timestamp,
            event.receive_timestamp,
            event.bid,
            event.ask,
            event.ltp,
            event.source,
        )
        if key in tracker.event_keys:
            return False
        tracker.event_keys.add(key)
        if event.instrument_token == tracker.option_token:
            if tracker.last_option_event_at is not None:
                gap = (event.exchange_timestamp - tracker.last_option_event_at).total_seconds()
                if gap > float(settings.outcome_missing_interval_seconds):
                    tracker.missing_intervals.append(
                        {"start": tracker.last_option_event_at.isoformat(), "end": event.exchange_timestamp.isoformat(), "seconds": gap}
                    )
            tracker.last_option_event_at = event.exchange_timestamp
            tracker.option_events.append(event)
            if tracker.hypothetical_entry is None and event.ask is not None:
                tracker.first_executable_at = event.exchange_timestamp
                tracker.first_executable_ask = event.ask
                tracker.hypothetical_entry = self._entry_value(event.ask)
                tracker.transition_times.setdefault("FIRST_EXECUTABLE", event.exchange_timestamp)
            target = self._float(tracker.context.get("target_1"))
            stop = self._float(tracker.context.get("stop_loss"))
            if event.bid is not None:
                if target > 0 and event.bid >= target and tracker.first_target_at is None:
                    tracker.first_target_at = event.exchange_timestamp
                if stop > 0 and event.bid <= stop and tracker.first_stop_at is None:
                    tracker.first_stop_at = event.exchange_timestamp
        elif event.instrument_token == tracker.underlying_token:
            tracker.underlying_events.append(event)
        return True

    def _due_horizons_locked(self, tracker: EpisodePathState, current: datetime) -> list[str]:
        due: list[str] = []
        elapsed = (current - tracker.first_observed_at).total_seconds()
        for name, seconds in self.horizons.items():
            if name not in tracker.completed_horizons and elapsed >= seconds:
                tracker.completed_horizons.add(name)
                due.append(name)
        resolution = "original_stop_or_target"
        if resolution not in tracker.completed_horizons and (tracker.first_target_at or tracker.first_stop_at):
            tracker.completed_horizons.add(resolution)
            due.append(resolution)
        cutoff = self._session_cutoff(current)
        if "session_cutoff" not in tracker.completed_horizons and current >= cutoff:
            tracker.completed_horizons.add("session_cutoff")
            due.append("session_cutoff")
        return due

    def _persist_horizon(self, tracker: EpisodePathState, horizon: str) -> None:
        with self._lock:
            outcome = self._summary_locked(tracker, horizon)
        self._persist_with_retry(
            "horizon",
            tracker.episode_key,
            lambda: self.repository.record_outcome(
                episode_key=tracker.episode_key,
                horizon=horizon,
                outcome_source="continuous_executable_collector",
                outcome=outcome,
            ),
            metadata={"horizon": horizon},
        )

    def _summary_locked(self, tracker: EpisodePathState | None, horizon: str | None) -> dict[str, Any]:
        if tracker is None:
            return {}
        cutoff = self._horizon_cutoff(tracker, horizon)
        option_events = [event for event in tracker.option_events if cutoff is None or event.exchange_timestamp <= cutoff]
        underlying_events = [event for event in tracker.underlying_events if cutoff is None or event.exchange_timestamp <= cutoff]
        executable = [event for event in option_events if event.bid is not None]
        option_count = len(option_events)
        coverage_pct = (len(executable) / option_count * 100.0) if option_count else 0.0
        entry = tracker.hypothetical_entry
        net_values = [(event.exchange_timestamp, self._exit_value(float(event.bid)) - entry) for event in executable] if entry is not None else []
        mfe = max(net_values, key=lambda item: item[1]) if net_values else None
        mae = min(net_values, key=lambda item: item[1]) if net_values else None
        latest = executable[-1] if executable else None
        best = max(executable, key=lambda item: float(item.bid or 0.0)) if executable else None
        target = self._float(tracker.context.get("target_1"))
        stop = self._float(tracker.context.get("stop_loss"))
        ltp_target_without_bid = any((event.ltp or 0.0) >= target and event.bid is None for event in option_events) if target > 0 else False
        ltp_stop_without_bid = any((event.ltp or float("inf")) <= stop and event.bid is None for event in option_events) if stop > 0 else False
        sufficient = bool(
            entry is not None
            and executable
            and coverage_pct >= float(settings.outcome_min_executable_coverage_pct)
            and tracker.dropped_event_count == 0
        )
        target_before_stop = bool(
            tracker.first_target_at
            and (tracker.first_stop_at is None or tracker.first_target_at < tracker.first_stop_at)
        )
        stop_before_target = bool(
            tracker.first_stop_at
            and (tracker.first_target_at is None or tracker.first_stop_at < tracker.first_target_at)
        )
        if not sufficient:
            classification = "UNDETERMINABLE_DATA"
        elif target_before_stop:
            classification = "TARGET_BEFORE_STOP"
        elif stop_before_target:
            classification = "STOP_BEFORE_TARGET"
        else:
            classification = "NEITHER_REACHED"
        underlying_ltps = [float(event.ltp) for event in underlying_events if event.ltp is not None]
        option_bids = [float(event.bid) for event in executable if event.bid is not None]
        return {
            "episode_key": tracker.episode_key,
            "state": tracker.state,
            "horizon": horizon,
            "executable_data_sufficient": sufficient,
            "classification": classification,
            "hypothetical_entry_ask": tracker.first_executable_ask,
            "hypothetical_entry_after_cost": round(entry, 4) if entry is not None else None,
            "latest_executable_bid": latest.bid if latest else None,
            "latest_exit_after_cost": round(self._exit_value(float(latest.bid)), 4) if latest else None,
            "after_cost_result_per_unit": round(self._exit_value(float(latest.bid)) - entry, 4) if latest and entry is not None else None,
            "after_cost_result": (
                round((self._exit_value(float(latest.bid)) - entry) * int(tracker.context.get("quantity") or 1), 2)
                if latest and entry is not None
                else None
            ),
            "best_executable_bid": best.bid if best else None,
            "best_exit_after_cost": round(self._exit_value(float(best.bid)), 4) if best else None,
            "net_mfe_per_unit": round(mfe[1], 4) if mfe else None,
            "net_mae_per_unit": round(mae[1], 4) if mae else None,
            "time_to_mfe_seconds": round((mfe[0] - tracker.first_observed_at).total_seconds(), 3) if mfe else None,
            "time_to_mae_seconds": round((mae[0] - tracker.first_observed_at).total_seconds(), 3) if mae else None,
            "first_target_at": tracker.first_target_at.isoformat() if tracker.first_target_at else None,
            "first_stop_at": tracker.first_stop_at.isoformat() if tracker.first_stop_at else None,
            "target_before_stop": target_before_stop if sufficient else None,
            "stop_before_target": stop_before_target if sufficient else None,
            "ltp_target_without_executable_bid": ltp_target_without_bid,
            "ltp_stop_without_executable_bid": ltp_stop_without_bid,
            "market_moved_without_executable_quote": bool(underlying_events and not option_events),
            "option_ltp_moved_without_executable_bid": bool(option_events and not executable),
            "option_event_count": option_count,
            "executable_bid_event_count": len(executable),
            "executable_coverage_percent": round(coverage_pct, 2),
            "missing_intervals": list(tracker.missing_intervals),
            "dropped_event_count": tracker.dropped_event_count,
            "feed_gap_count": sum(1 for event in option_events + underlying_events if event.feed_gap),
            "reconnect_count": sum(1 for event in option_events + underlying_events if event.reconnect),
            "underlying_high": max(underlying_ltps) if underlying_ltps else None,
            "underlying_low": min(underlying_ltps) if underlying_ltps else None,
            "option_executable_bid_high": max(option_bids) if option_bids else None,
            "option_executable_bid_low": min(option_bids) if option_bids else None,
            "transition_times": {key: value.isoformat() for key, value in tracker.transition_times.items()},
            "cost_model": {
                "entry_slippage_percent": settings.risk_expected_entry_slippage_pct,
                "exit_slippage_percent": settings.risk_expected_exit_slippage_pct,
                "entry_cost_percent": settings.risk_allocated_entry_cost_pct,
                "exit_cost_percent": settings.risk_allocated_exit_cost_pct,
            },
        }

    def _persist_observation(self, tracker: EpisodePathState) -> None:
        with self._lock:
            summary = self._summary_locked(tracker, None)
            transitions = dict(tracker.transition_times)
            context = dict(tracker.context)
        def write() -> None:
            session = get_session()
            try:
                record = session.query(EpisodeObservationRecord).filter(EpisodeObservationRecord.episode_key == tracker.episode_key).first()
                values = {
                "updated_at": ist_now_naive(),
                "state": tracker.state,
                "underlying_token": tracker.underlying_token,
                "option_token": tracker.option_token,
                "first_prepared_at": transitions.get("PREPARED"),
                "first_armed_at": transitions.get("ARMED"),
                "first_triggered_at": transitions.get("TRIGGERED"),
                "first_policy_eligible_at": transitions.get("POLICY_ELIGIBLE"),
                "first_executable_at": tracker.first_executable_at,
                "actual_order_at": transitions.get("ORDER_PENDING"),
                "invalidated_at": transitions.get("INVALIDATED"),
                "terminal_at": transitions.get("EXPIRED") or transitions.get("CLOSED"),
                "context_json": json.dumps(context, default=str, sort_keys=True),
                "path_summary_json": json.dumps(summary, default=str, sort_keys=True),
                "coverage_json": json.dumps(
                    {
                        "option_event_count": summary.get("option_event_count"),
                        "executable_bid_event_count": summary.get("executable_bid_event_count"),
                        "executable_coverage_percent": summary.get("executable_coverage_percent"),
                        "missing_intervals": summary.get("missing_intervals"),
                        "dropped_event_count": summary.get("dropped_event_count"),
                    },
                    default=str,
                    sort_keys=True,
                ),
            }
                if record is None:
                    record = EpisodeObservationRecord(
                        episode_key=tracker.episode_key,
                        created_at=ist_now_naive(),
                        first_observed_at=tracker.first_observed_at,
                        **values,
                    )
                    session.add(record)
                else:
                    for key, value in values.items():
                        setattr(record, key, value)
                session.commit()
            finally:
                session.close()

        self._persist_with_retry("observation", tracker.episode_key, write)

    def _persist_with_retry(
        self,
        kind: str,
        episode_key: str,
        operation: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        maximum_attempts = max(1, int(settings.outcome_queue_max_retries) + 1)
        for attempt in range(1, maximum_attempts + 1):
            try:
                operation()
                return True
            except Exception as exc:
                with self._lock:
                    self.persistence_failure_count += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < maximum_attempts:
                        self.persistence_retry_count += 1
                if attempt < maximum_attempts:
                    time_module.sleep(min(0.01 * attempt, 0.05))
                    continue
                dead_letter = {
                    "kind": kind,
                    "episode_key": episode_key,
                    "attempts": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                    "metadata": dict(metadata or {}),
                    "recorded_at": ist_now_naive().isoformat(),
                }
                with self._lock:
                    self.dead_letter_count += 1
                    self._dead_letters.append(dead_letter)
                    self._dead_letters[:] = self._dead_letters[-1000:]
                return False
        return False

    def _record_transition_locked(self, tracker: EpisodePathState, state: str, timestamp: datetime) -> None:
        tracker.transition_times.setdefault(state, timestamp)

    def _configured_horizons(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for raw in str(settings.outcome_horizons_seconds).split(","):
            try:
                seconds = int(raw.strip())
            except ValueError:
                continue
            if seconds > 0:
                name = "30_seconds" if seconds == 30 else f"{seconds // 60}_minute" if seconds == 60 else f"{seconds // 60}_minutes"
                values[name] = seconds
        return values

    def _horizon_cutoff(self, tracker: EpisodePathState, horizon: str | None) -> datetime | None:
        if horizon in self.horizons:
            from datetime import timedelta

            return tracker.first_observed_at + timedelta(seconds=self.horizons[str(horizon)])
        if horizon == "original_stop_or_target":
            candidates = [value for value in (tracker.first_target_at, tracker.first_stop_at) if value is not None]
            return min(candidates) if candidates else None
        if horizon == "session_cutoff":
            return self._session_cutoff(tracker.first_observed_at)
        return None

    def _session_cutoff(self, value: datetime) -> datetime:
        hour, minute = (int(item) for item in str(settings.outcome_session_cutoff_time).split(":", 1))
        return value.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _entry_value(self, ask: float) -> float:
        return ask * (
            1.0
            + float(settings.risk_expected_entry_slippage_pct) / 100.0
            + float(settings.risk_allocated_entry_cost_pct) / 100.0
        )

    def _exit_value(self, bid: float) -> float:
        return bid * (
            1.0
            - float(settings.risk_expected_exit_slippage_pct) / 100.0
            - float(settings.risk_allocated_exit_cost_pct) / 100.0
        )

    def _positive(self, value: Any) -> float | None:
        parsed = self._float(value)
        return parsed if parsed > 0 else None

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _optional_int(self, value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _naive(self, value: datetime) -> datetime:
        return value.replace(tzinfo=None)
