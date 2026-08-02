from __future__ import annotations

from typing import Any

from app.config import settings


class DecisionEngineService:
    """Shared deterministic scoring helpers used by live and research paths."""

    def score_breakdown(
        self,
        *,
        technical_score: int,
        market_score: int,
        price_score: int,
        chain_score: int,
        liquidity_score: int,
        quality_score: int,
        banknifty_score: int = 50,
    ) -> dict[str, Any]:
        capped_trend = min(technical_score, settings.max_trend_momentum_score)
        weights = {
            "trend_momentum": 0.14,
            "market_regime": 0.11,
            "price_action": 0.19,
            "option_chain_context": 0.07,
            "liquidity": 0.13,
            "option_quality": 0.16,
            "banknifty_intelligence": 0.20,
        }
        components = {
            "trend_momentum": capped_trend,
            "market_regime": market_score,
            "price_action": price_score,
            "option_chain_context": chain_score,
            "liquidity": liquidity_score,
            "option_quality": quality_score,
            "banknifty_intelligence": banknifty_score,
        }
        contributions = {
            key: round(components[key] * value, 2) for key, value in weights.items()
        }
        score = min(100, max(0, round(sum(contributions.values()))))
        return {
            "score": score,
            "threshold": settings.min_signal_score,
            "weights": weights,
            "components": components,
            "contributions": contributions,
            "caps": {
                "trend_momentum_raw": technical_score,
                "trend_momentum_capped_at": settings.max_trend_momentum_score,
            },
        }
