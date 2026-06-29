from __future__ import annotations


class IndicatorScoringService:
    """Score a symbol using a simple rule-based indicator framework."""

    def score_symbol(
        self,
        rsi: float,
        adx: float,
        macd_positive: bool,
        ema_alignment: bool,
        vwap_above_price: bool,
        volume_confirmed: bool,
        trend_bullish: bool,
        market_context: str,
    ) -> int:
        score = 0
        if trend_bullish:
            score += 25
        if ema_alignment:
            score += 15
        if vwap_above_price:
            score += 10
        if macd_positive:
            score += 10
        if volume_confirmed:
            score += 10
        if 50 <= rsi <= 70:
            score += 10
        if adx >= 20:
            score += 10
        if market_context.lower() == "strong":
            score += 10
        return min(score, 100)
