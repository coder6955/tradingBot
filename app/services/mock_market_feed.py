from __future__ import annotations

from typing import Dict, Any


class MockMarketFeed:
    """Provide deterministic mock market snapshots for development and testing."""

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        base = {
            "NIFTY": {
                "price": 22150.0,
                "rsi": 58,
                "adx": 25,
                "macd_positive": True,
                "ema_alignment": True,
                "vwap_above_price": True,
                "volume_confirmed": True,
                "trend_bullish": True,
                "market_context": "strong",
            },
            "BANKNIFTY": {
                "price": 47200.0,
                "rsi": 54,
                "adx": 22,
                "macd_positive": True,
                "ema_alignment": True,
                "vwap_above_price": True,
                "volume_confirmed": True,
                "trend_bullish": True,
                "market_context": "strong",
            },
        }
        return base.get(symbol, {
            "price": 100.0,
            "rsi": 50,
            "adx": 15,
            "macd_positive": False,
            "ema_alignment": False,
            "vwap_above_price": False,
            "volume_confirmed": False,
            "trend_bullish": False,
            "market_context": "neutral",
        })
