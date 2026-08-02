from __future__ import annotations

import asyncio
from typing import Any

from app.services.data_ingestion_service import DataIngestionService
from app.services.time_utils import ist_now_naive


class OptionSnapshotCollectorService:
    """Continuously collect option-chain snapshots for IV/Greeks/OI history."""

    def __init__(self, ingestion_service: DataIngestionService) -> None:
        self.ingestion_service = ingestion_service
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.symbols: list[str] = ["BANKNIFTY"]
        self.interval_seconds = 300
        self.strike_window_pct = 4.0
        self.max_contracts_per_symbol = 120
        self.last_run_at: str | None = None
        self.last_result: dict[str, Any] = {}
        self.errors: list[dict[str, Any]] = []

    def start(
        self,
        *,
        symbols: list[str],
        interval_seconds: int = 300,
        strike_window_pct: float = 4.0,
        max_contracts_per_symbol: int = 120,
    ) -> dict[str, Any]:
        if self.running:
            return self.status()
        self.symbols = symbols or self.symbols
        self.interval_seconds = max(30, int(interval_seconds))
        self.strike_window_pct = float(strike_window_pct)
        self.max_contracts_per_symbol = int(max_contracts_per_symbol)
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
            "symbols": self.symbols,
            "interval_seconds": self.interval_seconds,
            "strike_window_pct": self.strike_window_pct,
            "max_contracts_per_symbol": self.max_contracts_per_symbol,
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
            "error_count": len(self.errors),
            "recent_errors": self.errors[-5:],
        }

    def collect_once(self) -> dict[str, Any]:
        result = self.ingestion_service.capture_option_snapshots(
            symbols=self.symbols,
            strike_window_pct=self.strike_window_pct,
            max_contracts_per_symbol=self.max_contracts_per_symbol,
        )
        self.last_run_at = ist_now_naive().isoformat(timespec="seconds")
        self.last_result = result
        return result

    async def _run(self) -> None:
        while self.running:
            try:
                await asyncio.to_thread(self.collect_once)
            except Exception as exc:
                self.errors.append(
                    {
                        "time": ist_now_naive().isoformat(timespec="seconds"),
                        "error": str(exc),
                    }
                )
            await asyncio.sleep(float(self.interval_seconds))
