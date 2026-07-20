from __future__ import annotations

import json
from typing import Any

from app.services.database import StrategyValidationRecord, get_session
from app.services.strategy_lineage_service import current_strategy_lineage


class StrategyValidationRepository:
    """Persist measured strategy edge so execution can be gated by evidence."""

    def save(self, *, strategy_name: str, symbol: str, timeframe: str, direction: str, mode: str, result: dict[str, Any], passed: bool) -> StrategyValidationRecord:
        summary = result.get("summary") or result.get("test_summary") or {}
        lineage = current_strategy_lineage()
        record = StrategyValidationRecord(
            strategy_name=strategy_name,
            symbol=symbol.upper(),
            timeframe=timeframe,
            direction=direction.upper(),
            mode=mode,
            trades=int(summary.get("trades") or 0),
            win_rate=float(summary.get("win_rate") or 0.0),
            expectancy_pct=float(summary.get("expectancy_pct") or 0.0),
            profit_factor=self._optional_float(summary.get("profit_factor")),
            max_drawdown_pct=float(summary.get("max_drawdown_pct") or 0.0),
            passed=1 if passed else 0,
            strategy_version=str(lineage["strategy_version"]),
            config_hash=str(lineage["config_hash"]),
            fold_count=int(result.get("fold_count") or 0),
            out_of_sample_sessions=int(result.get("out_of_sample_sessions") or 0),
            result_json=json.dumps(result, default=str),
        )
        session = get_session()
        try:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def latest(self, *, strategy_name: str, symbol: str, timeframe: str, direction: str, mode: str = "walk_forward_option") -> StrategyValidationRecord | None:
        session = get_session()
        try:
            return (
                session.query(StrategyValidationRecord)
                .filter(
                    StrategyValidationRecord.strategy_name == strategy_name,
                    StrategyValidationRecord.symbol == symbol.upper(),
                    StrategyValidationRecord.timeframe == timeframe,
                    StrategyValidationRecord.direction == direction.upper(),
                    StrategyValidationRecord.mode == mode,
                )
                .order_by(StrategyValidationRecord.id.desc())
                .first()
            )
        finally:
            session.close()

    def recent(self, *, limit: int = 50) -> list[StrategyValidationRecord]:
        session = get_session()
        try:
            return session.query(StrategyValidationRecord).order_by(StrategyValidationRecord.id.desc()).limit(limit).all()
        finally:
            session.close()

    def _optional_float(self, value: Any) -> float | None:
        if value is None:
            return None
        return float(value)
