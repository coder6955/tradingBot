from __future__ import annotations

from typing import Any, Dict, List


class MarketService:
    """Placeholder service for future market data integration."""

    def __init__(self) -> None:
        self._state: Dict[str, Any] = {}

    def get_market_status(self) -> Dict[str, Any]:
        return {
            "status": "market-open",
            "source": "placeholder",
            "symbols": ["NIFTY", "BANKNIFTY"],
        }

    def get_top_opportunities(self, limit: int = 5) -> List[Dict[str, Any]]:
        return [
            {
                "symbol": "NIFTY",
                "score": 84,
                "signal": "BUY_CE",
                "confidence": 0.82,
            }
            for _ in range(min(limit, 5))
        ]
