from __future__ import annotations

from typing import Dict, Any


class MarketAnalysisService:
    """Create a simple market regime summary from core market inputs."""

    def analyze_market(self, market_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        nifty = float(market_snapshot.get("nifty", 0.0))
        banknifty = float(market_snapshot.get("banknifty", 0.0))
        vix = float(market_snapshot.get("vix", 0.0))

        nifty_trend = "bullish" if nifty > 20000 else "bearish"
        banknifty_trend = "bullish" if banknifty > 45000 else "bearish"
        market_sentiment = "risk-on" if vix < 20 else "risk-off"
        score = (
            75
            + (10 if nifty_trend == "bullish" else -5)
            + (10 if banknifty_trend == "bullish" else -5)
        )

        return {
            "nifty_trend": nifty_trend,
            "banknifty_trend": banknifty_trend,
            "vix": vix,
            "market_sentiment": market_sentiment,
            "score": max(0, min(100, score)),
        }
