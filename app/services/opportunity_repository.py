from __future__ import annotations

import json
from dataclasses import asdict
from collections import Counter
from typing import Any

from app.models import Signal
from app.services.database import OpportunityRecord, get_session
from app.services.time_utils import ist_now_naive


class OpportunityRepository:
    """Persist scanner opportunities and their later trading outcomes."""

    def save_opportunity(self, signal: Signal) -> OpportunityRecord:
        payload = asdict(signal)
        session = get_session()
        try:
            record = OpportunityRecord(
                symbol=signal.symbol,
                action=signal.action,
                side=signal.side,
                tradingsymbol=signal.tradingsymbol,
                exchange=signal.exchange,
                expiry=signal.expiry,
                strike=signal.strike,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                target_1=signal.target_1,
                target_2=signal.target_2,
                target_3=signal.target_3,
                quantity=signal.quantity,
                lot_size=signal.lot_size,
                score=signal.score,
                probability=signal.probability,
                risk_reward=signal.risk_reward,
                signal_json=json.dumps(payload, default=str),
                factor_scores_json=json.dumps(signal.factor_scores, default=str),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def list_opportunities(self, status: str | None = None, limit: int = 50) -> list[OpportunityRecord]:
        session = get_session()
        try:
            query = session.query(OpportunityRecord).order_by(OpportunityRecord.id.desc())
            if status:
                query = query.filter(OpportunityRecord.status == status)
            return query.limit(limit).all()
        finally:
            session.close()

    def get_opportunity(self, opportunity_id: int) -> OpportunityRecord | None:
        session = get_session()
        try:
            return session.get(OpportunityRecord, opportunity_id)
        finally:
            session.close()

    def update_outcome(
        self,
        opportunity_id: int,
        *,
        outcome: str,
        exit_price: float | None = None,
        review_notes: str | None = None,
        failure_tags: list[str] | None = None,
    ) -> OpportunityRecord:
        session = get_session()
        try:
            record = session.get(OpportunityRecord, opportunity_id)
            if record is None:
                raise ValueError(f"opportunity {opportunity_id} was not found")

            record.status = "closed"
            record.outcome = outcome
            record.exit_price = exit_price
            record.closed_at = ist_now_naive()
            record.review_notes = review_notes
            record.failure_tags_json = json.dumps(failure_tags or [])
            if exit_price is not None and record.entry_price is not None:
                multiplier = 1 if record.side == "BUY" else -1
                record.pnl = (exit_price - record.entry_price) * record.quantity * multiplier

            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def summarize_performance(self) -> dict[str, Any]:
        records = self.list_opportunities(limit=1000)
        closed = [record for record in records if record.status == "closed"]
        wins = [record for record in closed if record.outcome in {"target_1", "target_2", "target_3", "winner"}]
        losses = [record for record in closed if record.outcome in {"stop_loss", "false_signal", "loser"}]
        pnl_values = [float(record.pnl or 0.0) for record in closed]
        return {
            "total": len(records),
            "open": len([record for record in records if record.status == "open"]),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
            "pnl": round(sum(pnl_values), 2),
        }

    def failure_analysis(self) -> dict[str, Any]:
        records = self.list_opportunities(limit=1000)
        failed = [record for record in records if record.outcome in {"stop_loss", "false_signal", "loser", "expired"}]
        tag_counter: Counter[str] = Counter()
        by_symbol: Counter[str] = Counter()
        by_action: Counter[str] = Counter()
        examples: list[dict[str, Any]] = []
        for record in failed:
            by_symbol.update([record.symbol])
            by_action.update([record.action])
            try:
                tags = json.loads(record.failure_tags_json or "[]")
            except json.JSONDecodeError:
                tags = []
            tag_counter.update(str(tag) for tag in tags)
            if len(examples) < 10:
                examples.append(
                    {
                        "id": record.id,
                        "symbol": record.symbol,
                        "action": record.action,
                        "tradingsymbol": record.tradingsymbol,
                        "outcome": record.outcome,
                        "entry_price": record.entry_price,
                        "exit_price": record.exit_price,
                        "score": record.score,
                        "tags": tags,
                        "review_notes": record.review_notes,
                    }
                )
        return {
            "failed_count": len(failed),
            "top_failure_tags": dict(tag_counter.most_common(20)),
            "by_symbol": dict(by_symbol.most_common(20)),
            "by_action": dict(by_action.most_common(20)),
            "examples": examples,
        }
