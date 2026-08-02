from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func

from app.config import settings
from app.services.database import (
    Candle,
    DecisionOutcomeRecord,
    EpisodeObservationRecord,
    RawTickRecord,
    SetupEpisodeRecord,
    ShadowPolicyDecisionRecord,
    get_session,
)
from app.services.policy_evaluation_service import PolicyEvaluationService


class ResearchDatasetAuditService:
    """Read-only completeness audit; it never converts absent data into observations."""

    def audit(self) -> dict[str, Any]:
        session = get_session()
        try:
            raw_ticks = int(session.query(func.count(RawTickRecord.id)).scalar() or 0)
            sessions = int(session.query(func.count(func.distinct(RawTickRecord.session_date))).scalar() or 0)
            unique_episodes = int(session.query(func.count(SetupEpisodeRecord.id)).scalar() or 0)
            bid_ticks = int(
                session.query(func.count(RawTickRecord.id))
                .filter(RawTickRecord.bid.isnot(None), RawTickRecord.bid > 0)
                .scalar()
                or 0
            )
            ask_ticks = int(
                session.query(func.count(RawTickRecord.id))
                .filter(RawTickRecord.ask.isnot(None), RawTickRecord.ask > 0)
                .scalar()
                or 0
            )
            bid_ask_ticks = int(
                session.query(func.count(RawTickRecord.id))
                .filter(
                    RawTickRecord.bid.isnot(None),
                    RawTickRecord.bid > 0,
                    RawTickRecord.ask.isnot(None),
                    RawTickRecord.ask > 0,
                )
                .scalar()
                or 0
            )
            underlying_ticks = int(
                session.query(func.count(RawTickRecord.id))
                .filter(func.upper(RawTickRecord.symbol).in_(("BANKNIFTY", "NIFTY BANK")))
                .scalar()
                or 0
            )
            option_ticks = int(
                session.query(func.count(RawTickRecord.id))
                .filter(
                    func.upper(RawTickRecord.symbol).like("BANKNIFTY%"),
                    ~func.upper(RawTickRecord.symbol).in_(("BANKNIFTY", "NIFTY BANK")),
                )
                .scalar()
                or 0
            )
            candle_count = int(session.query(func.count(Candle.id)).scalar() or 0)
            generated = int(session.query(func.count(Candle.id)).filter(Candle.is_generated == 1).scalar() or 0)
            backfilled = int(
                session.query(func.count(Candle.id))
                .filter(
                    (func.lower(func.coalesce(Candle.timestamp_source, "")).like("%backfill%"))
                    | (func.lower(func.coalesce(Candle.data_quality, "")).like("%backfill%"))
                )
                .scalar()
                or 0
            )
            observations = int(session.query(func.count(EpisodeObservationRecord.id)).scalar() or 0)
            policy_decisions = int(session.query(func.count(ShadowPolicyDecisionRecord.id)).scalar() or 0)
            missing = self._missing_intervals(session)
        finally:
            session.close()

        bid_ask_coverage = self._percent(bid_ask_ticks, raw_ticks)
        sufficient = bool(
            sessions >= 20
            and unique_episodes >= 100
            and bid_ask_coverage is not None
            and bid_ask_coverage >= float(settings.outcome_min_executable_coverage_pct)
            and observations >= 100
        )
        return {
            "dataset_label": "PRELIMINARY" if sufficient else "INSUFFICIENT DATA",
            "sessions_available": sessions,
            "unique_episodes": unique_episodes,
            "episode_observations": observations,
            "shadow_policy_decisions": policy_decisions,
            "raw_tick_count": raw_ticks,
            "tick_data_coverage": {
                "bid_percent": self._percent(bid_ticks, raw_ticks),
                "ask_percent": self._percent(ask_ticks, raw_ticks),
                "bid_and_ask_percent": bid_ask_coverage,
            },
            "underlying_tick_count": underlying_ticks,
            "option_tick_count": option_ticks,
            "underlying_coverage_percent": self._percent(underlying_ticks, raw_ticks),
            "option_coverage_percent": self._percent(option_ticks, raw_ticks),
            "missing_intervals": missing,
            "candle_count": candle_count,
            "generated_candle_percent": self._percent(generated, candle_count),
            "backfilled_candle_percent": self._percent(backfilled, candle_count),
            "preliminary_conclusions_supported": sufficient,
            "conclusion": (
                "Dataset meets the minimum mechanical completeness screen; chronological policy evaluation is still required."
                if sufficient
                else "Insufficient unique episodes, sessions, executable quotes, or completed observations for policy conclusions."
            ),
        }

    def _missing_intervals(self, session: Any) -> dict[str, Any]:
        threshold = float(settings.outcome_missing_interval_seconds)
        rows = (
            session.query(
                RawTickRecord.instrument_token,
                RawTickRecord.session_date,
                RawTickRecord.exchange_timestamp,
                RawTickRecord.receive_timestamp,
            )
            .order_by(
                RawTickRecord.instrument_token.asc(),
                RawTickRecord.session_date.asc(),
                RawTickRecord.exchange_timestamp.asc(),
                RawTickRecord.receive_timestamp.asc(),
            )
            .yield_per(5000)
        )
        last: dict[tuple[int, str], datetime] = {}
        count = 0
        seconds = 0.0
        worst = 0.0
        per_token: dict[int, int] = defaultdict(int)
        for token, session_date, exchange_timestamp, receive_timestamp in rows:
            timestamp = exchange_timestamp or receive_timestamp
            if timestamp is None:
                continue
            token_int = int(token)
            session_key = (token_int, str(session_date))
            prior = last.get(session_key)
            if prior is not None:
                gap = max(0.0, (timestamp - prior).total_seconds())
                if gap > threshold:
                    count += 1
                    seconds += gap
                    worst = max(worst, gap)
                    per_token[token_int] += 1
            last[session_key] = timestamp
        return {
            "threshold_seconds": threshold,
            "count": count,
            "total_seconds": round(seconds, 3),
            "worst_seconds": round(worst, 3),
            "tokens_with_gaps": len(per_token),
            "largest_counts_by_token": [
                {"instrument_token": token, "count": token_count}
                for token, token_count in sorted(per_token.items(), key=lambda item: item[1], reverse=True)[:20]
            ],
        }

    def _percent(self, numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator * 100.0, 4) if denominator else None


class ShadowPolicyResearchReportService:
    """Builds a unique-episode evaluation view from persisted policy and path evidence."""

    def __init__(self, evaluator: PolicyEvaluationService | None = None) -> None:
        self.evaluator = evaluator or PolicyEvaluationService()

    def report(self, *, maximum_horizon_seconds: int = 900) -> dict[str, Any]:
        rows = self._rows()
        if not rows:
            return self.evaluator.evaluate([], dataset_label="INSUFFICIENT DATA")
        result = self.evaluator.evaluate_chronological(rows, maximum_horizon_seconds=maximum_horizon_seconds)
        result["source"] = "persisted_unique_episode_shadow_evidence"
        result["thresholds_frozen_before_final_oos_required"] = True
        return result

    def _rows(self) -> list[dict[str, Any]]:
        session = get_session()
        try:
            decisions = session.query(ShadowPolicyDecisionRecord).all()
            observations = {
                item.episode_key: item for item in session.query(EpisodeObservationRecord).all()
            }
            outcomes: dict[str, dict[str, Any]] = {}
            for item in (
                session.query(DecisionOutcomeRecord)
                .filter(DecisionOutcomeRecord.outcome_source == "continuous_executable_collector")
                .order_by(DecisionOutcomeRecord.created_at.asc())
                .all()
            ):
                parsed = self._json(item.outcome_json)
                current = outcomes.get(item.episode_key)
                if item.horizon == "original_stop_or_target" or current is None:
                    outcomes[item.episode_key] = parsed
            result: list[dict[str, Any]] = []
            for decision in decisions:
                payload = self._json(decision.decision_json)
                observation = observations.get(decision.episode_key)
                context = self._json(observation.context_json) if observation is not None else {}
                timing = self._json(decision.timing_waterfall_json)
                decision_at = (
                    observation.first_observed_at
                    if observation is not None
                    else decision.created_at
                )
                result.append(
                    {
                        "episode_key": decision.episode_key,
                        "policy_version": decision.policy_version,
                        "decision_at": decision_at,
                        "trading_date": decision_at.date().isoformat(),
                        "entered": bool(payload.get("enterable")),
                        "outcome": outcomes.get(decision.episode_key, {}),
                        "gates": (payload.get("features") or {}).get("gates", {}),
                        "timing_waterfall": timing,
                        "session_phase": context.get("session_phase", "unknown"),
                        "regime": context.get("market_regime", "unknown"),
                        "setup_family": context.get("setup_family", "unknown"),
                        "direction": context.get("direction", "unknown"),
                        "dte": context.get("dte", "unknown"),
                        "expiry_day": context.get("expiry_day", "unknown"),
                        "premium_band": context.get("premium_band", "unknown"),
                        "spread_band": context.get("spread_band", "unknown"),
                        "liquidity_band": context.get("liquidity_band", "unknown"),
                    }
                )
            return result
        finally:
            session.close()

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
