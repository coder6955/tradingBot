from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.config import settings
from app.services.database import ShadowPolicyDecisionRecord, get_session
from app.services.episode_outcome_collector import MarketPathEvent
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


@dataclass(frozen=True)
class EntryPolicyContext:
    episode_key: str
    direction: str
    setup_family: str
    observed_at: datetime
    underlying_trigger: float
    stop_loss: float
    target_1: float
    lot_size: int
    minimum_depth: int
    maximum_spread_pct: float
    five_minute_structure: str
    one_minute_structure: str
    five_minute_strongly_opposed: bool = False
    constituent_evidence_available: bool = False
    constituent_strongly_contradictory: bool = False
    contract_tradeable: bool = False
    price_plan_valid: bool = False
    base_risk_feasible: bool = False
    provenance_valid: bool = False
    data_fresh: bool = False
    premium_confirmation_passed: bool = False
    preparation_passed: bool = False
    fast_candidate_requirements_passed: bool = False
    promotion_registered: bool = False
    armed_confirmation_passed: bool = False
    chase_valid: bool = False
    target_room_valid: bool = False
    remaining_rr_valid: bool = False
    session_eligible: bool = False
    account_eligible: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryPolicyDecision:
    policy_version: str
    policy_kind: str
    state: str
    enterable: bool
    reasons: tuple[str, ...]
    features: dict[str, Any]
    timing_waterfall: dict[str, str | None]
    shadow_only: bool = True
    can_invoke_order_service: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EntryPolicy(Protocol):
    version: str
    kind: str

    def evaluate(
        self, context: EntryPolicyContext, events: list[MarketPathEvent]
    ) -> EntryPolicyDecision: ...


class CurrentBaselineEntryPolicy:
    """Shadow representation of the unchanged active v6 confirmation sequence."""

    version = "current_active_baseline_v1"
    kind = "CURRENT_ACTIVE_BASELINE"

    def evaluate(
        self, context: EntryPolicyContext, events: list[MarketPathEvent]
    ) -> EntryPolicyDecision:
        direction = context.direction.lower()
        required_structure = (
            "bullish" if direction in {"bullish", "buy_ce"} else "bearish"
        )
        route = str(context.metadata.get("entry_route") or "fast").lower()
        requires_fast_promotion = route == "fast"
        requires_armed_confirmation = route in {"fast", "armed"}
        gates = {
            "completed_five_minute_directional": context.five_minute_structure.lower()
            == required_structure,
            "exact_one_five_agreement": context.one_minute_structure.lower()
            == required_structure,
            "premium_confirmation": context.premium_confirmation_passed,
            "preparation": context.preparation_passed,
            "fast_candidate_requirements": context.fast_candidate_requirements_passed
            if requires_fast_promotion
            else True,
            "promotion_registered": context.promotion_registered
            if requires_armed_confirmation
            else True,
            "armed_second_confirmation": context.armed_confirmation_passed
            if requires_armed_confirmation
            else True,
            "chase": context.chase_valid,
            "target_room": context.target_room_valid,
            "remaining_rr": context.remaining_rr_valid,
            "session": context.session_eligible,
            "account": context.account_eligible,
        }
        failed = tuple(name for name, passed in gates.items() if not passed)
        enterable = not failed
        state = (
            "TRIGGERED"
            if enterable
            else "ARMED"
            if context.promotion_registered
            else "PREPARED"
            if context.preparation_passed
            else "OBSERVE"
        )
        return EntryPolicyDecision(
            policy_version=self.version,
            policy_kind=self.kind,
            state=state,
            enterable=enterable,
            reasons=failed or ("ACTIVE_BASELINE_ALL_CONFIRMATIONS_PASSED",),
            features={
                "gates": gates,
                "event_count": len(events),
                "active_behavior_unchanged": True,
                "entry_route": route,
                "route_conditional_gates_reproduced": True,
            },
            timing_waterfall=_timing_waterfall(context, events),
        )


class TransitionPreparationShadowPolicy:
    version = "transition_preparation_shadow_v1"
    kind = "TRANSITION_PREPARATION"

    def evaluate(
        self, context: EntryPolicyContext, events: list[MarketPathEvent]
    ) -> EntryPolicyDecision:
        direction = context.direction.lower()
        expected = "bullish" if direction in {"bullish", "buy_ce"} else "bearish"
        five_allows_preparation = context.five_minute_structure.lower() in {
            expected,
            "neutral",
            "transition",
            "transitioning",
        }
        gates = {
            "five_minute_not_strongly_opposed": five_allows_preparation
            and not context.five_minute_strongly_opposed,
            "one_minute_directional": context.one_minute_structure.lower() == expected,
            "underlying_trigger_defined": context.underlying_trigger > 0,
            "constituents_available": context.constituent_evidence_available,
            "constituents_not_strongly_contradictory": not context.constituent_strongly_contradictory,
            "contract_tradeable": context.contract_tradeable,
            "price_plan_valid": context.price_plan_valid,
            "base_risk_feasible": context.base_risk_feasible,
            "provenance_valid": context.provenance_valid,
            "data_fresh": context.data_fresh,
        }
        failed = tuple(name for name, passed in gates.items() if not passed)
        prepared = not failed
        return EntryPolicyDecision(
            policy_version=self.version,
            policy_kind=self.kind,
            state="PREPARED" if prepared else "OBSERVE",
            enterable=False,
            reasons=failed or ("TRANSITION_PREPARED_AWAITING_FRESH_TRIGGER",),
            features={
                "gates": gates,
                "event_count": len(events),
                "preparation_is_not_entry": True,
            },
            timing_waterfall=_timing_waterfall(context, events),
        )


class ContinuousTransmissionShadowPolicy:
    version = "continuous_option_transmission_shadow_v1"
    kind = "CONTINUOUS_TRIGGER_TRANSMISSION"

    def evaluate(
        self, context: EntryPolicyContext, events: list[MarketPathEvent]
    ) -> EntryPolicyDecision:
        option_token = _optional_int(
            context.metadata.get("option_token")
            or context.metadata.get("instrument_token")
        )
        underlying_token = _optional_int(context.metadata.get("underlying_token"))
        direction_up = context.direction.lower() in {"bullish", "buy_ce"}
        underlying_cross_at: datetime | None = None
        option_response_at: datetime | None = None
        first_bid: float | None = None
        first_ask: float | None = None
        prior_bid: float | None = None
        prior_ask: float | None = None
        bid_progressions = 0
        ask_progressions = 0
        non_regressing_bid_started: datetime | None = None
        latest_bid: float | None = None
        latest_ask: float | None = None
        latest_bid_depth: int | None = None
        latest_ask_depth: int | None = None
        underlying_start: float | None = None
        underlying_latest: float | None = None
        recent_bids: list[float] = []
        for event in sorted(
            events, key=lambda item: (item.exchange_timestamp, item.receive_timestamp)
        ):
            if (
                underlying_token is not None
                and event.instrument_token == underlying_token
            ):
                if event.ltp is not None:
                    underlying_start = (
                        underlying_start if underlying_start is not None else event.ltp
                    )
                    underlying_latest = event.ltp
                    crossed = (
                        event.ltp >= context.underlying_trigger
                        if direction_up
                        else event.ltp <= context.underlying_trigger
                    )
                    if crossed and underlying_cross_at is None:
                        underlying_cross_at = event.exchange_timestamp
            if option_token is not None and event.instrument_token == option_token:
                if event.bid is not None:
                    first_bid = first_bid if first_bid is not None else event.bid
                    latest_bid = event.bid
                    recent_bids.append(event.bid)
                    if prior_bid is not None and event.bid > prior_bid:
                        bid_progressions += 1
                    if prior_bid is None or event.bid >= prior_bid:
                        non_regressing_bid_started = (
                            non_regressing_bid_started or event.exchange_timestamp
                        )
                    else:
                        non_regressing_bid_started = None
                    prior_bid = event.bid
                if event.ask is not None:
                    first_ask = first_ask if first_ask is not None else event.ask
                    latest_ask = event.ask
                    if prior_ask is not None and event.ask > prior_ask:
                        ask_progressions += 1
                    prior_ask = event.ask
                latest_bid_depth = event.bid_depth
                latest_ask_depth = event.ask_depth
                if (
                    underlying_cross_at
                    and option_response_at is None
                    and first_bid
                    and event.bid
                    and event.bid > first_bid
                ):
                    option_response_at = event.exchange_timestamp
        spread_pct = (
            (latest_ask - latest_bid) / max(latest_ask, 0.01) * 100.0
            if latest_bid is not None and latest_ask is not None
            else None
        )
        underlying_move_pct = (
            (underlying_latest - underlying_start)
            / max(abs(underlying_start), 0.01)
            * 100.0
            if underlying_start is not None and underlying_latest is not None
            else None
        )
        bid_move_pct = (
            (latest_bid - first_bid) / max(first_bid, 0.01) * 100.0
            if first_bid is not None and latest_bid is not None
            else None
        )
        ask_move_pct = (
            (latest_ask - first_ask) / max(first_ask, 0.01) * 100.0
            if first_ask is not None and latest_ask is not None
            else None
        )
        response_latency_ms = (
            (option_response_at - underlying_cross_at).total_seconds() * 1000.0
            if underlying_cross_at and option_response_at
            else None
        )
        transmission_consistent = bool(
            bid_progressions >= 1
            and latest_bid is not None
            and first_bid is not None
            and latest_bid >= first_bid
        )
        gates = {
            "prepared_context": context.price_plan_valid
            and context.base_risk_feasible
            and context.data_fresh
            and context.provenance_valid,
            "underlying_trigger_crossed": underlying_cross_at is not None,
            "option_bid_responded": option_response_at is not None,
            "transmission_consistent": transmission_consistent,
            "spread_stable": spread_pct is not None
            and spread_pct <= context.maximum_spread_pct,
            "bid_depth_safe": latest_bid_depth is not None
            and latest_bid_depth >= context.minimum_depth,
            "ask_depth_safe": latest_ask_depth is not None
            and latest_ask_depth >= context.minimum_depth,
            "chase": context.chase_valid,
            "target_room": context.target_room_valid,
            "remaining_rr": context.remaining_rr_valid,
            "session": context.session_eligible,
            "account": context.account_eligible,
        }
        failed = tuple(name for name, passed in gates.items() if not passed)
        enterable = not failed
        state = (
            "TRIGGERED" if enterable else "ARMED" if underlying_cross_at else "PREPARED"
        )
        features = {
            "gates": gates,
            "underlying_move_percent": underlying_move_pct,
            "option_bid_move_percent": bid_move_pct,
            "option_ask_move_percent": ask_move_pct,
            "mid_price_move_diagnostic_only": _mid_move(
                first_bid, first_ask, latest_bid, latest_ask
            ),
            "bid_progression_count": bid_progressions,
            "ask_progression_count": ask_progressions,
            "non_regressing_bid_duration_ms": (
                (
                    events[-1].exchange_timestamp - non_regressing_bid_started
                ).total_seconds()
                * 1000.0
                if events and non_regressing_bid_started
                else None
            ),
            "spread_after_trigger_percent": spread_pct,
            "latest_bid_depth": latest_bid_depth,
            "latest_ask_depth": latest_ask_depth,
            "response_latency_ms": response_latency_ms,
            "premium_move_per_underlying_move": (
                bid_move_pct / underlying_move_pct
                if bid_move_pct is not None and underlying_move_pct not in {None, 0.0}
                else None
            ),
            "response_consistency": sum(
                1 for left, right in zip(recent_bids, recent_bids[1:]) if right >= left
            )
            / max(len(recent_bids) - 1, 1),
            "recent_executable_bid_breakout": bool(
                recent_bids and latest_bid == max(recent_bids)
            ),
            "second_confirmation_reset_required": False,
            "prior_tick_evidence_preserved": True,
        }
        waterfall = _timing_waterfall(context, events)
        waterfall["underlying_trigger_crossed"] = _iso(underlying_cross_at)
        waterfall["option_first_responded"] = _iso(option_response_at)
        waterfall["policy_became_enterable"] = (
            _iso(option_response_at) if enterable else None
        )
        return EntryPolicyDecision(
            policy_version=self.version,
            policy_kind=self.kind,
            state=state,
            enterable=enterable,
            reasons=failed or ("CONTINUOUS_EXECUTABLE_TRANSMISSION_CONFIRMED",),
            features=features,
            timing_waterfall=waterfall,
        )


class ShadowEntryPolicyComparisonService:
    """Evaluates policies without accepting any order-routing dependency."""

    def __init__(self, policies: list[EntryPolicy] | None = None) -> None:
        self.policies = policies or [
            CurrentBaselineEntryPolicy(),
            TransitionPreparationShadowPolicy(),
            ContinuousTransmissionShadowPolicy(),
        ]

    def compare(
        self,
        context: EntryPolicyContext,
        events: list[MarketPathEvent],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        decisions = [policy.evaluate(context, list(events)) for policy in self.policies]
        if persist:
            for decision in decisions:
                self._persist(context, decision)
        return {
            "episode_key": context.episode_key,
            "same_context_for_all_policies": True,
            "order_service_available": False,
            "decisions": {
                decision.policy_version: decision.to_dict() for decision in decisions
            },
        }

    def _persist(
        self, context: EntryPolicyContext, decision: EntryPolicyDecision
    ) -> None:
        lineage = current_strategy_lineage()
        session = get_session()
        try:
            record = (
                session.query(ShadowPolicyDecisionRecord)
                .filter(
                    ShadowPolicyDecisionRecord.episode_key == context.episode_key,
                    ShadowPolicyDecisionRecord.policy_version
                    == decision.policy_version,
                )
                .first()
            )
            values = {
                "updated_at": ist_now_naive(),
                "policy_kind": decision.policy_kind,
                "final_state": decision.state,
                "decision_json": json.dumps(
                    decision.to_dict(), default=str, sort_keys=True
                ),
                "timing_waterfall_json": json.dumps(
                    decision.timing_waterfall, default=str, sort_keys=True
                ),
                "strategy_version": str(lineage["strategy_version"]),
                "config_hash": str(lineage["config_hash"]),
            }
            if record is None:
                record = ShadowPolicyDecisionRecord(
                    created_at=ist_now_naive(),
                    episode_key=context.episode_key,
                    policy_version=decision.policy_version,
                    outcome_json=None,
                    **values,
                )
                session.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            session.commit()
        finally:
            session.close()


def _timing_waterfall(
    context: EntryPolicyContext, events: list[MarketPathEvent]
) -> dict[str, str | None]:
    metadata = context.metadata
    option_token = _optional_int(
        metadata.get("option_token") or metadata.get("instrument_token")
    )
    first_ask = next(
        (
            event.exchange_timestamp
            for event in events
            if event.instrument_token == option_token and event.ask is not None
        ),
        None,
    )
    return {
        "first_observable_directional_evidence": _iso(context.observed_at),
        "first_eligible_preparation": _string_time(metadata.get("first_prepared_at")),
        "underlying_trigger_crossed": _string_time(
            metadata.get("underlying_trigger_crossed_at")
        ),
        "option_first_responded": _string_time(
            metadata.get("option_first_responded_at")
        ),
        "premium_confirmation_passed": _string_time(
            metadata.get("premium_confirmation_at")
        ),
        "fast_path_validation_passed": _string_time(metadata.get("fast_validation_at")),
        "armed_registration": _string_time(metadata.get("armed_at")),
        "armed_confirmation_passed": _string_time(
            metadata.get("armed_confirmation_at")
        ),
        "policy_became_enterable": None,
        "first_executable_ask": _iso(first_ask),
        "order_submission": _string_time(metadata.get("order_submission_at")),
    }


def _mid_move(
    first_bid: float | None,
    first_ask: float | None,
    bid: float | None,
    ask: float | None,
) -> float | None:
    if None in {first_bid, first_ask, bid, ask}:
        return None
    first_mid = (float(first_bid) + float(first_ask)) / 2.0
    latest_mid = (float(bid) + float(ask)) / 2.0
    return (latest_mid - first_mid) / max(first_mid, 0.01) * 100.0


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _string_time(value: Any) -> str | None:
    return str(value) if value else None
