from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import Signal
from app.services.notification_service import NotificationService
from app.services.opportunity_repository import OpportunityRepository
from app.services.order_service import OrderService
from app.services.risk_management_service import RiskManagementService
from app.services.scanner_service import ScannerService


ScannerFactory = Callable[[], ScannerService]
OrderServiceFactory = Callable[[], OrderService]


class AutoTraderService:
    """Continuously scan for opportunities and optionally route them to orders."""

    def __init__(
        self,
        scanner_factory: ScannerFactory,
        order_service_factory: OrderServiceFactory,
        opportunity_repository: OpportunityRepository | None = None,
        risk_management_service: RiskManagementService | None = None,
        notification_service: NotificationService | None = None,
    ) -> None:
        self.scanner_factory = scanner_factory
        self.order_service_factory = order_service_factory
        self.opportunity_repository = opportunity_repository
        self.risk_management_service = risk_management_service or RiskManagementService()
        self.notification_service = notification_service or NotificationService()
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.config: dict[str, Any] = {}
        self.latest_opportunities: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.seen_order_keys: set[str] = set()
        self.last_scan_at: str | None = None

    def _now_ist(self) -> str:
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M:%S %p IST")

    def start(
        self,
        *,
        side: str = "BUY",
        symbols: list[str] | None = None,
        interval_seconds: int | None = None,
        limit: int = 5,
        place_orders: bool = False,
        confirm_live: bool = False,
        order_mode: str | None = None,
    ) -> dict[str, Any]:
        if self.running:
            return self.status()

        interval = interval_seconds or settings.scanner_interval_seconds
        self.config = {
            "side": side.upper(),
            "symbols": symbols,
            "interval_seconds": max(1, int(interval)),
            "limit": max(1, int(limit)),
            "place_orders": bool(place_orders),
            "confirm_live": bool(confirm_live),
            "order_mode": (order_mode or settings.default_order_mode or "paper").lower(),
        }
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.notification_service.send(f"Auto trader started: side={side.upper()}, symbols={symbols or 'default'}, interval={max(1, int(interval))}s")
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
        return self.status()

    def status(self) -> dict[str, Any]:
        order_mode = str(self.config.get("order_mode") or settings.default_order_mode or "paper").lower()
        return {
            "running": self.running,
            "config": self.config,
            "last_scan_at": self.last_scan_at,
            "latest_count": len(self.latest_opportunities),
            "execution_count": len(self.executions),
            "error_count": len(self.errors),
            "mode": order_mode,
            "live_ordering_requires_confirm_live": True,
            "risk_limits_apply_to_order_mode": order_mode == "live",
            "risk": self.risk_management_service.evaluate_entry(),
        }

    async def _run(self) -> None:
        while self.running:
            try:
                self.scan_once()
            except Exception as exc:
                self.errors.append(
                    {
                        "time": self._now_ist(),
                        "error": str(exc),
                    }
                )
            await asyncio.sleep(float(self.config.get("interval_seconds", settings.scanner_interval_seconds)))

    def scan_once(self) -> dict[str, Any]:
        scanner = self.scanner_factory()
        symbols = self.config.get("symbols")
        opportunities = scanner.scan_symbols(
            symbols=symbols,
            side=str(self.config.get("side", "BUY")),
            order_mode=str(self.config.get("order_mode") or "paper"),
            rejection_source="automation_scan",
        )
        limited = opportunities[: int(self.config.get("limit", 5))]
        self.latest_opportunities = [asdict(signal) for signal in limited]
        saved_ids: list[int] = []
        saved_id_by_order_key: dict[str, int] = {}
        if self.opportunity_repository is not None:
            for signal in limited:
                saved = self.opportunity_repository.save_opportunity(signal)
                saved_ids.append(saved.id)
                saved_id_by_order_key[self._order_key(signal)] = saved.id
        self.last_scan_at = self._now_ist()

        placed: list[dict[str, Any]] = []
        if self.config.get("place_orders"):
            order_mode = str(self.config.get("order_mode") or "paper").lower()
            for signal in limited:
                try:
                    result = self._place_once(signal, opportunity_id=saved_id_by_order_key.get(self._order_key(signal)))
                    if result is not None:
                        placed.append(result)
                except Exception as exc:
                    message = str(exc)
                    if order_mode == "live" and message.startswith("risk guard blocked order:"):
                        try:
                            shadow = self._place_shadow_paper(
                                signal,
                                opportunity_id=saved_id_by_order_key.get(self._order_key(signal)),
                                reason=message,
                            )
                            placed.append(shadow)
                        except Exception as shadow_exc:
                            self.errors.append(
                                {
                                    "time": self._now_ist(),
                                    "error": str(shadow_exc),
                                    "shadow_reason": message,
                                    "order_key": self._order_key(signal),
                                }
                            )
                    else:
                        self.errors.append(
                            {
                                "time": self._now_ist(),
                                "error": message,
                                "order_key": self._order_key(signal),
                            }
                        )

        return {
            "last_scan_at": self.last_scan_at,
            "count": len(self.latest_opportunities),
            "saved_ids": saved_ids,
            "opportunities": self.latest_opportunities,
            "placed": placed,
        }

    def _order_key(self, signal: Signal) -> str:
        return "|".join(
            [
                str(signal.tradingsymbol or signal.symbol),
                signal.side.upper(),
                signal.action,
                str(signal.expiry or ""),
                str(signal.strike or ""),
            ]
        )

    def _place_once(self, signal: Signal, opportunity_id: int | None = None) -> dict[str, Any] | None:
        order_key = self._order_key(signal)
        order_mode = str(self.config.get("order_mode") or "paper").lower()
        if order_mode == "live" and order_key in self.seen_order_keys:
            return None

        if order_mode == "live":
            self.seen_order_keys.add(order_key)
        service = self.order_service_factory()
        result = service.place_signal_order(
            signal,
            confirm_live=bool(self.config.get("confirm_live", False)),
            opportunity_id=opportunity_id,
            order_mode=str(self.config.get("order_mode") or "paper"),
        )
        execution = {
            "time": self._now_ist(),
            "order_key": order_key,
            "signal": asdict(signal),
            "result": result,
        }
        self.executions.append(execution)
        self.notification_service.send(f"Auto trader placed {result.get('status')} order for {signal.tradingsymbol or signal.symbol}")
        return execution

    def _place_shadow_paper(self, signal: Signal, opportunity_id: int | None = None, reason: str = "") -> dict[str, Any]:
        service = self.order_service_factory()
        result = service.place_signal_order(
            signal,
            confirm_live=False,
            opportunity_id=opportunity_id,
            order_mode="paper",
        )
        result = {**result, "shadow_for_live": True, "shadow_reason": reason}
        execution = {
            "time": self._now_ist(),
            "order_key": self._order_key(signal),
            "signal": asdict(signal),
            "result": result,
        }
        self.executions.append(execution)
        self.notification_service.send(f"Auto trader recorded paper shadow for {signal.tradingsymbol or signal.symbol}")
        return execution
