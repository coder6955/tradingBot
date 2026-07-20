from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.backtest_service import BacktestService
from app.services.execution_analytics_service import ExecutionAnalyticsService
from app.services.opportunity_analytics_service import OpportunityAnalyticsService
from app.services.option_history_repository import OptionHistoryRepository


class ProfessionalReadinessService:
    """Combine research evidence into a professional-readiness report."""

    def __init__(
        self,
        *,
        backtest_service: BacktestService,
        opportunity_analytics_service: OpportunityAnalyticsService,
        execution_analytics_service: ExecutionAnalyticsService,
        option_history_repository: OptionHistoryRepository,
    ) -> None:
        self.backtest_service = backtest_service
        self.opportunity_analytics_service = opportunity_analytics_service
        self.execution_analytics_service = execution_analytics_service
        self.option_history_repository = option_history_repository

    def report(
        self,
        *,
        symbol: str = "BANKNIFTY",
        timeframe: str = "5minute",
        direction: str = "BOTH",
        limit: int = 3000,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        option_coverage = self.option_history_repository.coverage_summary(symbol)
        opportunities = self.opportunity_analytics_service.analyze(symbol=symbol, limit=limit)
        execution = self.execution_analytics_service.analyze(symbol=symbol, limit=limit)
        option_backtest = self.backtest_service.run_option_premium(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            limit=limit,
        )
        walk_forward = self.backtest_service.run_walk_forward(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            limit=limit,
        )
        ablation = self.backtest_service.run_ablation(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            limit=min(limit, 1500),
        )
        checks = self._checks(
            option_coverage=option_coverage,
            opportunities=opportunities,
            execution=execution,
            option_backtest=option_backtest,
            walk_forward=walk_forward,
        )
        return {
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "verdict": self._verdict(checks),
            "checks": checks,
            "option_data_coverage": option_coverage,
            "opportunity_learning": opportunities,
            "execution_quality": execution,
            "option_backtest": option_backtest,
            "walk_forward": walk_forward,
            "ablation": ablation,
            "professional_notes": [
                "High win rate is less important than positive net expectancy after slippage and charges.",
                "Use this report to decide whether paper evidence is strong enough before enabling live automation.",
                "Do not promote a factor to a hard gate unless this report shows it improves out-of-sample expectancy.",
            ],
        }

    def _checks(
        self,
        *,
        option_coverage: dict[str, Any],
        opportunities: dict[str, Any],
        execution: dict[str, Any],
        option_backtest: dict[str, Any],
        walk_forward: dict[str, Any],
    ) -> dict[str, Any]:
        opportunity_overall = opportunities.get("overall", {})
        execution_overall = execution.get("overall", {})
        execution_quality = execution.get("execution_quality", {})
        backtest_summary = option_backtest.get("summary", {})
        walk_summary = walk_forward.get("summary", {})
        return {
            "strategy_lineage_consistency": {
                "passed": not bool(opportunities.get("mixed_lineage")) and not bool(execution.get("mixed_lineage")),
                "opportunity_mixed_lineage": bool(opportunities.get("mixed_lineage")),
                "execution_mixed_lineage": bool(execution.get("mixed_lineage")),
                "message": "Readiness never combines different config hashes silently.",
            },
            "option_snapshot_sample": {
                "passed": int(option_coverage.get("snapshots") or 0) >= 500,
                "value": option_coverage.get("snapshots"),
                "minimum": 500,
            },
            "opportunity_outcome_sample": {
                "passed": int(opportunity_overall.get("trades") or 0) >= 50,
                "value": opportunity_overall.get("trades"),
                "minimum": 50,
            },
            "opportunity_expectancy": {
                "passed": float(opportunity_overall.get("expectancy") or 0.0) > 0,
                "value": opportunity_overall.get("expectancy"),
                "minimum": 0,
            },
            "trade_execution_sample": {
                "passed": int(execution_overall.get("trades") or 0) >= 20,
                "value": execution_overall.get("trades"),
                "minimum": 20,
            },
            "execution_deviation": {
                "passed": float(execution_quality.get("max_entry_deviation_pct") or 0.0) <= settings.max_entry_price_deviation_pct,
                "value": execution_quality.get("max_entry_deviation_pct"),
                "maximum": settings.max_entry_price_deviation_pct,
            },
            "option_backtest_expectancy": {
                "passed": option_backtest.get("status") == "ok" and float(backtest_summary.get("expectancy_pct") or 0.0) > 0,
                "value": backtest_summary.get("expectancy_pct"),
                "minimum": 0,
                "status": option_backtest.get("status"),
            },
            "walk_forward_passed": {
                "passed": bool(walk_forward.get("passed"))
                and int(walk_forward.get("fold_count") or 0) >= settings.readiness_min_validation_folds
                and int(walk_summary.get("trades") or 0) >= settings.readiness_min_oos_trades
                and int(walk_forward.get("out_of_sample_sessions") or 0) >= settings.readiness_min_oos_sessions,
                "value": walk_summary.get("expectancy_pct"),
                "status": walk_forward.get("status"),
                "reasons": walk_forward.get("reasons", []),
                "fold_count": walk_forward.get("fold_count"),
                "minimum_folds": settings.readiness_min_validation_folds,
                "out_of_sample_trades": walk_summary.get("trades"),
                "minimum_out_of_sample_trades": settings.readiness_min_oos_trades,
                "out_of_sample_sessions": walk_forward.get("out_of_sample_sessions"),
                "minimum_out_of_sample_sessions": settings.readiness_min_oos_sessions,
            },
        }

    def _verdict(self, checks: dict[str, Any]) -> dict[str, Any]:
        passed = [name for name, check in checks.items() if bool(check.get("passed"))]
        failed = [name for name, check in checks.items() if not bool(check.get("passed"))]
        if len(failed) == 0:
            label = "ready_for_cautious_live_scaling"
        elif len(passed) >= max(1, len(checks) - 2):
            label = "paper_trade_more_before_live_scaling"
        else:
            label = "not_professionally_validated_yet"
        return {
            "label": label,
            "passed_checks": passed,
            "failed_checks": failed,
            "message": "Professional readiness is evidence-based; failed checks mean collect more data or improve expectancy before increasing live risk.",
        }
