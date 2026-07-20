from __future__ import annotations

import json
from typing import Any

from app.services.database import OpportunityRecord, get_session
from app.services.trading_metrics import minutes_between, score_bucket, summarize_groups, summarize_returns, time_bucket


class OpportunityAnalyticsService:
    """Study saved scanner opportunities, including signals that were not live trades."""

    def analyze(self, *, symbol: str | None = "BANKNIFTY", limit: int = 1000) -> dict[str, Any]:
        records = self._records(symbol=symbol, limit=limit)
        closed = [record for record in records if record.status == "closed" and record.outcome]
        lineage_groups: dict[str, list[OpportunityRecord]] = {}
        for record in closed:
            key = f"{record.strategy_version or 'legacy'}|{record.config_hash or 'unknown'}"
            lineage_groups.setdefault(key, []).append(record)
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "sample": {
                "total_saved": len(records),
                "closed": len(closed),
                "open": len([record for record in records if record.status != "closed"]),
                "note": "This studies saved scanner opportunities, not only executed trades.",
            },
            "overall": self._summary(closed),
            "overall_by_lineage": {key: self._summary(items) for key, items in lineage_groups.items()},
            "mixed_lineage": len(lineage_groups) > 1,
            "lineage_warning": "Do not use the combined overall result for readiness; compare matching config hashes." if len(lineage_groups) > 1 else None,
            "segments": {
                "ce_vs_pe": self._groups(closed, self._ce_pe),
                "expiry_day": self._groups(closed, self._expiry_day),
                "time_bucket": self._groups(closed, lambda record: time_bucket(record.created_at)),
                "score_bucket": self._groups(closed, lambda record: score_bucket(record.score)),
                "setup": self._groups(closed, self._setup_type),
                "failure_tag": self._failure_tag_groups(closed),
            },
            "data_quality": self._data_quality(records),
        }

    def _records(self, *, symbol: str | None, limit: int) -> list[OpportunityRecord]:
        session = get_session()
        try:
            query = session.query(OpportunityRecord).order_by(OpportunityRecord.id.desc())
            if symbol:
                query = query.filter(OpportunityRecord.symbol == symbol.upper())
            return query.limit(limit).all()
        finally:
            session.close()

    def _summary(self, records: list[OpportunityRecord]) -> dict[str, Any]:
        return summarize_returns(
            records,
            pnl_fn=lambda record: float(record.pnl or 0.0),
            outcome_fn=lambda record: record.outcome,
            time_in_trade_fn=lambda record: minutes_between(record.created_at, record.closed_at),
        )

    def _groups(self, records: list[OpportunityRecord], key_fn: Any) -> dict[str, Any]:
        return summarize_groups(
            records,
            key_fn,
            pnl_fn=lambda record: float(record.pnl or 0.0),
            outcome_fn=lambda record: record.outcome,
            time_in_trade_fn=lambda record: minutes_between(record.created_at, record.closed_at),
        )

    def _failure_tag_groups(self, records: list[OpportunityRecord]) -> dict[str, Any]:
        expanded: list[tuple[str, OpportunityRecord]] = []
        for record in records:
            for tag in self._failure_tags(record):
                expanded.append((tag, record))
        groups: dict[str, list[OpportunityRecord]] = {}
        for tag, record in expanded:
            groups.setdefault(tag, []).append(record)
        return {tag: self._summary(items) for tag, items in sorted(groups.items())}

    def _data_quality(self, records: list[OpportunityRecord]) -> dict[str, Any]:
        closed = [record for record in records if record.status == "closed"]
        with_factor_scores = [record for record in records if record.factor_scores_json]
        return {
            "closed_sample_enough_for_learning": len(closed) >= 50,
            "closed_count": len(closed),
            "factor_score_coverage_pct": round((len(with_factor_scores) / len(records)) * 100, 2) if records else 0.0,
            "recommendation": "Collect at least 50 closed Bank Nifty opportunities before using this as a blocking guard.",
        }

    def _ce_pe(self, record: OpportunityRecord) -> str:
        action = str(record.action or "").upper()
        symbol = str(record.tradingsymbol or "").upper()
        if "CE" in action or symbol.endswith("CE"):
            return "CE"
        if "PE" in action or symbol.endswith("PE"):
            return "PE"
        return "unknown"

    def _expiry_day(self, record: OpportunityRecord) -> str:
        if not record.expiry or not record.created_at:
            return "non_expiry_or_unknown"
        return "expiry_day" if record.created_at.date().isoformat() == str(record.expiry)[:10] else "non_expiry"

    def _setup_type(self, record: OpportunityRecord) -> str:
        payload = self._json(record.signal_json)
        setup = str(payload.get("setup_type") or "").strip()
        if setup:
            return setup
        return str(record.action or "unknown")

    def _failure_tags(self, record: OpportunityRecord) -> list[str]:
        value = self._json(record.failure_tags_json, default=[])
        if isinstance(value, list):
            return [str(item) for item in value if str(item)]
        return []

    def _json(self, value: str | None, default: Any | None = None) -> Any:
        if not value:
            return {} if default is None else default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {} if default is None else default
