from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.greeks_service import GreeksService
from app.services.trade_setup_service import OptionContract


class OptionQualityService:
    """Professional option-buying filters: Greeks, IV, theta drag, moneyness, and expiry risk."""

    def __init__(self, greeks_service: GreeksService | None = None) -> None:
        self.greeks_service = greeks_service or GreeksService()

    def evaluate(
        self,
        *,
        spot_price: float,
        contract: OptionContract,
        entry_price: float,
        side: str,
    ) -> dict[str, Any]:
        if side.upper() != "BUY":
            return {
                "score": 100,
                "passed": True,
                "reasons": [],
                "details": {"skipped": "quality gate applies to option buying"},
            }

        reasons: list[str] = []
        score = 100
        greeks = self.greeks_service.estimate(
            spot_price=spot_price,
            strike=contract.strike,
            option_price=entry_price,
            option_type=contract.option_type,
            expiry=contract.expiry,
        )
        abs_delta = abs(greeks.delta)

        if abs_delta < settings.min_option_buy_delta:
            score -= 25
            reasons.append("delta is too low for directional option buying")
        elif abs_delta > settings.max_option_buy_delta:
            score -= 12
            reasons.append("delta is too high; risk/reward may be stock-like")

        if greeks.theta_pct > settings.max_option_buy_theta_pct:
            score -= 25
            reasons.append("theta decay is too high relative to premium")

        if greeks.implied_volatility < settings.min_option_buy_iv:
            score -= 10
            reasons.append("implied volatility estimate is suspiciously low")
        elif greeks.implied_volatility > settings.max_option_buy_iv:
            score -= 20
            reasons.append("implied volatility is too high for option buying")

        if greeks.days_to_expiry <= 1:
            score -= 30
            reasons.append("near-expiry gamma/theta risk is too high for buying")
        elif greeks.days_to_expiry <= 3:
            score -= 12
            reasons.append("short expiry leaves limited time for trade to work")

        spread_pct = self._spread_pct(contract)
        if spread_pct > 3:
            score -= 15
            reasons.append("spread/slippage is high for option buying")

        if entry_price < settings.min_option_buy_premium:
            score -= 20
            reasons.append("premium is too low and vulnerable to tick noise")

        score = max(0, min(100, score))
        passed = score >= settings.min_option_quality_score
        if not passed:
            reasons.append("option quality score is below threshold")

        return {
            "score": score,
            "passed": passed,
            "reasons": list(dict.fromkeys(reasons)),
            "details": {
                "greeks": greeks.to_dict(),
                "abs_delta": round(abs_delta, 4),
                "spread_pct": round(spread_pct, 2),
                "min_quality_score": settings.min_option_quality_score,
            },
        }

    def _spread_pct(self, contract: OptionContract) -> float:
        if not contract.bid or not contract.ask or not contract.last_price:
            return 100.0
        return ((contract.ask - contract.bid) / max(contract.last_price, 0.01)) * 100
