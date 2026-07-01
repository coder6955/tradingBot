from __future__ import annotations

from typing import Any

from app.providers.kite_provider import KiteProvider


class AccountFundsService:
    """Read available trading funds from Kite for sizing and risk checks."""

    def __init__(self, kite_provider: KiteProvider | None = None) -> None:
        self.kite_provider = kite_provider or KiteProvider()

    def available_cash(self) -> float:
        margins = self.kite_provider.margins()
        return self._available_cash(margins)

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
