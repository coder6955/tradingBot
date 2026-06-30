from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable

from app.config import settings
from app.models import Signal
from app.services.opportunity_repository import OpportunityRepository
from app.services.order_service import OrderService
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
    ) -> None:
        self.scanner_factory = scanner_factory
        self.order_service_factory = order_service_factory
        self.opportunity_repository = opportunity_repository
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.config: dict[str, Any] = {}
        self.latest_opportunities: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.seen_order_keys: set[str] = set()
        self.last_scan_at: str | None = None

    def start(
        self,
        *,
        side: str = "BUY",
        symbols: list[str] | None = None,
        interval_seconds: int | None = None,
        limit: int = 5,
        place_orders: bool = False,
        confirm_live: bool = False,
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
        }
        self.running = True
        self.task = asyncio.create_task(self._run())
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
        return {
            "running": self.running,
            "config": self.config,
            "last_scan_at": self.last_scan_at,
            "latest_count": len(self.latest_opportunities),
            "execution_count": len(self.executions),
            "error_count": len(self.errors),
            "mode": "live" if settings.live_trading_mode else "paper",
            "live_ordering_requires_confirm_live": True,
        }

    async def _run(self) -> None:
        while self.running:
            try:
                self.scan_once()
            except Exception as exc:
                self.errors.append(
                    {
                        "time": datetime.utcnow().isoformat(),
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
        )
        limited = opportunities[: int(self.config.get("limit", 5))]
        self.latest_opportunities = [asdict(signal) for signal in limited]
        saved_ids: list[int] = []
        if self.opportunity_repository is not None:
            for signal in limited:
                saved_ids.append(self.opportunity_repository.save_opportunity(signal).id)
        self.last_scan_at = datetime.utcnow().isoformat()

        placed: list[dict[str, Any]] = []
        if self.config.get("place_orders"):
            for signal in limited:
                result = self._place_once(signal)
                if result is not None:
                    placed.append(result)

        return {
            "last_scan_at": self.last_scan_at,
            "count": len(self.latest_opportunities),
            "saved_ids": saved_ids,
            "opportunities": self.latest_opportunities,
            "placed": placed,
        }

    def _place_once(self, signal: Signal) -> dict[str, Any] | None:
        order_key = "|".join(
            [
                str(signal.tradingsymbol or signal.symbol),
                signal.side.upper(),
                signal.action,
                str(signal.expiry or ""),
                str(signal.strike or ""),
            ]
        )
        if order_key in self.seen_order_keys:
            return None

        self.seen_order_keys.add(order_key)
        service = self.order_service_factory()
        result = service.place_signal_order(
            signal,
            confirm_live=bool(self.config.get("confirm_live", False)),
        )
        execution = {
            "time": datetime.utcnow().isoformat(),
            "order_key": order_key,
            "signal": asdict(signal),
            "result": result,
        }
        self.executions.append(execution)
        return execution
