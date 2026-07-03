from __future__ import annotations

from typing import Any, Dict, List

from app.services.trade_setup_service import OptionContract


class OptionChainService:
    """Score option-chain evidence around the selected contract."""

    def analyze(
        self,
        spot_price: float,
        trend: str,
        side: str,
        selected: OptionContract,
        contracts: List[OptionContract],
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        if not contracts:
            return {"score": 0, "passed": False, "reasons": ["option chain contracts are unavailable"], "details": {}}

        calls = [contract for contract in contracts if contract.option_type == "CE"]
        puts = [contract for contract in contracts if contract.option_type == "PE"]
        call_oi = sum(contract.open_interest for contract in calls)
        put_oi = sum(contract.open_interest for contract in puts)
        call_volume = sum(contract.volume for contract in calls)
        put_volume = sum(contract.volume for contract in puts)
        oi_complete = call_oi > 0 and put_oi > 0
        volume_complete = call_volume > 0 and put_volume > 0
        pcr_oi = put_oi / call_oi if oi_complete else None
        pcr_volume = put_volume / call_volume if volume_complete else None

        support = self._max_oi_strike([contract for contract in puts if contract.strike <= spot_price and contract.open_interest > 0]) if oi_complete else 0.0
        resistance = self._max_oi_strike([contract for contract in calls if contract.strike >= spot_price and contract.open_interest > 0]) if oi_complete else 0.0
        max_pain = self._max_pain(contracts) if oi_complete else None
        bullish = trend.lower() == "bullish"
        score = 0

        if pcr_oi is not None and 0.7 <= pcr_oi <= 1.6:
            score += 18
        elif pcr_oi is not None and pcr_oi > 0:
            score += 8
            reasons.append("PCR is stretched or one-sided")
        else:
            reasons.append("PCR/max pain unavailable because OI data is incomplete")

        if selected.open_interest > 0 and selected.volume > 0:
            score += 18
        else:
            reasons.append("selected option has weak OI/volume")

        if selected.bid > 0 and selected.ask > 0 and selected.last_price > 0:
            spread_pct = ((selected.ask - selected.bid) / selected.last_price) * 100
            if spread_pct <= 2:
                score += 18
            elif spread_pct <= 5:
                score += 10
                reasons.append("selected option spread is acceptable but not ideal")
            else:
                reasons.append("selected option spread is too wide")
        else:
            reasons.append("selected option has incomplete bid/ask data")

        if bullish:
            if support and support <= spot_price:
                score += 10
            else:
                reasons.append("put OI support below spot is weak")
            if resistance and (resistance - spot_price) / spot_price >= 0.003:
                score += 12
            else:
                reasons.append("call OI resistance is too close")
            if max_pain and spot_price >= max_pain:
                score += 9
        else:
            if resistance and resistance >= spot_price:
                score += 10
            else:
                reasons.append("call OI resistance above spot is weak")
            if support and (spot_price - support) / spot_price >= 0.003:
                score += 12
            else:
                reasons.append("put OI support is too close")
            if max_pain and spot_price <= max_pain:
                score += 9

        if self._selected_contract_is_sensible(spot_price, bullish, side, selected):
            score += 15
        else:
            reasons.append("selected option strike is not ideal for this setup type")

        passed = score >= 55
        if not passed:
            reasons.append("option-chain score is below threshold")

        return {
            "score": min(100, score),
            "passed": passed,
            "reasons": reasons,
            "details": {
                "pcr_oi": round(pcr_oi, 2) if pcr_oi is not None else None,
                "pcr_volume": round(pcr_volume, 2) if pcr_volume is not None else None,
                "support_strike": support,
                "resistance_strike": resistance,
                "max_pain": max_pain,
                "oi_data_complete": oi_complete,
                "volume_data_complete": volume_complete,
                "selected_oi": selected.open_interest,
                "selected_volume": selected.volume,
            },
        }

    def _max_oi_strike(self, contracts: List[OptionContract]) -> float:
        if not contracts:
            return 0.0
        return max(contracts, key=lambda contract: contract.open_interest).strike

    def _max_pain(self, contracts: List[OptionContract]) -> float:
        strikes = sorted({contract.strike for contract in contracts})
        if not strikes:
            return 0.0
        pain_by_strike: Dict[float, float] = {}
        for settlement in strikes:
            pain = 0.0
            for contract in contracts:
                if contract.option_type == "CE":
                    pain += max(0.0, settlement - contract.strike) * contract.open_interest
                else:
                    pain += max(0.0, contract.strike - settlement) * contract.open_interest
            pain_by_strike[settlement] = pain
        return min(pain_by_strike, key=pain_by_strike.get)

    def _selected_contract_is_sensible(self, spot_price: float, bullish: bool, side: str, selected: OptionContract) -> bool:
        if side.upper() == "SELL":
            if bullish:
                return selected.option_type == "PE" and selected.strike <= spot_price
            return selected.option_type == "CE" and selected.strike >= spot_price
        if bullish:
            return selected.option_type == "CE" and selected.strike <= spot_price * 1.005
        return selected.option_type == "PE" and selected.strike >= spot_price * 0.995
