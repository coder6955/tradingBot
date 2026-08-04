from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.auto_trader_service import AutoTraderService
from app.services.data_ingestion_service import DataIngestionService
from app.services.market_session_service import MarketSessionService
from app.services.notification_service import NotificationService
from app.services.opportunity_outcome_service import OpportunityOutcomeService
from app.services.option_snapshot_collector_service import (
    OptionSnapshotCollectorService,
)
from app.services.risk_management_service import RiskManagementService
from app.services.runtime_job_repository import RuntimeJobRepository


class AutomationSupervisorService:
    """Coordinate daily data ingestion, snapshot collection, scanning, and outcome monitoring."""

    def __init__(
        self,
        *,
        data_ingestion_service: DataIngestionService,
        snapshot_collector_service: OptionSnapshotCollectorService,
        auto_trader_service: AutoTraderService,
        outcome_service: OpportunityOutcomeService,
        risk_management_service: RiskManagementService,
        notification_service: NotificationService | None = None,
        after_market_research_service: Any | None = None,
        market_session_service: MarketSessionService | None = None,
        lifecycle_repository: RuntimeJobRepository | None = None,
    ) -> None:
        self.data_ingestion_service = data_ingestion_service
        self.snapshot_collector_service = snapshot_collector_service
        self.auto_trader_service = auto_trader_service
        self.outcome_service = outcome_service
        self.risk_management_service = risk_management_service
        self.notification_service = notification_service or NotificationService()
        self.after_market_research_service = after_market_research_service
        self.market_session_service = market_session_service or MarketSessionService(
            clock=self._now
        )
        self.lifecycle_repository = lifecycle_repository or RuntimeJobRepository()
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.config: dict[str, Any] = {}
        self.last_cycle_at: str | None = None
        self.last_bootstrap_date: str | None = None
        self.last_intraday_candle_sync_at: datetime | None = None
        self.last_live_option_candle_catchup_at: datetime | None = None
        self.last_market_closed_evaluation_date: str | None = None
        self.last_intraday_candle_sync_result: dict[str, Any] = {}
        self.last_live_option_candle_catchup_result: dict[str, Any] = {}
        self.last_bootstrap_result: dict[str, Any] = {}
        self.last_actions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.duplicate_start_prevented_count = 0
        self.boot_id = uuid.uuid4().hex
        self.process_id = os.getpid()
        self.started_at: datetime | None = None
        self.stopped_at: datetime | None = None
        self.last_stop_reason: str | None = None
        self.last_start_trigger: str | None = None
        self.lifecycle_run: dict[str, Any] | None = None
        self.recovered_unclean_run_count = 0
        self.supervisor_restart_count = 0
        self.service_recovery_count = 0

    def start(
        self, config: dict[str, Any] | None = None, *, trigger: str = "api"
    ) -> dict[str, Any]:
        if self._task_is_healthy():
            self.duplicate_start_prevented_count += 1
            return self.status()
        if self.running:
            self.supervisor_restart_count += 1
            self.running = False
            self._record_action(
                {
                    "action": "recover_stale_supervisor_task",
                    "status": "ok",
                    "reason": "running_flag_without_live_task",
                }
            )
        self.config = self._normalize_config(config or {})
        self.running = True
        self.started_at = self._now()
        self.stopped_at = None
        self.last_stop_reason = None
        self.last_start_trigger = str(trigger or "api")
        self._start_lifecycle_run(trigger=self.last_start_trigger)
        self.task = asyncio.create_task(self._run())
        if not settings.scheduled_run_exit_after_complete:
            self.notification_service.send("Automation supervisor started")
        return self.status()

    async def stop(self, *, reason: str = "requested") -> dict[str, Any]:
        self.running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await self._stop_intraday_services()
        self.stopped_at = self._now()
        self.last_stop_reason = str(reason or "requested")
        self._finish_lifecycle_run(status="stopped", reason=self.last_stop_reason)
        if not settings.scheduled_run_exit_after_complete:
            self.notification_service.send("Automation supervisor stopped")
        return self.status()

    def status(self) -> dict[str, Any]:
        now = self._now()
        market_open = self._market_is_open(now)
        task_healthy = self._task_is_healthy()
        try:
            research_status = (
                self.after_market_research_service.status()
                if self.after_market_research_service
                else {"enabled": False}
            )
        except Exception as exc:
            research_status = {
                "status": "error",
                "enabled": True,
                "running": False,
                "last_error": str(exc),
            }
        operator_state = self._operator_state(
            now=now,
            market_open=market_open,
            task_healthy=task_healthy,
            research_status=research_status,
        )
        return {
            "running": self.running,
            "market_open": market_open,
            "runtime_mode": self.market_session_service.current_runtime_mode(now),
            "execution_profile": (
                "scheduled_run"
                if settings.scheduled_run_exit_after_complete
                else "continuous"
            ),
            "completion_policy": {
                "stop_after_after_market_complete": bool(
                    settings.automation_stop_after_after_market_complete
                ),
                "exit_server_after_complete": bool(
                    settings.scheduled_run_exit_after_complete
                ),
            },
            "operator_state": operator_state,
            "duplicate_start_prevented_count": self.duplicate_start_prevented_count,
            "lifecycle": {
                "boot_id": self.boot_id,
                "process_id": self.process_id,
                "task_healthy": task_healthy,
                "started_at": self._format_dt(self.started_at)
                if self.started_at
                else None,
                "stopped_at": self._format_dt(self.stopped_at)
                if self.stopped_at
                else None,
                "last_start_trigger": self.last_start_trigger,
                "last_stop_reason": self.last_stop_reason,
                "current_run": dict(self.lifecycle_run) if self.lifecycle_run else None,
                "recovered_unclean_run_count": self.recovered_unclean_run_count,
                "supervisor_restart_count": self.supervisor_restart_count,
                "service_recovery_count": self.service_recovery_count,
            },
            "config": self.config or self._normalize_config({}),
            "last_cycle_at": self.last_cycle_at,
            "last_bootstrap_date": self.last_bootstrap_date,
            "last_intraday_candle_sync_at": self._format_dt(
                self.last_intraday_candle_sync_at
            )
            if self.last_intraday_candle_sync_at
            else None,
            "last_intraday_candle_sync_result": self.last_intraday_candle_sync_result,
            "last_live_option_candle_catchup_at": self._format_dt(
                self.last_live_option_candle_catchup_at
            )
            if self.last_live_option_candle_catchup_at
            else None,
            "last_live_option_candle_catchup_result": self.last_live_option_candle_catchup_result,
            "last_bootstrap_result": self.last_bootstrap_result,
            "last_actions": self.last_actions[-20:],
            "error_count": len(self.errors),
            "recent_errors": self.errors[-5:],
            "services": {
                "collector": self.snapshot_collector_service.status(),
                "auto_trader": self.auto_trader_service.status(),
                "outcome_monitor": self.outcome_service.status(),
                "risk": {
                    "status": "deferred",
                    "reason": "use /risk/status for explicit risk evaluation",
                },
                "after_market_research": research_status,
            },
        }

    def run_once(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if config:
            self.config = self._normalize_config(config)
        elif not self.config:
            self.config = self._normalize_config({})
        actions: list[dict[str, Any]] = []
        now = self._now()
        self.last_cycle_at = self._format_dt(now)
        try:
            if self._should_bootstrap_today(now):
                actions.append(self._bootstrap_daily_data())
            if self._market_is_open(now):
                self._sync_intraday_candles_if_due(now, actions)
                self._live_option_candle_catchup_if_due(now, actions)
                actions.extend(self._ensure_intraday_services())
            else:
                actions.extend(self._stop_intraday_services_now())
                self._evaluate_open_once_if_due(now, actions)
                self._run_after_market_research_if_due(now, actions)
                actions.append(
                    {
                        "action": "market_closed",
                        "status": "ok",
                        "message": "intraday Kite scanning services are stopped outside market hours",
                    }
                )
                if self._should_stop_after_after_market_complete(now, actions):
                    self.running = False
                    self.stopped_at = now
                    self.last_stop_reason = "after_market_pipeline_completed"
                    self._finish_lifecycle_run(
                        status="completed", reason=self.last_stop_reason
                    )
                    actions.append(
                        {
                            "action": "automation_stop_after_after_market_complete",
                            "status": "ok",
                            "reason": "after_market_pipeline_completed",
                        }
                    )
            self._capture_action_errors(now, actions)
            self.last_actions.extend(actions)
            return {
                "status": "ok",
                "market_open": self._market_is_open(now),
                "actions": actions,
                "automation": self.status(),
            }
        except Exception as exc:
            error = self._append_operational_error(
                now=now,
                source="automation_supervisor_cycle",
                message=str(exc),
                error_type=type(exc).__name__,
            )
            return {"status": "error", "error": error, "automation": self.status()}

    async def _run(self) -> None:
        try:
            while self.running:
                try:
                    if self._market_is_open(self._now()):
                        self.run_once()
                    else:
                        await asyncio.to_thread(self.run_once)
                except Exception as exc:
                    self._append_operational_error(
                        now=self._now(),
                        source="automation_supervisor_loop",
                        message=str(exc),
                        error_type=type(exc).__name__,
                    )
                if self.running:
                    await asyncio.sleep(30)
        finally:
            if self.running:
                self.running = False
                self.stopped_at = self._now()
                self.last_stop_reason = "supervisor_task_terminated_unexpectedly"
                self._finish_lifecycle_run(
                    status="failed", reason=self.last_stop_reason
                )

    def _bootstrap_daily_data(self) -> dict[str, Any]:
        symbols = self._symbols()
        result = self.data_ingestion_service.ingest_candles(
            symbols=symbols,
            timeframe="5minute",
            days=int(self.config["ingest_days"]),
            use_checkpoint=True,
            overlap_minutes=int(self.config["checkpoint_overlap_minutes"]),
        )
        self.last_bootstrap_date = self._now().date().isoformat()
        self.last_bootstrap_result = result
        return {"action": "daily_candle_ingestion", "status": "ok", "result": result}

    def _sync_intraday_candles_if_due(
        self, now: datetime, actions: list[dict[str, Any]]
    ) -> None:
        if not bool(self.config["intraday_candle_sync"]):
            return
        interval_minutes = max(1, int(self.config["intraday_candle_sync_minutes"]))
        if self.last_intraday_candle_sync_at is not None:
            elapsed = (now - self.last_intraday_candle_sync_at).total_seconds()
            if elapsed < interval_minutes * 60:
                return
        result = self.data_ingestion_service.ingest_candles(
            symbols=self._symbols(),
            timeframe="5minute",
            days=1,
            use_checkpoint=True,
            overlap_minutes=int(self.config["checkpoint_overlap_minutes"]),
        )
        self.last_intraday_candle_sync_at = now
        self.last_intraday_candle_sync_result = result
        actions.append(
            {
                "action": "intraday_candle_checkpoint_sync",
                "status": "ok",
                "result": result,
            }
        )

    def _live_option_candle_catchup_if_due(
        self, now: datetime, actions: list[dict[str, Any]]
    ) -> None:
        if not settings.enable_live_option_candle_gap_backfill:
            return
        interval_seconds = max(
            1, int(settings.live_option_candle_backfill_interval_seconds)
        )
        if self.last_live_option_candle_catchup_at is not None:
            elapsed = (now - self.last_live_option_candle_catchup_at).total_seconds()
            if elapsed < interval_seconds:
                return
        timeframes = [
            item.strip()
            for item in str(
                settings.live_option_candle_backfill_timeframes or "1minute"
            ).split(",")
            if item.strip()
        ]
        result = self.data_ingestion_service.backfill_live_relevant_option_candle_gaps(
            symbols=self._symbols(),
            now=now,
            timeframes=timeframes,
            max_contracts=settings.live_option_candle_backfill_max_contracts,
            lookback_minutes=settings.live_option_candle_backfill_lookback_minutes,
            batch_limit=settings.live_option_candle_backfill_batch_limit,
            delay_seconds=settings.live_option_candle_backfill_delay_seconds,
        )
        self.last_live_option_candle_catchup_at = now
        self.last_live_option_candle_catchup_result = result
        actions.append(
            {
                "action": "live_option_candle_gap_catchup",
                "status": result.get("status", "ok"),
                "result": result,
            }
        )

    def _ensure_intraday_services(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        symbols = self._symbols()
        if not self._service_is_healthy(self.snapshot_collector_service):
            self._prepare_service_recovery(
                self.snapshot_collector_service, "snapshot_collector"
            )
            collector = self.snapshot_collector_service.start(
                symbols=symbols,
                interval_seconds=int(self.config["snapshot_interval_seconds"]),
                strike_window_pct=float(self.config["strike_window_pct"]),
                max_contracts_per_symbol=int(self.config["max_contracts_per_symbol"]),
            )
            actions.append(
                {
                    "action": "start_snapshot_collector",
                    "status": "ok",
                    "collector": collector,
                }
            )

        if not self._service_is_healthy(self.outcome_service):
            self._prepare_service_recovery(self.outcome_service, "outcome_monitor")
            monitor = self.outcome_service.start(
                interval_seconds=int(self.config["outcome_interval_seconds"])
            )
            actions.append(
                {"action": "start_outcome_monitor", "status": "ok", "monitor": monitor}
            )

        if not self._service_is_healthy(self.auto_trader_service):
            self._prepare_service_recovery(self.auto_trader_service, "auto_trader")
            trader = self.auto_trader_service.start(
                side=str(self.config["side"]),
                symbols=symbols,
                interval_seconds=int(self.config["scan_interval_seconds"]),
                limit=int(self.config["scan_limit"]),
                place_orders=bool(self.config["place_orders"]),
                confirm_live=bool(self.config["confirm_live"]),
                order_mode=str(self.config["order_mode"]),
            )
            actions.append(
                {"action": "start_auto_trader", "status": "ok", "auto_trader": trader}
            )
        return actions or [{"action": "intraday_services", "status": "already_running"}]

    def _evaluate_open_once(self, actions: list[dict[str, Any]]) -> None:
        try:
            result = self.outcome_service.evaluate_once(
                limit=int(settings.rejected_outcome_batch_limit),
                exhaust_rejected=bool(
                    settings.automation_exhaust_rejected_outcomes_after_close
                ),
            )
            actions.append(
                {
                    "action": "evaluate_open_opportunities",
                    "status": "ok",
                    "result": result,
                }
            )
        except Exception as exc:
            actions.append(
                {
                    "action": "evaluate_open_opportunities",
                    "status": "error",
                    "message": str(exc),
                }
            )

    def _evaluate_open_once_if_due(
        self, now: datetime, actions: list[dict[str, Any]]
    ) -> None:
        market_close = self._parse_time(settings.runtime_market_close_time)
        if now.weekday() >= 5 or now.time() <= market_close:
            actions.append(
                {
                    "action": "evaluate_open_opportunities",
                    "status": "skipped",
                    "reason": "market_not_closed_for_day",
                }
            )
            return
        today = now.date().isoformat()
        if self.last_market_closed_evaluation_date == today:
            actions.append(
                {
                    "action": "evaluate_open_opportunities",
                    "status": "skipped",
                    "reason": "already_checked_after_market_close",
                }
            )
            return
        self._evaluate_open_once(actions)
        self.last_market_closed_evaluation_date = today

    def _run_after_market_research_if_due(
        self, now: datetime, actions: list[dict[str, Any]]
    ) -> None:
        if self.after_market_research_service is None:
            return
        try:
            result = self.after_market_research_service.maybe_run_after_market(now)
            if (
                result.get("status") != "idle"
                or result.get("reason") == "already_ran_today"
            ):
                actions.append(result)
        except Exception as exc:
            actions.append(
                {
                    "action": "after_market_research",
                    "status": "error",
                    "message": str(exc),
                }
            )

    def _should_stop_after_after_market_complete(
        self, now: datetime, actions: list[dict[str, Any]]
    ) -> bool:
        if not self.running or not bool(
            settings.automation_stop_after_after_market_complete
        ):
            return False
        # A continuously boot-managed supervisor survives overnight. A Windows
        # scheduled run exits after today's durable after-market work completes.
        if bool(settings.automation_enabled) and not bool(
            settings.scheduled_run_exit_after_complete
        ):
            return False
        market_close = self._parse_time(settings.runtime_market_close_time)
        if now.weekday() >= 5 or now.time() <= market_close:
            return False
        if self.last_market_closed_evaluation_date != now.date().isoformat():
            return False
        for action in reversed(actions):
            if action.get("action") != "after_market_research":
                continue
            status = str(action.get("status") or "")
            reason = str(action.get("reason") or "")
            return status in {"ok", "partial"} or reason == "already_ran_today"
        try:
            status = (
                self.after_market_research_service.status()
                if self.after_market_research_service
                else {}
            )
        except Exception:
            return False
        return status.get("next_action") == "research_completed_for_today"

    def _operator_state(
        self,
        *,
        now: datetime,
        market_open: bool,
        task_healthy: bool,
        research_status: dict[str, Any],
    ) -> dict[str, Any]:
        runtime_mode = self.market_session_service.current_runtime_mode(now)
        job_run = (
            research_status.get("job_run")
            if isinstance(research_status.get("job_run"), dict)
            else {}
        )
        completed_today = bool(
            research_status.get("next_action") == "research_completed_for_today"
            or (
                job_run.get("status") == "success"
                and str(job_run.get("trading_date") or "") == now.date().isoformat()
            )
            or self.last_stop_reason == "after_market_pipeline_completed"
        )
        base = {
            "runtime_mode": runtime_mode,
            "day_complete": completed_today,
            "trading_active": False,
            "supervisor_alive": bool(self.running and task_healthy),
            "action_required": False,
        }
        if self.running and not task_healthy:
            return {
                **base,
                "code": "RUNTIME_FAILURE",
                "label": "Runtime failure",
                "severity": "error",
                "summary": "The supervisor flag is set but its task is not healthy.",
                "action_required": True,
            }
        if not self.running:
            if completed_today:
                return {
                    **base,
                    "code": "DAY_COMPLETE",
                    "label": "Day complete",
                    "severity": "ok",
                    "summary": "Trading and after-market research are complete for today.",
                }
            unexpected = self.last_stop_reason not in {None, "requested", "application_shutdown"}
            return {
                **base,
                "code": "STOPPED",
                "label": "Automation stopped",
                "severity": "error" if unexpected else "warning",
                "summary": (
                    f"Supervisor stopped: {self.last_stop_reason}."
                    if self.last_stop_reason
                    else "The automation supervisor is not running."
                ),
                "action_required": unexpected,
            }
        if completed_today:
            return {
                **base,
                "code": "DAY_COMPLETE",
                "label": "Day complete",
                "severity": "ok",
                "summary": (
                    "Research is complete; the continuous supervisor is only waiting "
                    "for the next session."
                    if not settings.scheduled_run_exit_after_complete
                    else "Research is complete; scheduled shutdown is in progress."
                ),
            }
        if market_open:
            return {
                **base,
                "code": "TRADING_ACTIVE",
                "label": "Trading automation active",
                "severity": "ok",
                "summary": "Market-hours scanning, monitoring and risk controls are expected to run.",
                "trading_active": True,
            }
        if research_status.get("running") or research_status.get("queued"):
            return {
                **base,
                "code": "RESEARCH_RUNNING",
                "label": "After-market research running",
                "severity": "working",
                "summary": "Trading is stopped while the research pipeline finishes.",
            }
        if runtime_mode == "PRE_MARKET":
            return {
                **base,
                "code": "PRE_MARKET_READY",
                "label": "Pre-market preparation",
                "severity": "working",
                "summary": "The supervisor is preparing for the market session.",
            }
        return {
            **base,
            "code": "MARKET_CLOSED_IDLE",
            "label": "Live modules idle",
            "severity": "ok",
            "summary": "The market is closed, so trading workers are stopped by design.",
        }

    def _capture_action_errors(
        self, now: datetime, actions: list[dict[str, Any]]
    ) -> None:
        for action in actions:
            if str(action.get("status") or "").lower() != "error":
                continue
            source = str(action.get("action") or "automation_action")
            message = str(
                action.get("message")
                or action.get("error")
                or action.get("reason")
                or "runtime action failed"
            )
            self._append_operational_error(
                now=now,
                source=source,
                message=message,
                error_type=str(action.get("error_type") or "RuntimeActionError"),
            )

    def _append_operational_error(
        self,
        *,
        now: datetime,
        source: str,
        message: str,
        error_type: str,
    ) -> dict[str, Any]:
        fingerprint = f"{source}:{error_type}:{message}"
        if self.errors and self.errors[-1].get("fingerprint") == fingerprint:
            self.errors[-1]["last_seen_at"] = self._format_dt(now)
            self.errors[-1]["occurrences"] = int(
                self.errors[-1].get("occurrences") or 1
            ) + 1
            return self.errors[-1]
        error = {
            "time": self._format_dt(now),
            "last_seen_at": self._format_dt(now),
            "error": message,
            "error_type": error_type,
            "source": source,
            "fingerprint": fingerprint,
            "occurrences": 1,
        }
        self.errors.append(error)
        return error

    async def _stop_intraday_services(self) -> None:
        if self.auto_trader_service.running:
            await self.auto_trader_service.stop()
        if self.snapshot_collector_service.running:
            await self.snapshot_collector_service.stop()
        if self.outcome_service.running:
            await self.outcome_service.stop()

    def _stop_intraday_services_now(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for action, service in (
            ("stop_auto_trader", self.auto_trader_service),
            ("stop_snapshot_collector", self.snapshot_collector_service),
            ("stop_outcome_monitor", self.outcome_service),
        ):
            if not getattr(service, "running", False):
                continue
            setattr(service, "running", False)
            task = getattr(service, "task", None)
            if task is not None:
                task.cancel()
            actions.append(
                {
                    "action": action,
                    "status": "ok",
                    "reason": "market_closed",
                    "service": service.status(),
                }
            )
        return actions

    def _should_bootstrap_today(self, now: datetime) -> bool:
        if not self.market_session_service.is_market_day(now):
            return False
        if self.last_bootstrap_date == now.date().isoformat():
            return False
        return self.market_session_service.current_runtime_mode(now) in {
            "PRE_MARKET",
            "AFTER_MARKET_REVIEW",
        }

    def _market_is_open(self, now: datetime | None = None) -> bool:
        return self.market_session_service.should_run_live_modules(now or self._now())

    def _normalize_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_mode = str(
            payload.get("order_mode") or settings.default_order_mode or "paper"
        ).lower()
        if order_mode not in {"paper", "live"}:
            order_mode = "paper"
        place_orders = bool(
            payload.get("place_orders", settings.automation_place_orders)
        )
        if "place_orders" not in payload:
            place_orders = True
        confirm_live = bool(payload.get("confirm_live", order_mode == "live"))
        return {
            "symbols": payload.get("symbols") or settings.automation_symbols,
            "order_mode": order_mode,
            "side": str(payload.get("side") or settings.automation_side).upper(),
            "scan_interval_seconds": int(
                payload.get("scan_interval_seconds")
                or settings.automation_scan_interval_seconds
            ),
            "snapshot_interval_seconds": int(
                payload.get("snapshot_interval_seconds")
                or settings.automation_snapshot_interval_seconds
            ),
            "outcome_interval_seconds": int(
                payload.get("outcome_interval_seconds")
                or settings.automation_outcome_interval_seconds
            ),
            "ingest_days": int(
                payload.get("ingest_days") or settings.automation_ingest_days
            ),
            "checkpoint_overlap_minutes": int(
                payload.get("checkpoint_overlap_minutes")
                or settings.automation_checkpoint_overlap_minutes
            ),
            "intraday_candle_sync": bool(
                payload.get(
                    "intraday_candle_sync", settings.automation_intraday_candle_sync
                )
            ),
            "intraday_candle_sync_minutes": int(
                payload.get("intraday_candle_sync_minutes")
                or settings.automation_intraday_candle_sync_minutes
            ),
            "place_orders": place_orders,
            "confirm_live": confirm_live if order_mode == "live" else False,
            "scan_limit": int(
                payload.get("scan_limit") or settings.automation_scan_limit
            ),
            "strike_window_pct": float(
                payload.get("strike_window_pct")
                or settings.automation_strike_window_pct
            ),
            "max_contracts_per_symbol": int(
                payload.get("max_contracts_per_symbol")
                or settings.automation_max_contracts_per_symbol
            ),
        }

    def _symbols(self) -> list[str]:
        symbols = self.config.get("symbols") or settings.automation_symbols
        if isinstance(symbols, str):
            return [item.strip().upper() for item in symbols.split(",") if item.strip()]
        return [str(item).strip().upper() for item in symbols if str(item).strip()]

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo("Asia/Kolkata"))

    def _format_dt(self, value: datetime) -> str:
        return value.strftime("%d %b %Y, %I:%M:%S %p IST")

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))

    def _task_is_healthy(self) -> bool:
        return bool(self.running and self.task is not None and not self.task.done())

    def _service_is_healthy(self, service: Any) -> bool:
        if not bool(getattr(service, "running", False)):
            return False
        task = getattr(service, "task", None)
        return task is not None and not task.done()

    def _prepare_service_recovery(self, service: Any, service_name: str) -> None:
        was_running = bool(getattr(service, "running", False))
        task = getattr(service, "task", None)
        if not was_running and task is None:
            return
        if task is not None and not task.done():
            return
        setattr(service, "running", False)
        setattr(service, "task", None)
        self.service_recovery_count += 1
        self._record_action(
            {
                "action": "recover_intraday_service",
                "status": "ok",
                "service": service_name,
                "reason": "stale_running_state" if was_running else "completed_task",
            }
        )

    def _record_action(self, action: dict[str, Any]) -> None:
        self.last_actions.append(dict(action))
        self.last_actions = self.last_actions[-100:]

    def _start_lifecycle_run(self, *, trigger: str) -> None:
        trading_date = self._now().date().isoformat()
        try:
            latest_run = getattr(self.lifecycle_repository, "latest_run", None)
            previous = (
                latest_run(job_name="automation_supervisor")
                if callable(latest_run)
                else self.lifecycle_repository.latest(
                    job_name="automation_supervisor", trading_date=trading_date
                )
            )
            if previous and previous.get("status") == "running" and previous.get("id"):
                previous_metadata = dict(previous.get("metadata") or {})
                previous_metadata.update(
                    {
                        "interrupted_detected_by_boot_id": self.boot_id,
                        "interrupted_detected_by_process_id": self.process_id,
                    }
                )
                self.lifecycle_repository.finish(
                    run_id=int(previous["id"]),
                    status="interrupted",
                    metadata=previous_metadata,
                    error_message="previous application process ended without a recorded supervisor stop",
                )
                self.recovered_unclean_run_count += 1
            self.lifecycle_run = self.lifecycle_repository.start(
                job_name="automation_supervisor",
                trading_date=trading_date,
                metadata={
                    "boot_id": self.boot_id,
                    "process_id": self.process_id,
                    "trigger": trigger,
                    "order_mode": self.config.get("order_mode"),
                    "symbols": self._symbols(),
                },
            )
        except Exception as exc:
            self.errors.append(
                {
                    "time": self._format_dt(self._now()),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "source": "automation_lifecycle_repository",
                }
            )

    def _finish_lifecycle_run(self, *, status: str, reason: str) -> None:
        run = self.lifecycle_run
        if not run or not run.get("id"):
            return
        metadata = dict(run.get("metadata") or {})
        metadata.update(
            {
                "boot_id": self.boot_id,
                "process_id": self.process_id,
                "stop_reason": reason,
                "last_cycle_at": self.last_cycle_at,
                "service_recovery_count": self.service_recovery_count,
            }
        )
        try:
            finished = self.lifecycle_repository.finish(
                run_id=int(run["id"]),
                status=status,
                metadata=metadata,
                error_message=reason if status in {"failed", "interrupted"} else None,
            )
            if finished:
                self.lifecycle_run = finished
        except Exception as exc:
            self.errors.append(
                {
                    "time": self._format_dt(self._now()),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "source": "automation_lifecycle_repository",
                }
            )
