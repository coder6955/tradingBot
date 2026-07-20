from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.strategy_version_registry import StrategyVersionRegistry


def current_strategy_lineage() -> dict[str, str]:
    """Return the active strategy/config identity without writing to the database."""
    registry = StrategyVersionRegistry()
    snapshot = registry.current_config_snapshot()
    return {
        "strategy_name": str(settings.strategy_name),
        "strategy_version": str(settings.strategy_version),
        "config_hash": registry.config_hash(snapshot),
    }


def add_strategy_lineage(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**dict(payload or {}), **current_strategy_lineage()}
