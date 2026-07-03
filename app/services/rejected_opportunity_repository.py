from __future__ import annotations

import json
from collections import Counter
from typing import Any

from app.services.database import RejectedOpportunityRecord, get_session
from app.services.time_utils import ist_now_naive


class RejectedOpportunityRepository:
    """Persist rejected scanner setups for later no-trade review."""

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
    ) -> RejectedOpportunityRecord:
        factors = factor_scores or {}
        quality = factors.get("option_quality", {}) if isinstance(factors.get("option_quality"), dict) else {}
        premium = factors.get("option_premium_confirmation", {}) if isinstance(factors.get("option_premium_confirmation"), dict) else {}
        session = get_session()
        try:
            record = RejectedOpportunityRecord(
                symbol=symbol.upper(),
                action=action,
                side=side.upper(),
                tradingsymbol=getattr(contract, "tradingsymbol", None),
                exchange=getattr(contract, "exchange", None),
                expiry=getattr(contract, "expiry", None),
                strike=getattr(contract, "strike", None),
                option_type=getattr(contract, "option_type", None),
                score=int(score or 0),
                primary_gate=reasons[0] if reasons else None,
                reasons_json=json.dumps(reasons, default=str),
                market_state_json=json.dumps(snapshot or {}, default=str),
                option_quality_json=json.dumps(quality, default=str),
                premium_state_json=json.dumps(premium, default=str),
                score_breakdown_json=json.dumps(score_breakdown or {}, default=str),
                factor_scores_json=json.dumps(factors, default=str),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def list_rejections(self, *, symbol: str | None = "BANKNIFTY", limit: int = 100) -> list[RejectedOpportunityRecord]:
        session = get_session()
        try:
            query = session.query(RejectedOpportunityRecord).order_by(RejectedOpportunityRecord.id.desc())
            if symbol:
                query = query.filter(RejectedOpportunityRecord.symbol == symbol.upper())
            return query.limit(limit).all()
        finally:
            session.close()

    def list_pending_later_outcomes(self, *, symbol: str | None = "BANKNIFTY", limit: int = 100) -> list[RejectedOpportunityRecord]:
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
    ) -> RejectedOpportunityRecord:
        session = get_session()
        try:
            record = session.get(RejectedOpportunityRecord, rejection_id)
            if record is None:
                raise ValueError(f"rejected opportunity {rejection_id} was not found")
            record.later_outcome = outcome
            record.later_exit_price = exit_price
            record.later_notes = notes
            record.later_evaluated_at = ist_now_naive()
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def analyze(self, *, symbol: str | None = "BANKNIFTY", limit: int = 1000) -> dict[str, Any]:
        rows = self.list_rejections(symbol=symbol, limit=limit)
        reason_counter: Counter[str] = Counter()
        gate_counter: Counter[str] = Counter()
        ce_pe: Counter[str] = Counter()
        later: Counter[str] = Counter()
        for row in rows:
            gate_counter.update([str(row.primary_gate or "unknown")])
            ce_pe.update([str(row.option_type or "unknown")])
            if row.later_outcome:
                later.update([str(row.later_outcome)])
            for reason in self._json_list(row.reasons_json):
                reason_counter.update([reason])
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "sample": {"total_rejected": len(rows), "with_later_outcome": sum(later.values())},
            "top_primary_gates": dict(gate_counter.most_common(20)),
            "top_reasons": dict(reason_counter.most_common(30)),
            "ce_vs_pe": dict(ce_pe.most_common()),
            "later_outcomes": dict(later.most_common()),
            "examples": [self.to_dict(row) for row in rows[:20]],
        }

    def to_dict(self, record: RejectedOpportunityRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "created_at": record.created_at.isoformat(sep=" ") if record.created_at else None,
            "symbol": record.symbol,
            "action": record.action,
            "side": record.side,
            "tradingsymbol": record.tradingsymbol,
            "expiry": record.expiry,
            "strike": record.strike,
            "option_type": record.option_type,
            "score": record.score,
            "primary_gate": record.primary_gate,
            "reasons": self._json_list(record.reasons_json),
            "later_outcome": record.later_outcome,
            "later_exit_price": record.later_exit_price,
        }

    def _json_list(self, value: str | None) -> list[str]:
        if not value:
            return []
        try:
            data = json.loads(value)
            return [str(item) for item in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
