from __future__ import annotations

from datetime import datetime, time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.backtest_service import BacktestService
from app.services.professional_insights_service import ProfessionalInsightsService
from app.services.time_utils import format_ist, to_ist_naive


class AfterMarketResearchService:
    """Run evidence checks after market close, outside the live scanner path."""

    def __init__(
        self,
        *,
        backtest_service: BacktestService,
        professional_insights_service: ProfessionalInsightsService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.backtest_service = backtest_service
        self.professional_insights_service = professional_insights_service
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")))
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
        try:
            result = self._build_report(now=now, trigger=trigger, force=force)
            self.last_result = result
            self.last_run_date = now.date().isoformat()
            self.run_count += 1
            if trigger == "manual":
                self.manual_run_count += 1
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

        daily_summary = self._safe_report(
            lambda: self.professional_insights_service.daily_banknifty_summary(summary_date=now.date())
        )
        data_completeness = self._safe_report(lambda: self.professional_insights_service.data_completeness(symbol=symbol))
        option_backtest = self._safe_report(
            lambda: self.backtest_service.run_option_premium(
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                horizon_candles=horizon,
                limit=limit,
                decision_mode=decision_mode,
            )
        )
        walk_forward = self._safe_report(
            lambda: self.backtest_service.run_walk_forward(
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                horizon_candles=horizon,
                limit=limit,
                decision_mode=decision_mode,
            )
        )
        reports = {
            "daily_summary": daily_summary,
            "data_completeness": data_completeness,
            "option_backtest": self._compact_research_result(option_backtest),
            "walk_forward": self._compact_research_result(walk_forward),
        }
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
            "recommendation": self._recommendation(daily_summary=daily_summary, walk_forward=walk_forward),
            "notes": [
                "This job runs after market close and does not affect live scanner decisions.",
                "Backtest and walk-forward use scanner_parity by default so evidence is aligned with live scanner logic.",
            ],
        }

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
        if self._market_session(now) != "AFTER_MARKET":
            return False, "not_after_market"
        if now.time() < self._parse_time(settings.after_market_research_time):
            return False, "waiting_for_after_market_research_time"
        if self.last_run_date == now.date().isoformat():
            return False, "already_ran_today"
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
        if now.weekday() >= 5:
            return "WEEKEND"
        start = self._parse_time(settings.market_open_time)
        end = self._parse_time(settings.market_close_time)
        if start <= now.time() <= end:
            return "REGULAR_MARKET"
        return "PRE_MARKET" if now.time() < start else "AFTER_MARKET"

    def _now(self) -> datetime:
        return to_ist_naive(self.clock())

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
