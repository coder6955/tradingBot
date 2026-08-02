from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.services.database import DecisionOutcomeRecord, DecisionRiskEvidenceRecord, get_session
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


class DecisionEvidenceRepository:
    """Append-only decision/risk evidence and episode outcome persistence."""

    HORIZONS = ("30_seconds", "1_minute", "3_minutes", "5_minutes", "15_minutes", "session_cutoff")

    def record_decision(
        self,
        *,
        decision_type: str,
        final_state: str,
        symbol: str = "BANKNIFTY",
        tradingsymbol: str | None = None,
        episode_key: str | None = None,
        context: dict[str, Any] | None = None,
        gate_results: dict[str, Any] | None = None,
        active_risk_decision: dict[str, Any] | None = None,
        shadow_risk_decisions: dict[str, Any] | None = None,
        transition_timestamps: dict[str, Any] | None = None,
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        lineage = current_strategy_lineage()
        payload = dict(context or {})
        now = ist_now_naive()
        record = DecisionRiskEvidenceRecord(
            created_at=now,
            decision_id=decision_id or uuid.uuid4().hex,
            episode_key=episode_key,
            decision_type=str(decision_type),
            final_state=str(final_state),
            symbol=str(symbol or "BANKNIFTY").upper(),
            tradingsymbol=tradingsymbol,
            strategy_version=str(payload.get("strategy_version") or lineage["strategy_version"]),
            config_hash=str(payload.get("config_hash") or lineage["config_hash"]),
            risk_policy_version=str(settings.risk_policy_version),
            trading_date=str(payload.get("trading_date") or now.date().isoformat()),
            session_phase=str(payload.get("session_phase") or payload.get("market_session") or "unknown"),
            decision_context_json=json.dumps(payload, default=str, sort_keys=True),
            gate_results_json=json.dumps(gate_results or {}, default=str, sort_keys=True),
            active_risk_decision_json=json.dumps(active_risk_decision, default=str, sort_keys=True) if active_risk_decision is not None else None,
            shadow_risk_decisions_json=json.dumps(shadow_risk_decisions, default=str, sort_keys=True) if shadow_risk_decisions is not None else None,
            transition_timestamps_json=json.dumps(transition_timestamps or {}, default=str, sort_keys=True),
        )
        session = get_session()
        try:
            session.add(record)
            try:
                session.commit()
                session.refresh(record)
                return {"id": record.id, "decision_id": record.decision_id, "episode_key": record.episode_key, "deduplicated": False}
            except IntegrityError:
                session.rollback()
                existing = session.query(DecisionRiskEvidenceRecord).filter(DecisionRiskEvidenceRecord.decision_id == record.decision_id).first()
                if existing is None:
                    raise
                return {"id": existing.id, "decision_id": existing.decision_id, "episode_key": existing.episode_key, "deduplicated": True}
        finally:
            session.close()

    def record_outcome(
        self,
        *,
        episode_key: str,
        horizon: str,
        outcome_source: str,
        outcome: dict[str, Any],
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        lineage = current_strategy_lineage()
        record = DecisionOutcomeRecord(
            created_at=ist_now_naive(),
            decision_id=decision_id,
            episode_key=episode_key,
            horizon=str(horizon),
            outcome_source=str(outcome_source),
            outcome_json=json.dumps(outcome, default=str, sort_keys=True),
            strategy_version=str(lineage["strategy_version"]),
            config_hash=str(lineage["config_hash"]),
            risk_policy_version=str(settings.risk_policy_version),
        )
        session = get_session()
        try:
            existing = (
                session.query(DecisionOutcomeRecord)
                .filter(
                    DecisionOutcomeRecord.episode_key == episode_key,
                    DecisionOutcomeRecord.horizon == str(horizon),
                    DecisionOutcomeRecord.outcome_source == str(outcome_source),
                )
                .first()
            )
            if existing is not None:
                return {"id": existing.id, "episode_key": existing.episode_key, "horizon": existing.horizon, "deduplicated": True}
            session.add(record)
            try:
                session.commit()
                session.refresh(record)
                return {"id": record.id, "episode_key": record.episode_key, "horizon": record.horizon, "deduplicated": False}
            except IntegrityError:
                session.rollback()
                existing = (
                    session.query(DecisionOutcomeRecord)
                    .filter(
                        DecisionOutcomeRecord.episode_key == episode_key,
                        DecisionOutcomeRecord.horizon == str(horizon),
                        DecisionOutcomeRecord.outcome_source == str(outcome_source),
                    )
                    .first()
                )
                if existing is None:
                    raise
                return {"id": existing.id, "episode_key": existing.episode_key, "horizon": existing.horizon, "deduplicated": True}
        finally:
            session.close()

    def counterfactual_tier_outcomes(
        self,
        *,
        entry_price: float,
        exit_price: float,
        account_equity: float,
        shadow_decisions: dict[str, dict[str, Any]],
        charges_per_unit: float = 0.0,
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for tier, decision in shadow_decisions.items():
            quantity = int(decision.get("counterfactual_maximum_quantity") or decision.get("maximum_quantity") or 0)
            pnl = ((float(exit_price) - float(entry_price)) - float(charges_per_unit)) * quantity
            results[tier] = {
                "hypothetical_only": True,
                "quantity": quantity,
                "rupee_result": round(pnl, 2),
                "return_on_equity_percent": round((pnl / max(float(account_equity), 0.01)) * 100.0, 4),
                "approved_risk_percent": decision.get("approved_risk_percent"),
                "approved_risk_amount": decision.get("approved_risk_amount"),
            }
        return {
            "tiers": results,
            "risk_of_ruin": None,
            "risk_of_ruin_reason": "not_calculated_without_disclosed_independent_sample_and_distribution_assumptions",
        }

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        session = get_session()
        try:
            row = session.query(DecisionRiskEvidenceRecord).filter(DecisionRiskEvidenceRecord.decision_id == decision_id).first()
            if row is None:
                return None
            return {
                "decision_id": row.decision_id,
                "episode_key": row.episode_key,
                "decision_type": row.decision_type,
                "final_state": row.final_state,
                "context": json.loads(row.decision_context_json),
                "gates": json.loads(row.gate_results_json),
                "active_risk_decision": json.loads(row.active_risk_decision_json) if row.active_risk_decision_json else None,
                "shadow_risk_decisions": json.loads(row.shadow_risk_decisions_json) if row.shadow_risk_decisions_json else None,
                "created_at": row.created_at.isoformat(sep=" ") if isinstance(row.created_at, datetime) else str(row.created_at),
            }
        finally:
            session.close()
