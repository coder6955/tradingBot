from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time
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
            session_qualification = self._session_qualification(session)
            sessions = int(session_qualification["complete_sessions"])
            unique_episodes = int(
                session.query(func.count(SetupEpisodeRecord.id)).scalar() or 0
            )
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
                .filter(
                    func.upper(RawTickRecord.symbol).in_(("BANKNIFTY", "NIFTY BANK"))
                )
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
            generated = int(
                session.query(func.count(Candle.id))
                .filter(Candle.is_generated == 1)
                .scalar()
                or 0
            )
            backfilled = int(
                session.query(func.count(Candle.id))
                .filter(
                    (
                        func.lower(func.coalesce(Candle.timestamp_source, "")).like(
                            "%backfill%"
                        )
                    )
                    | (
                        func.lower(func.coalesce(Candle.data_quality, "")).like(
                            "%backfill%"
                        )
                    )
                )
                .scalar()
                or 0
            )
            observations = int(
                session.query(func.count(EpisodeObservationRecord.id)).scalar() or 0
            )
            policy_decisions = int(
                session.query(func.count(ShadowPolicyDecisionRecord.id)).scalar() or 0
            )
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
            "sessions_observed": int(session_qualification["observed_sessions"]),
            "partial_sessions": int(session_qualification["partial_sessions"]),
            "session_qualification": session_qualification,
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
                else "Insufficient unique episodes, complete sessions, executable quotes, or completed observations for policy conclusions."
            ),
        }

    def _session_qualification(self, session: Any) -> dict[str, Any]:
        rows = (
            session.query(
                RawTickRecord.session_date,
                RawTickRecord.exchange_timestamp,
                RawTickRecord.receive_timestamp,
            )
            .filter(func.upper(RawTickRecord.symbol).in_(("BANKNIFTY", "NIFTY BANK")))
            .order_by(
                RawTickRecord.session_date.asc(),
                RawTickRecord.exchange_timestamp.asc(),
                RawTickRecord.receive_timestamp.asc(),
            )
            .yield_per(5000)
        )
        return self._qualify_session_rows(rows)

    def _qualify_session_rows(self, rows: Any) -> dict[str, Any]:
        market_open = self._parse_time(settings.runtime_market_open_time)
        market_close = self._parse_time(settings.runtime_market_close_time)
        tolerance_seconds = max(
            0,
            int(settings.research_session_boundary_tolerance_minutes) * 60,
        )
        expected_interval_seconds = max(
            0.1, float(settings.outcome_missing_interval_seconds)
        )
        maximum_gap_seconds = max(
            expected_interval_seconds,
            float(settings.research_session_max_gap_seconds),
        )
        minimum_coverage_pct = min(
            100.0, max(0.0, float(settings.research_session_min_coverage_pct))
        )
        grouped: dict[str, dict[str, Any]] = {}
        for session_date, exchange_timestamp, receive_timestamp in rows:
            timestamp = exchange_timestamp or receive_timestamp
            if timestamp is None:
                continue
            clean_timestamp = timestamp.replace(tzinfo=None)
            trading_date = self._coerce_date(session_date, clean_timestamp.date())
            date_key = trading_date.isoformat()
            bucket = grouped.setdefault(
                date_key,
                {"trading_date": trading_date, "timestamps": []},
            )
            start = datetime.combine(trading_date, market_open)
            end = datetime.combine(trading_date, market_close)
            if start <= clean_timestamp <= end:
                bucket["timestamps"].append(clean_timestamp)

        details: list[dict[str, Any]] = []
        complete_dates: list[str] = []
        partial_dates: list[str] = []
        for date_key in sorted(grouped):
            bucket = grouped[date_key]
            trading_date = bucket["trading_date"]
            timestamps = sorted(bucket["timestamps"])
            start = datetime.combine(trading_date, market_open)
            end = datetime.combine(trading_date, market_close)
            session_seconds = max(1.0, (end - start).total_seconds())
            reasons: list[str] = []
            if not timestamps:
                details.append(
                    {
                        "trading_date": date_key,
                        "classification": "PARTIAL",
                        "reasons": ["no_regular_market_underlying_ticks"],
                        "tick_count": 0,
                        "coverage_pct": 0.0,
                        "first_tick": None,
                        "last_tick": None,
                        "opening_delay_seconds": session_seconds,
                        "closing_shortfall_seconds": session_seconds,
                        "gap_count": 0,
                        "missing_seconds": session_seconds,
                        "worst_gap_seconds": None,
                    }
                )
                partial_dates.append(date_key)
                continue

            first_tick = timestamps[0]
            last_tick = timestamps[-1]
            opening_delay = max(0.0, (first_tick - start).total_seconds())
            closing_shortfall = max(0.0, (end - last_tick).total_seconds())
            gap_count = 0
            internal_missing = 0.0
            worst_gap = 0.0
            prior = first_tick
            for timestamp in timestamps[1:]:
                gap = max(0.0, (timestamp - prior).total_seconds())
                if gap > expected_interval_seconds:
                    gap_count += 1
                    internal_missing += gap - expected_interval_seconds
                worst_gap = max(worst_gap, gap)
                prior = timestamp
            missing_seconds = min(
                session_seconds,
                opening_delay + closing_shortfall + internal_missing,
            )
            coverage_pct = round(
                max(0.0, (session_seconds - missing_seconds) / session_seconds * 100),
                4,
            )
            if opening_delay > tolerance_seconds:
                reasons.append("late_session_start")
            if closing_shortfall > tolerance_seconds:
                reasons.append("early_session_end")
            if worst_gap > maximum_gap_seconds:
                reasons.append("gap_exceeds_limit")
            if coverage_pct < minimum_coverage_pct:
                reasons.append("coverage_below_minimum")
            classification = "COMPLETE" if not reasons else "PARTIAL"
            if classification == "COMPLETE":
                complete_dates.append(date_key)
            else:
                partial_dates.append(date_key)
            details.append(
                {
                    "trading_date": date_key,
                    "classification": classification,
                    "reasons": reasons,
                    "tick_count": len(timestamps),
                    "coverage_pct": coverage_pct,
                    "first_tick": first_tick.isoformat(sep=" "),
                    "last_tick": last_tick.isoformat(sep=" "),
                    "opening_delay_seconds": round(opening_delay, 3),
                    "closing_shortfall_seconds": round(closing_shortfall, 3),
                    "gap_count": gap_count,
                    "missing_seconds": round(missing_seconds, 3),
                    "worst_gap_seconds": round(worst_gap, 3),
                }
            )
        return {
            "underlying_symbols": ["BANKNIFTY", "NIFTY BANK"],
            "market_window": {
                "start": market_open.strftime("%H:%M"),
                "end": market_close.strftime("%H:%M"),
            },
            "boundary_tolerance_minutes": int(
                settings.research_session_boundary_tolerance_minutes
            ),
            "expected_tick_interval_seconds": expected_interval_seconds,
            "maximum_gap_seconds": maximum_gap_seconds,
            "minimum_coverage_pct": minimum_coverage_pct,
            "observed_sessions": len(details),
            "complete_sessions": len(complete_dates),
            "partial_sessions": len(partial_dates),
            "complete_dates": complete_dates,
            "partial_dates": partial_dates,
            "details": details,
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
                for token, token_count in sorted(
                    per_token.items(), key=lambda item: item[1], reverse=True
                )[:20]
            ],
        }

    def _percent(self, numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator * 100.0, 4) if denominator else None

    def _parse_time(self, value: str) -> time:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))

    def _coerce_date(self, value: Any, fallback: date) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return fallback


class ShadowPolicyResearchReportService:
    """Builds a unique-episode evaluation view from persisted policy and path evidence."""

    def __init__(self, evaluator: PolicyEvaluationService | None = None) -> None:
        self.evaluator = evaluator or PolicyEvaluationService()

    def report(self, *, maximum_horizon_seconds: int = 900) -> dict[str, Any]:
        rows = self._rows()
        if not rows:
            return self.evaluator.evaluate([], dataset_label="INSUFFICIENT DATA")
        result = self.evaluator.evaluate_chronological(
            rows, maximum_horizon_seconds=maximum_horizon_seconds
        )
        result["source"] = "persisted_unique_episode_shadow_evidence"
        result["thresholds_frozen_before_final_oos_required"] = True
        return result

    def _rows(self) -> list[dict[str, Any]]:
        session = get_session()
        try:
            decisions = session.query(ShadowPolicyDecisionRecord).all()
            observations = {
                item.episode_key: item
                for item in session.query(EpisodeObservationRecord).all()
            }
            outcomes: dict[str, dict[str, Any]] = {}
            for item in (
                session.query(DecisionOutcomeRecord)
                .filter(
                    DecisionOutcomeRecord.outcome_source
                    == "continuous_executable_collector"
                )
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
                context = (
                    self._json(observation.context_json)
                    if observation is not None
                    else {}
                )
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
