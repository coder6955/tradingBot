from __future__ import annotations

import time as time_module
from datetime import datetime, time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.backtest_service import BacktestService
from app.services.market_session_service import MarketSessionService
from app.services.professional_insights_service import ProfessionalInsightsService
from app.services.runtime_job_repository import RuntimeJobRepository
from app.services.time_utils import format_ist, to_ist_naive


class AfterMarketResearchService:
    """Run evidence checks after market close, outside the live scanner path."""

    JOB_NAME = "after_market_review"

    def __init__(
        self,
        *,
        backtest_service: BacktestService,
        professional_insights_service: ProfessionalInsightsService,
        data_ingestion_service: Any | None = None,
        outcome_learning_service: Any | None = None,
        rejected_outcome_service: Any | None = None,
        opportunity_analytics_service: Any | None = None,
        execution_analytics_service: Any | None = None,
        professional_readiness_service: Any | None = None,
        strategy_edge_service: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        market_session_service: MarketSessionService | None = None,
        job_repository: RuntimeJobRepository | None = None,
    ) -> None:
        self.backtest_service = backtest_service
        self.professional_insights_service = professional_insights_service
        self.data_ingestion_service = data_ingestion_service
        self.outcome_learning_service = outcome_learning_service
        self.rejected_outcome_service = rejected_outcome_service
        self.opportunity_analytics_service = opportunity_analytics_service
        self.execution_analytics_service = execution_analytics_service
        self.professional_readiness_service = professional_readiness_service
        self.strategy_edge_service = strategy_edge_service
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")))
        self.market_session_service = market_session_service or MarketSessionService(clock=self.clock)
        self.job_repository = job_repository or RuntimeJobRepository()
        self.running = False
        self.last_started_at: datetime | None = None
        self.last_completed_at: datetime | None = None
        self.last_run_date: str | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_skip_reason: str | None = None
        self.run_count = 0
        self.manual_run_count = 0
        self.skipped_count = 0

    def status(self) -> dict[str, Any]:
        now = self._now()
        return {
            "status": "ok",
            "enabled": settings.enable_after_market_research_job,
            "running": self.running,
            "market_session": self._market_session(now),
            "configured_run_time": settings.after_market_research_time,
            "symbol": settings.after_market_research_symbol.upper(),
            "timeframe": settings.after_market_research_timeframe,
            "direction": settings.after_market_research_direction.upper(),
            "decision_mode": settings.after_market_research_decision_mode,
            "step_delay_seconds": settings.after_market_research_step_delay_seconds,
            "pipeline": self._pipeline_names(),
            "last_started_at": format_ist(self.last_started_at),
            "last_completed_at": format_ist(self.last_completed_at),
            "last_run_date": self.last_run_date,
            "last_error": self.last_error,
            "last_skip_reason": self.last_skip_reason,
            "run_count": self.run_count,
            "manual_run_count": self.manual_run_count,
            "skipped_count": self.skipped_count,
            "due": self._can_run(now=now, force=False)[0],
            "next_action": self._next_action(now),
            "last_result": self.last_result,
            "job_run": self.job_repository.latest(job_name=self.JOB_NAME, trading_date=now.date().isoformat()),
        }

    def maybe_run_after_market(self, now: datetime | None = None) -> dict[str, Any]:
        now = to_ist_naive(now or self._now())
        allowed, reason = self._can_run(now=now, force=False)
        if not allowed:
            return {
                "action": "after_market_research",
                "status": "idle",
                "reason": reason,
                "market_session": self._market_session(now),
            }
        return self.run_once(trigger="scheduled", force=False, now=now)

    def run_once(self, *, trigger: str = "manual", force: bool = False, now: datetime | None = None) -> dict[str, Any]:
        now = to_ist_naive(now or self._now())
        allowed, reason = self._can_run(now=now, force=force)
        if not allowed:
            self.skipped_count += 1
            self.last_skip_reason = reason
            return {
                "action": "after_market_research",
                "status": "skipped",
                "trigger": trigger,
                "force": force,
                "reason": reason,
                "market_session": self._market_session(now),
                "configured_run_time": settings.after_market_research_time,
            }
        if self.running:
            self.skipped_count += 1
            self.last_skip_reason = "already_running"
            return {"action": "after_market_research", "status": "skipped", "trigger": trigger, "reason": "already_running"}

        self.running = True
        self.last_started_at = now
        self.last_error = None
        job_run = self.job_repository.start(
            job_name=self.JOB_NAME,
            trading_date=now.date().isoformat(),
            metadata={"trigger": trigger, "force": force},
        )
        try:
            result = self._build_report(now=now, trigger=trigger, force=force)
            self.last_result = result
            self.last_run_date = now.date().isoformat()
            self.run_count += 1
            if trigger == "manual":
                self.manual_run_count += 1
            self.job_repository.finish(
                run_id=int(job_run["id"]),
                status="success" if str(result.get("status")) == "ok" else "failed",
                metadata={"trigger": trigger, "force": force, "result": result},
                error_message=None if str(result.get("status")) == "ok" else str(result.get("status")),
            )
            return result
        except Exception as exc:
            self.last_error = str(exc)
            result = {
                "action": "after_market_research",
                "status": "error",
                "trigger": trigger,
                "force": force,
                "error": str(exc),
                "started_at": format_ist(now),
                "completed_at": format_ist(self._now()),
            }
            self.last_result = result
            self.job_repository.finish(
                run_id=int(job_run["id"]),
                status="failed",
                metadata={"trigger": trigger, "force": force, "result": result},
                error_message=str(exc),
            )
            return result
        finally:
            self.running = False
            self.last_completed_at = self._now()

    def _build_report(self, *, now: datetime, trigger: str, force: bool) -> dict[str, Any]:
        symbol = settings.after_market_research_symbol.upper()
        timeframe = settings.after_market_research_timeframe
        direction = settings.after_market_research_direction.upper()
        horizon = settings.after_market_research_horizon_candles
        limit = settings.after_market_research_limit
        decision_mode = settings.after_market_research_decision_mode

        reports: dict[str, dict[str, Any]] = {}
        if self.data_ingestion_service is not None and settings.enable_targeted_option_candle_backfill:
            self._run_stage(
                reports,
                "targeted_option_candle_backfill",
                lambda: self.data_ingestion_service.backfill_relevant_option_candles(
                    symbols=[symbol],
                    trading_date=now.date(),
                    timeframes=[
                        item.strip()
                        for item in str(settings.targeted_option_candle_backfill_timeframes or "1minute,5minute").split(",")
                        if item.strip()
                    ],
                    max_contracts=settings.targeted_option_candle_backfill_max_contracts,
                    batch_limit=settings.targeted_option_candle_backfill_batch_limit,
                    delay_seconds=settings.targeted_option_candle_backfill_delay_seconds,
                ),
            )
        if self.data_ingestion_service is not None:
            self._run_stage(
                reports,
                "option_candle_coverage",
                lambda: self.data_ingestion_service.option_candle_coverage_report(
                    symbols=[symbol],
                    trading_date=now.date(),
                    timeframes=[
                        item.strip()
                        for item in str(settings.targeted_option_candle_backfill_timeframes or "1minute,5minute").split(",")
                        if item.strip()
                    ],
                    max_contracts=settings.targeted_option_candle_backfill_max_contracts,
                ),
            )
        if self.rejected_outcome_service is not None:
            self._run_stage(
                reports,
                "rejected_outcome_replay",
                lambda: self.rejected_outcome_service.evaluate_batches(
                    symbol=symbol,
                    batch_limit=settings.rejected_outcome_batch_limit,
                    max_batches=settings.rejected_outcome_max_batches,
                    learning_only=True,
                    delay_seconds=settings.rejected_outcome_batch_delay_seconds,
                ),
            )
        self._run_stage(
            reports,
            "daily_summary",
            lambda: self.professional_insights_service.daily_banknifty_summary(summary_date=now.date()),
        )
        self._run_stage(reports, "data_completeness", lambda: self.professional_insights_service.data_completeness(symbol=symbol))
        if self.outcome_learning_service is not None:
            self._run_stage(reports, "outcome_learning", lambda: self.outcome_learning_service.analyze())
        if self.opportunity_analytics_service is not None:
            self._run_stage(reports, "opportunity_analytics", lambda: self.opportunity_analytics_service.analyze(symbol=symbol, limit=limit))
        if self.execution_analytics_service is not None:
            self._run_stage(reports, "execution_analytics", lambda: self.execution_analytics_service.analyze(symbol=symbol, limit=limit))
        self._run_stage(reports, "professional_insights", lambda: self.professional_insights_service.analyze(symbol=symbol, limit=limit))
        self._run_stage(reports, "research_engine", lambda: self.professional_insights_service.research_engine_report(symbol=symbol, limit=limit))
        self._run_stage(reports, "gate_effectiveness", lambda: self.professional_insights_service.gate_effectiveness_report(symbol=symbol, limit=limit))
        self._run_stage(reports, "rejected_opportunity_quality", lambda: self.professional_insights_service.rejected_opportunity_quality_report(symbol=symbol, limit=limit))
        self._run_stage(
            reports,
            "threshold_validation",
            lambda: self.professional_insights_service.threshold_validation_report(symbol=symbol, limit=limit),
        )
        self._run_stage(
            reports,
            "execution_realism",
            lambda: self.professional_insights_service.execution_realism_report(symbol=symbol, limit=limit),
        )
        self._run_stage(
            reports,
            "daily_review",
            lambda: self.professional_insights_service.daily_review(symbol=symbol, review_date=now.date(), limit=limit),
        )
        self._run_stage(
            reports,
            "option_backtest",
            lambda: self.backtest_service.run_option_premium(
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                horizon_candles=horizon,
                limit=limit,
                decision_mode=decision_mode,
            ),
            compact=True,
        )
        self._run_stage(
            reports,
            "ablation",
            lambda: self.backtest_service.run_ablation(
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                horizon_candles=horizon,
                limit=min(limit, 1000),
            ),
            compact=True,
        )
        self._run_stage(
            reports,
            "walk_forward",
            lambda: self.backtest_service.run_walk_forward(
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                horizon_candles=horizon,
                limit=limit,
                decision_mode=decision_mode,
            ),
            compact=True,
        )
        if self.professional_readiness_service is not None:
            self._run_stage(
                reports,
                "professional_readiness",
                lambda: self.professional_readiness_service.report(symbol=symbol, timeframe=timeframe, direction=direction, limit=limit),
                compact=True,
            )
        if self.strategy_edge_service is not None:
            self._run_stage(
                reports,
                "strategy_edge_validation",
                lambda: self.strategy_edge_service.validate(symbol=symbol, timeframe=timeframe, direction=direction, limit=limit),
                compact=True,
            )
        overall_status = "ok" if all(str(report.get("status")) == "ok" for report in reports.values()) else "partial"
        completed_at = self._now()
        return {
            "action": "after_market_research",
            "status": overall_status,
            "trigger": trigger,
            "force": force,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "decision_mode": decision_mode,
            "market_session": self._market_session(now),
            "started_at": format_ist(now),
            "completed_at": format_ist(completed_at),
            "reports": reports,
            "recommendation": self._recommendation(
                daily_summary=reports.get("daily_summary", {}),
                walk_forward=reports.get("walk_forward", {}),
            ),
            "notes": [
                "This staged job runs after market close and does not affect live scanner decisions.",
                "Each report is run sequentially with a configurable delay so the server is not loaded all at once.",
                "Backtest and walk-forward use scanner_parity by default so evidence is aligned with live scanner logic.",
            ],
        }

    def _run_stage(
        self,
        reports: dict[str, dict[str, Any]],
        name: str,
        fn: Callable[[], dict[str, Any]],
        *,
        compact: bool = False,
    ) -> None:
        started_at = self._now()
        result = self._safe_report(fn)
        if compact:
            result = self._compact_research_result(result)
        completed_at = self._now()
        reports[name] = {
            "status": result.get("status", "ok") if "error" not in result else "error",
            "started_at": format_ist(started_at),
            "completed_at": format_ist(completed_at),
            "result": result,
        }
        delay = max(0.0, float(settings.after_market_research_step_delay_seconds))
        if delay > 0:
            time_module.sleep(delay)

    def _pipeline_names(self) -> list[str]:
        names = [
            "targeted_option_candle_backfill",
            "option_candle_coverage",
            "rejected_outcome_replay",
            "daily_summary",
            "data_completeness",
            "professional_insights",
            "research_engine",
            "gate_effectiveness",
            "rejected_opportunity_quality",
            "threshold_validation",
            "execution_realism",
            "daily_review",
            "option_backtest",
            "ablation",
            "walk_forward",
        ]
        if self.rejected_outcome_service is None:
            names.remove("rejected_outcome_replay")
        if self.data_ingestion_service is None:
            names.remove("targeted_option_candle_backfill")
            names.remove("option_candle_coverage")
        elif not settings.enable_targeted_option_candle_backfill:
            names.remove("targeted_option_candle_backfill")
        if self.outcome_learning_service is not None:
            names.insert(2, "outcome_learning")
        if self.opportunity_analytics_service is not None:
            names.insert(3, "opportunity_analytics")
        if self.execution_analytics_service is not None:
            names.insert(4, "execution_analytics")
        if self.professional_readiness_service is not None:
            names.append("professional_readiness")
        if self.strategy_edge_service is not None:
            names.append("strategy_edge_validation")
        return names

    def _safe_report(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return fn()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _compact_research_result(self, result: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "status",
            "mode",
            "decision_mode",
            "decision_engine",
            "symbol",
            "timeframe",
            "direction",
            "underlying_candles",
            "option_contracts",
            "horizon_candles",
            "decisions_scanned",
            "accepted_decisions",
            "rejected_decisions",
            "train_candles",
            "test_candles",
            "passed",
            "reasons",
            "message",
            "summary",
            "thresholds",
        ]
        return {key: result[key] for key in keys if key in result}

    def _recommendation(self, *, daily_summary: dict[str, Any], walk_forward: dict[str, Any]) -> dict[str, Any]:
        closed_paper = int(daily_summary.get("total_closed_paper_trades") or 0)
        walk_passed = bool(walk_forward.get("passed"))
        if closed_paper < 30:
            reason = "Need at least 30 closed paper/live-shadow trades before changing strategy or enabling live."
        elif not walk_passed:
            reason = "Walk-forward evidence is not strong enough yet; keep collecting paper/live-shadow data."
        else:
            reason = "Evidence is improving, but live enablement still needs multi-session review and manual approval."
        return {
            "safe_to_change_strategy": False,
            "safe_to_enable_live": False,
            "reason": reason,
            "next_action": "Review the report, keep paper/live-shadow running, and compare multiple sessions.",
        }

    def _can_run(self, *, now: datetime, force: bool) -> tuple[bool, str | None]:
        if not settings.enable_after_market_research_job:
            return False, "disabled"
        if self.running:
            return False, "already_running"
        if force:
            return True, None
        if now.weekday() >= 5:
            return False, "not_market_day"
        if not self.market_session_service.should_run_after_market_review(now):
            return False, "not_after_market"
        if now.time() < self._parse_time(settings.after_market_research_time):
            return False, "waiting_for_after_market_research_time"
        trading_date = now.date().isoformat()
        job_run = self.job_repository.latest(job_name=self.JOB_NAME, trading_date=trading_date)
        if self.last_run_date == trading_date or self.job_repository.count(job_name=self.JOB_NAME, trading_date=trading_date, status="success") > 0:
            return False, "already_ran_today"
        if job_run and job_run.get("status") == "running":
            return False, "already_running"
        if self.job_repository.count(job_name=self.JOB_NAME, trading_date=trading_date, status="failed") >= 2:
            return False, "failed_retry_exhausted"
        return True, None

    def _next_action(self, now: datetime) -> str:
        allowed, reason = self._can_run(now=now, force=False)
        if allowed:
            return "ready_to_run_after_market_research"
        if reason == "already_ran_today":
            return "research_completed_for_today"
        if reason == "not_market_day":
            return "wait_for_next_market_day"
        if reason == "not_after_market":
            return "wait_until_market_close"
        if reason == "waiting_for_after_market_research_time":
            return f"wait_until_{settings.after_market_research_time}_IST"
        return reason or "idle"

    def _market_session(self, now: datetime) -> str:
        mode = self.market_session_service.current_runtime_mode(now)
        return "AFTER_MARKET" if mode == "AFTER_MARKET_REVIEW" else mode

    def _now(self) -> datetime:
        return to_ist_naive(self.clock())

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
