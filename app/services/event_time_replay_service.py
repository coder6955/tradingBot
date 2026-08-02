from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.models import Signal
from app.services.database import ReplayRunRecord, get_session
from app.services.entry_policy_shadow_service import (
    ContinuousTransmissionShadowPolicy,
    EntryPolicyContext,
    ShadowEntryPolicyComparisonService,
)
from app.services.episode_outcome_collector import EpisodeOutcomeCollector, MarketPathEvent
from app.services.episode_reservation_service import EpisodeReservationService
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


@dataclass(frozen=True)
class ReplayEvent:
    event_type: str
    instrument_token: int
    exchange_timestamp: datetime
    receive_timestamp: datetime
    sequence: int
    bid: float | None = None
    ask: float | None = None
    ltp: float | None = None
    bid_depth: int | None = None
    ask_depth: int | None = None
    provenance: str = "genuine"
    candle_state: str = "tick"
    candle_open_at: datetime | None = None
    candle_complete_at: datetime | None = None
    feed_gap: bool = False
    reconnect: bool = False
    instrument_master_version: str | None = None
    contract_available: bool | None = None


class EventTimeReplayService:
    """Deterministic event-time replay that has no order-routing capability."""

    def __init__(
        self,
        *,
        policy_service: ShadowEntryPolicyComparisonService | None = None,
        episode_service: EpisodeReservationService | None = None,
    ) -> None:
        self.policy_service = policy_service or ShadowEntryPolicyComparisonService()
        self.episode_service = episode_service or EpisodeReservationService()

    def episode_key_for_signal(self, signal: Signal, *, metadata: dict[str, Any] | None = None) -> str:
        return self.episode_service.identity(signal, metadata=metadata)["episode_key"]

    def run(
        self,
        *,
        context: EntryPolicyContext,
        events: list[ReplayEvent],
        data_version: str,
        tick_capable: bool,
        persist: bool = True,
    ) -> dict[str, Any]:
        ordered = sorted(
            events,
            key=lambda item: (
                self._naive(item.exchange_timestamp),
                self._naive(item.receive_timestamp),
                int(item.sequence),
            ),
        )
        self._validate_events(ordered, tick_capable=tick_capable)
        market_events = [self._to_market_event(event) for event in ordered if event.event_type.upper() == "TICK"]
        disabled_policies = [
            policy.version
            for policy in self.policy_service.policies
            if isinstance(policy, ContinuousTransmissionShadowPolicy) and not tick_capable
        ]
        active_policies = [policy for policy in self.policy_service.policies if policy.version not in disabled_policies]
        comparison = ShadowEntryPolicyComparisonService(active_policies).compare(context, market_events, persist=False)
        for version in disabled_policies:
            comparison["decisions"][version] = {
                "policy_version": version,
                "policy_kind": "CONTINUOUS_TRIGGER_TRANSMISSION",
                "state": "OBSERVE",
                "enterable": False,
                "reasons": ["TICK_CAPABLE_DATA_REQUIRED"],
                "features": {"bar_only_session": True},
                "timing_waterfall": {},
                "shadow_only": True,
                "can_invoke_order_service": False,
            }
        collector = EpisodeOutcomeCollector(start_worker=False, persist_observations=False)
        collector.register_episode(
            context.episode_key,
            context={
                "underlying_token": context.metadata.get("underlying_token"),
                "option_token": context.metadata.get("option_token") or context.metadata.get("instrument_token"),
                "target_1": context.target_1,
                "stop_loss": context.stop_loss,
                "account_equity": context.metadata.get("account_equity"),
            },
            state="OBSERVE",
            observed_at=context.observed_at,
        )
        for event in market_events:
            collector.process_event(event)
        outcome = collector.snapshot(context.episode_key)
        lineage = current_strategy_lineage()
        policy_versions = sorted(comparison["decisions"])
        input_payload = {
            "data_version": data_version,
            "config_hash": lineage["config_hash"],
            "strategy_version": lineage["strategy_version"],
            "policy_versions": policy_versions,
            "context": asdict(context),
            "events": [asdict(event) for event in ordered],
            "tick_capable": tick_capable,
        }
        input_hash = self._hash(input_payload)
        result_payload = {
            "episode_key": context.episode_key,
            "data_version": data_version,
            "tick_capable": tick_capable,
            "event_count": len(ordered),
            "event_order": [event.sequence for event in ordered],
            "disabled_policies": disabled_policies,
            "policy_comparison": comparison,
            "outcome": outcome,
            "lookahead_prevention": {
                "exchange_time_primary": True,
                "receive_time_secondary": True,
                "future_completed_candles_hidden": True,
                "ohlc_tick_inference_disabled": True,
                "instrument_master_lineage_preserved": True,
                "future_contract_liquidity_selection_disabled": True,
            },
        }
        output_hash = self._hash(result_payload)
        run_id = self._hash({"input_hash": input_hash, "output_hash": output_hash})
        result = {**result_payload, "run_id": run_id, "input_hash": input_hash, "output_hash": output_hash}
        if persist:
            self._persist_run(
                run_id=run_id,
                data_version=data_version,
                input_hash=input_hash,
                output_hash=output_hash,
                tick_capable=tick_capable,
                event_count=len(ordered),
                policy_versions=policy_versions,
                result=result,
            )
        return result

    def visible_candle_events(self, events: list[ReplayEvent], *, as_of: datetime) -> list[ReplayEvent]:
        boundary = self._naive(as_of)
        visible: list[ReplayEvent] = []
        for event in events:
            if event.event_type.upper() != "CANDLE":
                continue
            complete_at = self._naive(event.candle_complete_at) if event.candle_complete_at else None
            if event.candle_state.lower() == "completed" and complete_at and complete_at <= boundary:
                visible.append(event)
            elif event.candle_state.lower() == "building" and self._naive(event.exchange_timestamp) <= boundary:
                visible.append(event)
        return sorted(visible, key=lambda item: (item.exchange_timestamp, item.receive_timestamp, item.sequence))

    def _validate_events(self, events: list[ReplayEvent], *, tick_capable: bool) -> None:
        seen: set[tuple[datetime, datetime, int]] = set()
        for event in events:
            key = (self._naive(event.exchange_timestamp), self._naive(event.receive_timestamp), int(event.sequence))
            if key in seen:
                raise ValueError("replay events must have unique event-time ordering keys")
            seen.add(key)
            if event.event_type.upper() == "CANDLE":
                if event.candle_state.lower() not in {"building", "completed"}:
                    raise ValueError("candle replay event must be explicitly building or completed")
                if event.candle_state.lower() == "completed" and event.candle_complete_at is None:
                    raise ValueError("completed replay candle requires its actual completion timestamp")
                if event.provenance.lower() not in {"genuine", "generated", "backfilled", "websocket"}:
                    raise ValueError("candle provenance must be explicit")
            if event.event_type.upper() == "TICK" and event.contract_available is False:
                raise ValueError("tick event cannot exist before the replay contract is available")
            if event.instrument_master_version is None:
                raise ValueError("replay event requires instrument-master lineage")
        if not tick_capable and any(event.event_type.upper() == "TICK" for event in events):
            raise ValueError("bar-only replay cannot contain tick events")

    def _to_market_event(self, event: ReplayEvent) -> MarketPathEvent:
        return MarketPathEvent(
            instrument_token=int(event.instrument_token),
            exchange_timestamp=self._naive(event.exchange_timestamp),
            receive_timestamp=self._naive(event.receive_timestamp),
            bid=event.bid,
            ask=event.ask,
            ltp=event.ltp,
            bid_depth=event.bid_depth,
            ask_depth=event.ask_depth,
            source="event_time_replay",
            provenance=event.provenance,
            candle_state=event.candle_state,
            feed_gap=event.feed_gap,
            reconnect=event.reconnect,
        )

    def _persist_run(
        self,
        *,
        run_id: str,
        data_version: str,
        input_hash: str,
        output_hash: str,
        tick_capable: bool,
        event_count: int,
        policy_versions: list[str],
        result: dict[str, Any],
    ) -> None:
        lineage = current_strategy_lineage()
        session = get_session()
        try:
            record = session.query(ReplayRunRecord).filter(ReplayRunRecord.run_id == run_id).first()
            if record is None:
                session.add(
                    ReplayRunRecord(
                        created_at=ist_now_naive(),
                        run_id=run_id,
                        data_version=data_version,
                        config_hash=str(lineage["config_hash"]),
                        strategy_version=str(lineage["strategy_version"]),
                        policy_versions_json=json.dumps(policy_versions, sort_keys=True),
                        input_hash=input_hash,
                        output_hash=output_hash,
                        tick_capable=1 if tick_capable else 0,
                        event_count=event_count,
                        result_json=json.dumps(result, default=str, sort_keys=True),
                    )
                )
                session.commit()
        finally:
            session.close()

    def _hash(self, payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _naive(self, value: datetime) -> datetime:
        return value.replace(tzinfo=None)
