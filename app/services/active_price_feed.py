from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.time_utils import ist_now_naive


@dataclass(frozen=True)
class PriceTick:
    instrument: str
    price: float
    timestamp: datetime
    source: str


class ActivePriceFeed(Protocol):
    def latest_price(self, *, exchange: str, tradingsymbol: str) -> PriceTick | None:
        ...


class KitePollingPriceFeed:
    """Current active-trade feed; replaceable with Kite ticker/WebSocket later."""

    def __init__(self, provider: KiteProvider) -> None:
        self.provider = provider

    def latest_price(self, *, exchange: str, tradingsymbol: str) -> PriceTick | None:
        instrument = f"{exchange or settings.option_exchange}:{tradingsymbol}"
        try:
            quote = self.provider.quote([instrument])
        except Exception:
            return None
        data = quote.get(instrument) or quote.get(tradingsymbol) or {}
        if not isinstance(data, dict):
            return None
        for key in ("last_price", "last_traded_price"):
            value = data.get(key)
            try:
                if value is not None:
                    return PriceTick(instrument=instrument, price=float(value), timestamp=ist_now_naive(), source="kite_polling")
            except (TypeError, ValueError):
                continue
        return None
