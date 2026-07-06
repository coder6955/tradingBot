from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.auto_trader_service import AutoTraderService
from app.services.data_ingestion_service import DataIngestionService
from app.services.notification_service import NotificationService
from app.services.opportunity_outcome_service import OpportunityOutcomeService
from app.services.option_snapshot_collector_service import OptionSnapshotCollectorService
from app.services.risk_management_service import RiskManagementService


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
    ) -> None:
        self.data_ingestion_service = data_ingestion_service
        self.snapshot_collector_service = snapshot_collector_service
        self.auto_trader_service = auto_trader_service
        self.outcome_service = outcome_service
        self.risk_management_service = risk_management_service
        self.notification_service = notification_service or NotificationService()
        self.after_market_research_service = after_market_research_service
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.config: dict[str, Any] = {}
        self.last_cycle_at: str | None = None
        self.last_bootstrap_date: str | None = None
        self.last_intraday_candle_sync_at: datetime | None = None
        self.last_market_closed_evaluation_date: str | None = None
        self.last_intraday_candle_sync_result: dict[str, Any] = {}
        self.last_bootstrap_result: dict[str, Any] = {}
        self.last_actions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

    def start(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.running:
            return self.status()
        self.config = self._normalize_config(config or {})
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.notification_service.send("Automation supervisor started")
        return self.status()

    async def stop(self) -> dict[str, Any]:
        self.running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await self._stop_intraday_services()
        self.notification_service.send("Automation supervisor stopped")
        return self.status()

    def status(self) -> dict[str, Any]:
        market_open = self._market_is_open()
        return {
            "running": self.running,
            "market_open": market_open,
            "config": self.config or self._normalize_config({}),
            "last_cycle_at": self.last_cycle_at,
            "last_bootstrap_date": self.last_bootstrap_date,
            "last_intraday_candle_sync_at": self._format_dt(self.last_intraday_candle_sync_at) if self.last_intraday_candle_sync_at else None,
            "last_intraday_candle_sync_result": self.last_intraday_candle_sync_result,
            "last_bootstrap_result": self.last_bootstrap_result,
            "last_actions": self.last_actions[-20:],
            "error_count": len(self.errors),
            "recent_errors": self.errors[-5:],
            "services": {
                "collector": self.snapshot_collector_service.status(),
                "auto_trader": self.auto_trader_service.status(),
                "outcome_monitor": self.outcome_service.status(),
                "risk": self.risk_management_service.evaluate_entry(),
                "after_market_research": self.after_market_research_service.status() if self.after_market_research_service else {"enabled": False},
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
                actions.extend(self._ensure_intraday_services())
            else:
                actions.extend(self._stop_intraday_services_now())
                self._evaluate_open_once_if_due(now, actions)
                self._run_after_market_research_if_due(now, actions)
                actions.append({"action": "market_closed", "status": "ok", "message": "intraday Kite scanning services are stopped outside market hours"})
            self.last_actions.extend(actions)
            return {"status": "ok", "market_open": self._market_is_open(now), "actions": actions, "automation": self.status()}
        except Exception as exc:
            error = {"time": self._format_dt(now), "error": str(exc)}
            self.errors.append(error)
            return {"status": "error", "error": error, "automation": self.status()}

    async def _run(self) -> None:
        while self.running:
            self.run_once()
            await asyncio.sleep(30)

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

    def _sync_intraday_candles_if_due(self, now: datetime, actions: list[dict[str, Any]]) -> None:
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
        actions.append({"action": "intraday_candle_checkpoint_sync", "status": "ok", "result": result})

    def _ensure_intraday_services(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        symbols = self._symbols()
        if not self.snapshot_collector_service.running:
            collector = self.snapshot_collector_service.start(
                symbols=symbols,
                interval_seconds=int(self.config["snapshot_interval_seconds"]),
                strike_window_pct=float(self.config["strike_window_pct"]),
                max_contracts_per_symbol=int(self.config["max_contracts_per_symbol"]),
            )
            actions.append({"action": "start_snapshot_collector", "status": "ok", "collector": collector})

        if not self.outcome_service.running:
            monitor = self.outcome_service.start(interval_seconds=int(self.config["outcome_interval_seconds"]))
            actions.append({"action": "start_outcome_monitor", "status": "ok", "monitor": monitor})

        if not self.auto_trader_service.running:
            trader = self.auto_trader_service.start(
                side=str(self.config["side"]),
                symbols=symbols,
                interval_seconds=int(self.config["scan_interval_seconds"]),
                limit=int(self.config["scan_limit"]),
                place_orders=bool(self.config["place_orders"]),
                confirm_live=bool(self.config["confirm_live"]),
                order_mode=str(self.config["order_mode"]),
            )
            actions.append({"action": "start_auto_trader", "status": "ok", "auto_trader": trader})
        return actions or [{"action": "intraday_services", "status": "already_running"}]

    def _evaluate_open_once(self, actions: list[dict[str, Any]]) -> None:
        try:
            result = self.outcome_service.evaluate_once(limit=100)
            actions.append({"action": "evaluate_open_opportunities", "status": "ok", "result": result})
        except Exception as exc:
            actions.append({"action": "evaluate_open_opportunities", "status": "error", "message": str(exc)})

    def _evaluate_open_once_if_due(self, now: datetime, actions: list[dict[str, Any]]) -> None:
        market_close = self._parse_time(settings.market_close_time)
        if now.weekday() >= 5 or now.time() <= market_close:
            actions.append({"action": "evaluate_open_opportunities", "status": "skipped", "reason": "market_not_closed_for_day"})
            return
        today = now.date().isoformat()
        if self.last_market_closed_evaluation_date == today:
            actions.append({"action": "evaluate_open_opportunities", "status": "skipped", "reason": "already_checked_after_market_close"})
            return
        self._evaluate_open_once(actions)
        self.last_market_closed_evaluation_date = today

    def _run_after_market_research_if_due(self, now: datetime, actions: list[dict[str, Any]]) -> None:
        if self.after_market_research_service is None:
            return
        try:
            result = self.after_market_research_service.maybe_run_after_market(now)
            if result.get("status") != "idle":
                actions.append(result)
        except Exception as exc:
            actions.append({"action": "after_market_research", "status": "error", "message": str(exc)})

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
            actions.append({"action": action, "status": "ok", "reason": "market_closed", "service": service.status()})
        return actions

    def _should_bootstrap_today(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        return self.last_bootstrap_date != now.date().isoformat()

    def _market_is_open(self, now: datetime | None = None) -> bool:
        now = now or self._now()
        if now.weekday() >= 5:
            return False
        start = self._parse_time(settings.market_open_time)
        end = self._parse_time(settings.market_close_time)
        return start <= now.time() <= end

    def _normalize_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        order_mode = str(payload.get("order_mode") or settings.default_order_mode or "paper").lower()
        if order_mode not in {"paper", "live"}:
            order_mode = "paper"
        place_orders = bool(payload.get("place_orders", settings.automation_place_orders))
        if "place_orders" not in payload:
            place_orders = True
        confirm_live = bool(payload.get("confirm_live", order_mode == "live"))
        return {
            "symbols": payload.get("symbols") or settings.automation_symbols,
            "order_mode": order_mode,
            "side": str(payload.get("side") or settings.automation_side).upper(),
            "scan_interval_seconds": int(payload.get("scan_interval_seconds") or settings.automation_scan_interval_seconds),
            "snapshot_interval_seconds": int(payload.get("snapshot_interval_seconds") or settings.automation_snapshot_interval_seconds),
            "outcome_interval_seconds": int(payload.get("outcome_interval_seconds") or settings.automation_outcome_interval_seconds),
            "ingest_days": int(payload.get("ingest_days") or settings.automation_ingest_days),
            "checkpoint_overlap_minutes": int(payload.get("checkpoint_overlap_minutes") or settings.automation_checkpoint_overlap_minutes),
            "intraday_candle_sync": bool(payload.get("intraday_candle_sync", settings.automation_intraday_candle_sync)),
            "intraday_candle_sync_minutes": int(payload.get("intraday_candle_sync_minutes") or settings.automation_intraday_candle_sync_minutes),
            "place_orders": place_orders,
            "confirm_live": confirm_live if order_mode == "live" else False,
            "scan_limit": int(payload.get("scan_limit") or settings.automation_scan_limit),
            "strike_window_pct": float(payload.get("strike_window_pct") or settings.automation_strike_window_pct),
            "max_contracts_per_symbol": int(payload.get("max_contracts_per_symbol") or settings.automation_max_contracts_per_symbol),
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
