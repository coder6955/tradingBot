from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.services.backtest_service import BacktestService
from app.services.strategy_validation_repository import StrategyValidationRepository
from app.services.time_utils import format_ist, ist_now_naive


class StrategyEdgeService:
    """Decide whether a setup has enough measured edge to allow live scanning/execution."""

    STRATEGY_NAME = "directional_option_buy_v1"

    def __init__(
        self,
        *,
        backtest_service: BacktestService | None = None,
        repository: StrategyValidationRepository | None = None,
    ) -> None:
        self.backtest_service = backtest_service or BacktestService()
        self.repository = repository or StrategyValidationRepository()
        self.cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

    def evaluate(
        self,
        *,
        symbol: str,
        direction: str,
        timeframe: str = "5minute",
        refresh: bool = False,
    ) -> dict[str, Any]:
        direction = direction.upper()
        cache_key = f"{symbol.upper()}|{direction}|{timeframe}"
        cached = self.cache.get(cache_key)
        if (
            cached
            and not refresh
            and ist_now_naive() - cached[0]
            < timedelta(seconds=settings.strategy_edge_cache_seconds)
        ):
            return cached[1]

        latest = (
            None
            if refresh
            else self.repository.latest(
                strategy_name=self.STRATEGY_NAME,
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
            )
        )
        if latest is None:
            result = self.validate(
                symbol=symbol, direction=direction, timeframe=timeframe
            )
        else:
            result = self._record_to_evaluation(latest)
        self.cache[cache_key] = (ist_now_naive(), result)
        return result

    def validate(
        self,
        *,
        symbol: str,
        direction: str = "BOTH",
        timeframe: str = "5minute",
        limit: int = 3000,
    ) -> dict[str, Any]:
        result = self.backtest_service.run_walk_forward(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            limit=limit,
        )
        passed = bool(result.get("passed"))
        record = self.repository.save(
            strategy_name=self.STRATEGY_NAME,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            mode="walk_forward_option",
            result=result,
            passed=passed,
        )
        return self._record_to_evaluation(record)

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [
            self._record_to_evaluation(record)
            for record in self.repository.recent(limit=limit)
        ]

    def _record_to_evaluation(self, record: Any) -> dict[str, Any]:
        try:
            result = json.loads(record.result_json)
        except Exception:
            result = {}
        reasons = result.get("reasons") or (
            [] if bool(record.passed) else ["strategy validation did not pass"]
        )
        return {
            "strategy_name": record.strategy_name,
            "symbol": record.symbol,
            "timeframe": record.timeframe,
            "direction": record.direction,
            "mode": record.mode,
            "created_at": format_ist(record.created_at),
            "passed": bool(record.passed),
            "reasons": reasons,
            "summary": {
                "trades": record.trades,
                "win_rate": record.win_rate,
                "expectancy_pct": record.expectancy_pct,
                "profit_factor": record.profit_factor,
                "max_drawdown_pct": record.max_drawdown_pct,
            },
            "thresholds": {
                "min_trades": settings.min_strategy_trades,
                "min_expectancy_pct": settings.min_strategy_expectancy_pct,
                "min_profit_factor": settings.min_strategy_profit_factor,
                "min_win_rate_pct": settings.min_strategy_win_rate_pct,
            },
        }
