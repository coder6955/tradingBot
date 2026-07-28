from __future__ import annotations

from threading import RLock
from typing import Any

from app.config import settings
from app.services.strategy_version_registry import StrategyVersionRegistry


_lineage_lock = RLock()
_cached_lineage: dict[str, str] | None = None


def current_strategy_lineage() -> dict[str, str]:
    """Return an immutable runtime lineage without rebuilding it per tick."""
    global _cached_lineage
    with _lineage_lock:
        if _cached_lineage is None:
            registry = StrategyVersionRegistry()
            snapshot = registry.current_config_snapshot()
            _cached_lineage = {
                "strategy_name": str(settings.strategy_name),
                "strategy_version": str(settings.strategy_version),
                "config_hash": registry.config_hash(snapshot),
            }
        return dict(_cached_lineage)


def invalidate_strategy_lineage_cache() -> None:
    """Explicitly refresh lineage after a controlled runtime configuration change."""
    global _cached_lineage
    with _lineage_lock:
        _cached_lineage = None


def add_strategy_lineage(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**dict(payload or {}), **current_strategy_lineage()}
