from __future__ import annotations

from typing import Any, Dict, List


class HistoricalCandlesService:
    """Generate simple synthetic historical candles for development and dashboarding."""

    def generate_candles(
        self, symbol: str, count: int, base_price: float
    ) -> List[Dict[str, Any]]:
        candles: List[Dict[str, Any]] = []
        price = float(base_price)
        for idx in range(count):
            open_price = price
            close_price = open_price + ((idx % 3) - 1) * 20.0
            high_price = max(open_price, close_price) + 10.0
            low_price = min(open_price, close_price) - 10.0
            candles.append(
                {
                    "symbol": symbol,
                    "timestamp": f"2024-01-01T09:{idx + 15:02d}:00",
                    "open": round(open_price, 2),
                    "high": round(high_price, 2),
                    "low": round(low_price, 2),
                    "close": round(close_price, 2),
                    "volume": 1000 + idx * 100,
                }
            )
            price = close_price
        return candles
