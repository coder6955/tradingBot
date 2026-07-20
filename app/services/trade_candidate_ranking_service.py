from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.trade_setup_service import OptionContract


class TradeCandidateRankingService:
    """Rank executable opportunities using after-cost utility.

    Until outcomes are independently calibrated this intentionally does not emit
    a win probability or claim positive expectancy. The utility is only an
    ordering signal and is persisted so later outcomes can validate it.
    """

    def evaluate(
        self,
        *,
        contract: OptionContract,
        prices: dict[str, float],
        combined_score: int,
        market_state: dict[str, Any],
        setup_family: dict[str, Any],
        momentum_phase: dict[str, Any],
        volatility_edge: dict[str, Any],
        probability: float | None = None,
    ) -> dict[str, Any]:
        entry = self._float(prices.get("entry_price"))
        stop = self._float(prices.get("stop_loss"))
        target = self._float(prices.get("target_1"))
        risk = max(0.0, entry - stop)
        reward = max(0.0, target - entry)
        rr = reward / risk if risk > 0 else 0.0
        spread_pct = ((contract.ask - contract.bid) / max(contract.ask or contract.last_price, 0.01) * 100) if contract.ask > contract.bid > 0 else settings.max_bid_ask_spread_pct
        estimated_cost_pct = max(0.0, spread_pct / 2) + settings.candidate_round_trip_cost_pct
        confidence = self._float(market_state.get("confidence"), 0.0)
        uncertainty = self._float(market_state.get("uncertainty"), 1.0)
        policy_score = self._float(setup_family.get("policy_score"), 50.0)
        momentum_score = self._float(momentum_phase.get("score"), 0.0)
        vol_score = self._float(volatility_edge.get("score"), 50.0)
        liquidity = self._liquidity(contract)

        quality = (
            combined_score * 0.25
            + confidence * 100 * 0.15
            + policy_score * 0.15
            + momentum_score * 0.15
            + vol_score * 0.10
            + liquidity * 0.20
        )
        rr_component = max(-20.0, min(20.0, (rr - 1.0) * 16.0))
        cost_penalty = min(25.0, estimated_cost_pct * 2.5)
        uncertainty_penalty = min(30.0, uncertainty * 30.0)
        utility = max(0.0, min(100.0, quality + rr_component - cost_penalty - uncertainty_penalty))

        expected_net_value = None
        expectancy_source = "unavailable_until_outcome_calibration"
        if probability is not None and 0 < probability < 1 and risk > 0:
            gross_ev = probability * reward - (1 - probability) * risk
            costs = entry * estimated_cost_pct / 100
            expected_net_value = round(gross_ev - costs, 2)
            expectancy_source = "calibrated_probability_after_costs"

        eligible = utility >= settings.candidate_min_utility_score and rr >= settings.min_risk_reward
        abstention = None
        if rr < settings.min_risk_reward:
            abstention = "INSUFFICIENT_AFTER_COST_REWARD_RISK"
        elif utility < settings.candidate_min_utility_score:
            abstention = "CANDIDATE_UTILITY_BELOW_THRESHOLD"
        return {
            "eligible": eligible,
            "ranking_score": round(utility, 2),
            "utility_score": round(utility, 2),
            "expected_net_value": expected_net_value,
            "expectancy_source": expectancy_source,
            "abstention_code": abstention,
            "components": {
                "combined_score": combined_score,
                "market_confidence": round(confidence, 3),
                "setup_policy_score": round(policy_score, 2),
                "momentum_score": round(momentum_score, 2),
                "volatility_score": round(vol_score, 2),
                "liquidity_score": round(liquidity, 2),
                "risk_reward": round(rr, 3),
                "estimated_round_trip_cost_pct": round(estimated_cost_pct, 3),
                "uncertainty": round(uncertainty, 3),
            },
            "warning": "Ranking utility is not a profit probability and cannot establish expectancy before independent outcome calibration.",
        }

    def _liquidity(self, contract: OptionContract) -> float:
        score = 0.0
        if contract.bid > 0 and contract.ask > contract.bid:
            spread = (contract.ask - contract.bid) / contract.ask * 100
            score += max(0.0, 45.0 - spread * 8.0)
        if contract.volume >= 1000:
            score += 25.0
        elif contract.volume > 0:
            score += 10.0
        if contract.open_interest >= 10000:
            score += 30.0
        elif contract.open_interest > 0:
            score += 12.0
        return min(100.0, score)

    def _float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value if value is not None else default)
        except (TypeError, ValueError):
            return default
