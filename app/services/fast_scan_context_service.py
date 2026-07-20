from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from app.config import settings
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


@dataclass(frozen=True)
class FastScanContext:
    created_at: datetime
    symbols: tuple[str, ...]
    market_snapshots: dict[str, dict[str, Any]]
    strategy_version: str
    config_hash: str


class FastScanContextService:
    """Application-scoped immutable slow-evidence snapshot for candidate promotion."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._context: FastScanContext | None = None

    def refresh(self, *, symbols: list[str], market_snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
        lineage = current_strategy_lineage()
        context = FastScanContext(
            created_at=ist_now_naive(),
            symbols=tuple(str(symbol).upper() for symbol in symbols),
            market_snapshots=deepcopy(market_snapshots),
            strategy_version=str(lineage["strategy_version"]),
            config_hash=str(lineage["config_hash"]),
        )
        with self._lock:
            self._context = context
        return self.status()

    def validate(self, *, symbol: str = "BANKNIFTY") -> dict[str, Any]:
        with self._lock:
            context = self._context
        if context is None:
            return {"passed": False, "reason": "fast_scan_context_missing"}
        age = max(0.0, (ist_now_naive() - context.created_at).total_seconds())
        lineage = current_strategy_lineage()
        if age > settings.fast_scan_context_max_age_seconds:
            return {"passed": False, "reason": "fast_scan_context_stale", "age_seconds": round(age, 3)}
        if context.config_hash != lineage["config_hash"]:
            return {"passed": False, "reason": "fast_scan_context_config_mismatch", "age_seconds": round(age, 3)}
        if symbol.upper() not in context.symbols:
            return {"passed": False, "reason": "fast_scan_symbol_not_prepared", "age_seconds": round(age, 3)}
        return {
            "passed": True,
            "age_seconds": round(age, 3),
            "created_at": context.created_at.isoformat(sep=" "),
            "strategy_version": context.strategy_version,
            "config_hash": context.config_hash,
        }

    def status(self) -> dict[str, Any]:
        result = self.validate()
        result["max_age_seconds"] = settings.fast_scan_context_max_age_seconds
        return result
