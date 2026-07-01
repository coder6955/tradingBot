from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import settings
from app.services.backtest_service import BacktestService


class TimeBucketEdgeService:
    """Estimate whether this time of day has historically supported the setup."""

    def __init__(self, backtest_service: BacktestService | None = None) -> None:
        self.backtest_service = backtest_service or BacktestService()
        self.cache: dict[str, dict[str, Any]] = {}

    def evaluate(self, *, symbol: str, trend: str, timeframe: str = "5minute") -> dict[str, Any]:
        direction = "CALL" if trend.lower() == "bullish" else "PUT"
        bucket = self._current_bucket()
        result = self._bucket_stats(symbol=symbol.upper(), timeframe=timeframe, direction=direction)
        stats = result.get(bucket) or {"trades": 0, "expectancy_pct": 0.0, "win_rate": 0.0}
        reasons: list[str] = []
        passed = True
        if int(stats.get("trades") or 0) < settings.min_time_bucket_trades:
            reasons.append("not enough historical trades in this time bucket")
            passed = not settings.enable_time_bucket_filter
        elif float(stats.get("expectancy_pct") or 0.0) < settings.min_time_bucket_expectancy_pct:
            reasons.append("time bucket expectancy is below threshold")
            passed = False

        return {
            "enabled": settings.enable_time_bucket_filter,
            "passed": passed,
            "reasons": reasons,
            "details": {
                "bucket": bucket,
                "direction": direction,
                "stats": stats,
                "all_buckets": result,
            },
        }

    def _bucket_stats(self, *, symbol: str, timeframe: str, direction: str) -> dict[str, Any]:
        key = f"{symbol}|{timeframe}|{direction}"
        if key in self.cache:
            return self.cache[key]
        replay = self.backtest_service.run_option_premium(symbol=symbol, timeframe=timeframe, direction=direction, limit=3000)
        buckets: dict[str, list[float]] = {}
        for item in replay.get("examples", []):
            bucket = self._bucket_for_timestamp(str(item.get("timestamp") or ""))
            if bucket:
                buckets.setdefault(bucket, []).append(float(item.get("pnl_pct") or 0.0))
        stats = {
            bucket: {
                "trades": len(values),
                "expectancy_pct": round(sum(values) / len(values), 3) if values else 0.0,
                "win_rate": round((len([value for value in values if value > 0]) / len(values)) * 100, 2) if values else 0.0,
            }
            for bucket, values in buckets.items()
        }
        self.cache[key] = stats
        return stats

    def _current_bucket(self) -> str:
        now = datetime.now()
        return self._bucket_for_time(now.hour, now.minute)

    def _bucket_for_timestamp(self, value: str) -> str | None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return self._bucket_for_time(parsed.hour, parsed.minute)

    def _bucket_for_time(self, hour: int, minute: int) -> str:
        total = hour * 60 + minute
        if total < 10 * 60:
            return "open_early"
        if total < 11 * 60 + 30:
            return "morning"
        if total < 13 * 60 + 30:
            return "midday"
        if total < 14 * 60 + 45:
            return "afternoon"
        return "closing"
