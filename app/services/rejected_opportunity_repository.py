from __future__ import annotations

import json
import hashlib
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, time, timedelta
from typing import Any

from app.config import settings
from app.services.database import (
    RejectedOpportunityRecord,
    SetupEpisodeRecord,
    get_session,
)
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now, ist_now_naive


class RejectedOpportunityRepository:
    def market_session(self) -> str:
        """Public session label used by scanner diagnostic enrichment."""
        return self._market_session()

    """Persist rejected scanner setups for later no-trade review."""

    DATA_OR_SESSION_REASON_MARKERS = (
        "market_closed",
        "outside market hours",
        "weekend",
        "real kite market data was not available",
        "mock",
        "fallback",
        "data_stale",
        "data_gap_detected",
        "stale",
        "quote_invalid",
        "selected_option_quote_invalid",
        "quote_unavailable",
        "premium_candles_stale_or_missing",
        "insufficient_current_session_premium_candles",
        "token_missing",
        "subscription_failed",
        "websocket_disconnected",
        "tick_stale",
    )
    OPERATIONAL_RISK_REASON_MARKERS = (
        "max daily loss",
        "max open trades",
        "max open premium exposure",
        "cooldown",
        "risk guard",
        "premium is too large",
        "account risk",
    )

    def __init__(self) -> None:
        self._writer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rejection-writer"
        )

    def save_rejection_async(self, **kwargs: Any) -> Future[RejectedOpportunityRecord]:
        """Queue non-critical scanner observations away from the decision thread."""
        return self._writer.submit(self.save_rejection, **kwargs)

    def save_rejection(
        self,
        *,
        symbol: str,
        side: str,
        action: str | None,
        score: int,
        reasons: list[str],
        snapshot: dict[str, Any] | None = None,
        contract: Any | None = None,
        factor_scores: dict[str, Any] | None = None,
        score_breakdown: dict[str, Any] | None = None,
        rejection_source: str = "scanner",
        market_session: str | None = None,
        learning_eligible: bool | None = None,
    ) -> RejectedOpportunityRecord:
        factors = dict(factor_scores or {})
        if contract is not None and "contract" not in factors:
            factors["contract"] = {
                "tradingsymbol": getattr(contract, "tradingsymbol", None),
                "exchange": getattr(contract, "exchange", None),
                "instrument_token": getattr(contract, "instrument_token", None),
                "expiry": getattr(contract, "expiry", None),
                "strike": getattr(contract, "strike", None),
                "option_type": getattr(contract, "option_type", None),
                "lot_size": getattr(contract, "lot_size", None),
                "last_price": getattr(contract, "last_price", None),
                "bid": getattr(contract, "bid", None),
                "ask": getattr(contract, "ask", None),
                "open_interest": getattr(contract, "open_interest", None),
                "volume": getattr(contract, "volume", None),
            }
        quality = (
            factors.get("option_quality", {})
            if isinstance(factors.get("option_quality"), dict)
            else {}
        )
        premium = (
            factors.get("option_premium_confirmation", {})
            if isinstance(factors.get("option_premium_confirmation"), dict)
            else {}
        )
        session_label = str(market_session or self._market_session())
        learning = self._classify_learning(
            reasons=reasons,
            snapshot=snapshot,
            contract=contract,
            factor_scores=factors,
            rejection_source=rejection_source,
            market_session=session_label,
            learning_eligible_override=learning_eligible,
        )
        session = get_session()
        try:
            now = ist_now_naive()
            lineage = current_strategy_lineage()
            primary_gate = reasons[0] if reasons else None
            tradingsymbol = getattr(contract, "tradingsymbol", None)
            five_minute_marker = self._five_minute_marker(factors)
            duplicate = (
                session.query(RejectedOpportunityRecord)
                .filter(
                    RejectedOpportunityRecord.symbol == symbol.upper(),
                    RejectedOpportunityRecord.action == action,
                    RejectedOpportunityRecord.tradingsymbol == tradingsymbol,
                    RejectedOpportunityRecord.strategy_version
                    == str(lineage["strategy_version"]),
                    RejectedOpportunityRecord.config_hash
                    == str(lineage["config_hash"]),
                    RejectedOpportunityRecord.primary_gate == primary_gate,
                    RejectedOpportunityRecord.created_at
                    >= now
                    - timedelta(
                        seconds=max(300, int(settings.setup_episode_window_seconds))
                    ),
                )
                .order_by(RejectedOpportunityRecord.id.desc())
                .first()
            )
            if (
                duplicate is not None
                and self._five_minute_marker(
                    self._json_dict(duplicate.factor_scores_json)
                )
                == five_minute_marker
            ):
                if duplicate.episode_id:
                    episode = session.get(SetupEpisodeRecord, int(duplicate.episode_id))
                    if episode is not None:
                        episode.observation_count = (
                            int(episode.observation_count or 0) + 1
                        )
                        episode.updated_at = now
                session.commit()
                session.refresh(duplicate)
                session.expunge(duplicate)
                return duplicate
            episode_key = self._episode_key(
                timestamp=now,
                symbol=symbol,
                action=action,
                tradingsymbol=tradingsymbol,
                strategy_version=str(lineage["strategy_version"]),
                config_hash=str(lineage["config_hash"]),
            )
            episode = (
                session.query(SetupEpisodeRecord)
                .filter(SetupEpisodeRecord.episode_key == episode_key)
                .first()
            )
            if episode is None:
                episode = SetupEpisodeRecord(
                    created_at=now,
                    updated_at=now,
                    episode_key=episode_key,
                    symbol=symbol.upper(),
                    action=action,
                    side=side.upper(),
                    tradingsymbol=getattr(contract, "tradingsymbol", None),
                    strategy_version=str(lineage["strategy_version"]),
                    config_hash=str(lineage["config_hash"]),
                    observation_count=1,
                )
                session.add(episode)
                session.flush()
            else:
                episode.observation_count = int(episode.observation_count or 0) + 1
                episode.updated_at = now
            record = RejectedOpportunityRecord(
                created_at=now,
                symbol=symbol.upper(),
                action=action,
                side=side.upper(),
                tradingsymbol=getattr(contract, "tradingsymbol", None),
                exchange=getattr(contract, "exchange", None),
                expiry=getattr(contract, "expiry", None),
                strike=getattr(contract, "strike", None),
                option_type=getattr(contract, "option_type", None),
                score=int(score or 0),
                primary_gate=primary_gate,
                rejection_source=rejection_source,
                rejection_context=learning["rejection_context"],
                market_session=session_label,
                learning_eligible=1 if learning["learning_eligible"] else 0,
                learning_exclusion_reason=learning["learning_exclusion_reason"],
                reasons_json=json.dumps(reasons, default=str),
                market_state_json=json.dumps(snapshot or {}, default=str),
                option_quality_json=json.dumps(quality, default=str),
                premium_state_json=json.dumps(premium, default=str),
                score_breakdown_json=json.dumps(score_breakdown or {}, default=str),
                factor_scores_json=json.dumps(factors, default=str),
                episode_id=episode.id,
                episode_key=episode_key,
                strategy_version=str(lineage["strategy_version"]),
                config_hash=str(lineage["config_hash"]),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            session.expunge(record)
            return record
        finally:
            session.close()

    def list_rejections(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        limit: int = 100,
        learning_eligible: bool | None = None,
    ) -> list[RejectedOpportunityRecord]:
        session = get_session()
        try:
            query = session.query(RejectedOpportunityRecord).order_by(
                RejectedOpportunityRecord.id.desc()
            )
            if symbol:
                query = query.filter(RejectedOpportunityRecord.symbol == symbol.upper())
            if learning_eligible is not None:
                query = query.filter(
                    RejectedOpportunityRecord.learning_eligible
                    == (1 if learning_eligible else 0)
                )
            return query.limit(limit).all()
        finally:
            session.close()

    def list_pending_later_outcomes(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        limit: int = 100,
        learning_only: bool = True,
        after_id: int | None = None,
    ) -> list[RejectedOpportunityRecord]:
        session = get_session()
        try:
            query = (
                session.query(RejectedOpportunityRecord)
                .filter(RejectedOpportunityRecord.later_outcome.is_(None))
                .filter(RejectedOpportunityRecord.tradingsymbol.is_not(None))
                .order_by(RejectedOpportunityRecord.id.asc())
            )
            if symbol:
                query = query.filter(RejectedOpportunityRecord.symbol == symbol.upper())
            if learning_only:
                query = query.filter(RejectedOpportunityRecord.learning_eligible == 1)
            if after_id is not None:
                query = query.filter(RejectedOpportunityRecord.id > int(after_id))
            return query.limit(limit).all()
        finally:
            session.close()

    def mark_later_outcome(
        self,
        rejection_id: int,
        *,
        outcome: str,
        exit_price: float | None = None,
        notes: str | None = None,
        outcome_at: Any | None = None,
        outcome_minutes: float | None = None,
        outcome_source: str | None = None,
        outcome_timeframe: str | None = None,
        ambiguous: bool = False,
        confidence: str | None = None,
    ) -> RejectedOpportunityRecord:
        session = get_session()
        try:
            record = session.get(RejectedOpportunityRecord, rejection_id)
            if record is None:
                raise ValueError(f"rejected opportunity {rejection_id} was not found")
            record.later_outcome = outcome
            record.later_exit_price = exit_price
            record.later_outcome_at = outcome_at
            record.later_outcome_minutes = outcome_minutes
            record.later_outcome_source = outcome_source
            record.later_outcome_timeframe = outcome_timeframe
            record.later_outcome_ambiguous = 1 if ambiguous else 0
            record.later_outcome_confidence = confidence
            if str(outcome).startswith("censored_"):
                record.learning_eligible = 0
                record.learning_exclusion_reason = "censored_outcome"
            if record.episode_id:
                episode = session.get(SetupEpisodeRecord, int(record.episode_id))
                if episode is not None and episode.independent_outcome is None:
                    episode.independent_outcome = outcome
                    episode.outcome_source = outcome_source
                    episode.outcome_confidence = confidence
                    episode.updated_at = ist_now_naive()
            record.later_notes = notes
            record.later_evaluated_at = ist_now_naive()
            session.commit()
            session.refresh(record)
            if record.episode_key:
                try:
                    from app.services.decision_evidence_repository import (
                        DecisionEvidenceRepository,
                    )

                    DecisionEvidenceRepository().record_outcome(
                        episode_key=str(record.episode_key),
                        horizon=str(outcome_timeframe or "session_cutoff"),
                        outcome_source=str(
                            outcome_source or "rejected_opportunity_outcome"
                        ),
                        outcome={
                            "outcome": outcome,
                            "exit_price": exit_price,
                            "outcome_at": outcome_at,
                            "outcome_minutes": outcome_minutes,
                            "ambiguous": ambiguous,
                            "confidence": confidence,
                            "rejected": True,
                            "counterfactual_order_placed": False,
                        },
                    )
                except Exception:
                    pass
            return record
        finally:
            session.close()

    def analyze(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        limit: int = 1000,
        learning_eligible: bool | None = None,
    ) -> dict[str, Any]:
        rows = self.list_rejections(symbol=symbol, limit=limit)
        visible_rows = (
            rows
            if learning_eligible is None
            else [
                row for row in rows if bool(row.learning_eligible) is learning_eligible
            ]
        )
        independent_by_episode: dict[str, RejectedOpportunityRecord] = {}
        for row in visible_rows:
            independent_by_episode.setdefault(
                str(row.episode_key or f"legacy-row:{row.id}"), row
            )
        independent_rows = list(independent_by_episode.values())
        reason_counter: Counter[str] = Counter()
        gate_counter: Counter[str] = Counter()
        ce_pe: Counter[str] = Counter()
        later: Counter[str] = Counter()
        context_counter: Counter[str] = Counter()
        source_counter: Counter[str] = Counter()
        market_session_counter: Counter[str] = Counter()
        exclusion_counter: Counter[str] = Counter()
        for row in rows:
            context_counter.update([str(row.rejection_context or "unknown")])
            source_counter.update([str(row.rejection_source or "unknown")])
            market_session_counter.update([str(row.market_session or "unknown")])
            if not bool(row.learning_eligible):
                exclusion_counter.update(
                    [str(row.learning_exclusion_reason or "unknown")]
                )
        for row in independent_rows:
            gate_counter.update([str(row.primary_gate or "unknown")])
            ce_pe.update([str(row.option_type or "unknown")])
            if row.later_outcome:
                later.update([str(row.later_outcome)])
            for reason in self._json_list(row.reasons_json):
                reason_counter.update([reason])
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "filter": {"learning_eligible": learning_eligible},
            "sample": {
                "total_rejected": len(rows),
                "reported_rejected": len(visible_rows),
                "independent_episodes": len(independent_rows),
                "dependent_duplicate_observations": len(visible_rows)
                - len(independent_rows),
                "learning_eligible": len(
                    [row for row in rows if bool(row.learning_eligible)]
                ),
                "learning_excluded": len(
                    [row for row in rows if not bool(row.learning_eligible)]
                ),
                "with_later_outcome": sum(later.values()),
            },
            "rejection_contexts": dict(context_counter.most_common()),
            "rejection_sources": dict(source_counter.most_common()),
            "market_sessions": dict(market_session_counter.most_common()),
            "learning_exclusion_reasons": dict(exclusion_counter.most_common()),
            "top_primary_gates": dict(gate_counter.most_common(20)),
            "top_reasons": dict(reason_counter.most_common(30)),
            "ce_vs_pe": dict(ce_pe.most_common()),
            "later_outcomes": dict(later.most_common()),
            "examples": [self.to_dict(row) for row in independent_rows[:20]],
        }

    def to_dict(self, record: RejectedOpportunityRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "created_at": record.created_at.isoformat(sep=" ")
            if record.created_at
            else None,
            "symbol": record.symbol,
            "action": record.action,
            "side": record.side,
            "tradingsymbol": record.tradingsymbol,
            "expiry": record.expiry,
            "strike": record.strike,
            "option_type": record.option_type,
            "score": record.score,
            "primary_gate": record.primary_gate,
            "rejection_source": record.rejection_source,
            "rejection_context": record.rejection_context,
            "market_session": record.market_session,
            "learning_eligible": bool(record.learning_eligible),
            "learning_exclusion_reason": record.learning_exclusion_reason,
            "episode_id": record.episode_id,
            "episode_key": record.episode_key,
            "strategy_version": record.strategy_version,
            "config_hash": record.config_hash,
            "reasons": self._json_list(record.reasons_json),
            "later_outcome": record.later_outcome,
            "later_exit_price": record.later_exit_price,
            "later_outcome_at": record.later_outcome_at.isoformat(sep=" ")
            if record.later_outcome_at
            else None,
            "later_outcome_minutes": record.later_outcome_minutes,
            "later_outcome_source": record.later_outcome_source,
            "later_outcome_timeframe": record.later_outcome_timeframe,
            "later_outcome_ambiguous": bool(record.later_outcome_ambiguous),
            "later_outcome_confidence": record.later_outcome_confidence,
            "later_notes": record.later_notes,
        }

    def _episode_key(
        self,
        *,
        timestamp: datetime,
        symbol: str,
        action: str | None,
        tradingsymbol: str | None,
        strategy_version: str,
        config_hash: str,
    ) -> str:
        window = max(1, int(settings.setup_episode_window_seconds))
        bucket = int(timestamp.timestamp()) // window
        raw = "|".join(
            [
                symbol.upper(),
                str(action or "unknown").upper(),
                str(tradingsymbol or "unknown").upper(),
                str(bucket),
                strategy_version,
                config_hash,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _classify_learning(
        self,
        *,
        reasons: list[str],
        snapshot: dict[str, Any] | None,
        contract: Any | None,
        factor_scores: dict[str, Any],
        rejection_source: str,
        market_session: str,
        learning_eligible_override: bool | None,
    ) -> dict[str, Any]:
        source = str(rejection_source or "scanner")
        if learning_eligible_override is not None:
            return {
                "learning_eligible": bool(learning_eligible_override),
                "rejection_context": "strategy_rejection"
                if learning_eligible_override
                else "manual_override_excluded",
                "learning_exclusion_reason": None
                if learning_eligible_override
                else "manual_override",
            }
        if source == "manual_diagnostic":
            return {
                "learning_eligible": False,
                "rejection_context": "manual_diagnostic",
                "learning_exclusion_reason": "manual_diagnostic",
            }
        if market_session != "REGULAR_MARKET":
            return {
                "learning_eligible": False,
                "rejection_context": "market_closed",
                "learning_exclusion_reason": f"market_session:{market_session.lower()}",
            }
        if snapshot is not None and snapshot.get("is_real_data") is False:
            return {
                "learning_eligible": False,
                "rejection_context": "data_unavailable",
                "learning_exclusion_reason": "mock_or_fallback_snapshot",
            }
        normalized_reasons = " | ".join(str(reason).lower() for reason in reasons)
        if any(
            marker in normalized_reasons
            for marker in self.DATA_OR_SESSION_REASON_MARKERS
        ):
            return {
                "learning_eligible": False,
                "rejection_context": "data_or_session_rejection",
                "learning_exclusion_reason": self._first_matching_marker(
                    normalized_reasons, self.DATA_OR_SESSION_REASON_MARKERS
                ),
            }
        if any(
            marker in normalized_reasons
            for marker in self.OPERATIONAL_RISK_REASON_MARKERS
        ):
            return {
                "learning_eligible": False,
                "rejection_context": "operational_risk_rejection",
                "learning_exclusion_reason": self._first_matching_marker(
                    normalized_reasons, self.OPERATIONAL_RISK_REASON_MARKERS
                ),
            }
        freshness = (
            factor_scores.get("data_freshness", {})
            if isinstance(factor_scores.get("data_freshness"), dict)
            else {}
        )
        data_quality = (
            factor_scores.get("data_quality", {})
            if isinstance(factor_scores.get("data_quality"), dict)
            else {}
        )
        premium = (
            factor_scores.get("option_premium_confirmation", {})
            if isinstance(factor_scores.get("option_premium_confirmation"), dict)
            else {}
        )
        premium_details = (
            premium.get("details", {})
            if isinstance(premium.get("details"), dict)
            else {}
        )
        if freshness and not bool(freshness.get("passed", False)):
            return {
                "learning_eligible": False,
                "rejection_context": "data_or_session_rejection",
                "learning_exclusion_reason": "data_freshness_failed",
            }
        if data_quality and not bool(data_quality.get("passed", False)):
            return {
                "learning_eligible": False,
                "rejection_context": "data_or_session_rejection",
                "learning_exclusion_reason": "data_quality_failed",
            }
        if (
            premium_details
            and premium_details.get("premium_candle_freshness_passed") is False
        ):
            return {
                "learning_eligible": False,
                "rejection_context": "data_or_session_rejection",
                "learning_exclusion_reason": "premium_candle_freshness_failed",
            }
        if contract is None or not getattr(contract, "tradingsymbol", None):
            return {
                "learning_eligible": False,
                "rejection_context": "data_or_session_rejection",
                "learning_exclusion_reason": "contract_unavailable",
            }
        return {
            "learning_eligible": True,
            "rejection_context": "strategy_rejection",
            "learning_exclusion_reason": None,
        }

    def _market_session(self) -> str:
        now = ist_now()
        if now.weekday() >= 5:
            return "WEEKEND"
        start = self._parse_time(settings.market_open_time)
        end = self._parse_time(settings.market_close_time)
        if start <= now.time() <= end:
            return "REGULAR_MARKET"
        return "PRE_MARKET" if now.time() < start else "AFTER_MARKET"

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))

    def _first_matching_marker(self, text: str, markers: tuple[str, ...]) -> str:
        for marker in markers:
            if marker in text:
                return marker.replace(" ", "_")
        return "unknown"

    def _json_list(self, value: str | None) -> list[str]:
        if not value:
            return []
        try:
            data = json.loads(value)
            return [str(item) for item in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def _json_dict(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return dict(data) if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _five_minute_marker(self, factors: dict[str, Any]) -> str | None:
        mtf = (
            factors.get("multi_timeframe", {})
            if isinstance(factors.get("multi_timeframe"), dict)
            else {}
        )
        for frame in (
            mtf.get("frames", []) if isinstance(mtf.get("frames"), list) else []
        ):
            if isinstance(frame, dict) and str(frame.get("timeframe")) == "5minute":
                return str(frame.get("last_completed_at") or "") or None
        snapshot = (
            factors.get("rejection_snapshot", {})
            if isinstance(factors.get("rejection_snapshot"), dict)
            else {}
        )
        return (
            str(
                snapshot.get("five_minute_candle_at")
                or snapshot.get("candle_timestamp")
                or ""
            )
            or None
        )
