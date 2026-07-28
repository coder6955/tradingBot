from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.time_utils import ist_now_naive


class AccountFundsService:
    """Read available trading funds from Kite for sizing and risk checks."""

    _cached_margins: dict[str, Any] | None = None
    _cached_margins_at: datetime | None = None

    def __init__(self, kite_provider: KiteProvider | None = None) -> None:
        self.kite_provider = kite_provider or KiteProvider()

    def available_cash(self) -> float:
        margins = self._margins()
        return self._available_cash(margins)

    @classmethod
    def invalidate_cache(cls) -> None:
        cls._cached_margins = None
        cls._cached_margins_at = None

    def _margins(self) -> dict[str, Any]:
        now = ist_now_naive()
        ttl = max(0, int(settings.account_funds_cache_ttl_seconds))
        if self._cached_margins is not None and self._cached_margins_at is not None and (now - self._cached_margins_at).total_seconds() <= ttl:
            return self._cached_margins
        margins = self.kite_provider.margins()
        AccountFundsService._cached_margins = margins
        AccountFundsService._cached_margins_at = now
        return margins

    def _available_cash(self, margins: dict[str, Any]) -> float:
        candidates = [
            margins.get("available", {}).get("cash") if isinstance(margins.get("available"), dict) else None,
            margins.get("equity", {}).get("available", {}).get("cash") if isinstance(margins.get("equity"), dict) else None,
            margins.get("equity", {}).get("net") if isinstance(margins.get("equity"), dict) else None,
        ]
        for value in candidates:
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        raise RuntimeError("Kite available cash was not found in margins response")
