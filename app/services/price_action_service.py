from __future__ import annotations

from typing import Any, Dict, List

from app.config import settings


class PriceActionService:
    """Evaluate price structure around the underlying before selecting an option."""

    def evaluate(self, snapshot: Dict[str, Any], trend: str, side: str) -> Dict[str, Any]:
        reasons: List[str] = []
        score = 0
        price = float(snapshot.get("price") or 0.0)
        bullish = trend.lower() == "bullish"

        if price <= 0:
            return {"score": 0, "passed": False, "reasons": ["underlying price is unavailable"], "details": {}}

        if bool(snapshot.get("ema_alignment")) == bullish:
            score += 18
        else:
            reasons.append("EMA trend alignment is weak")

        vwap = float(snapshot.get("vwap") or 0.0)
        if vwap > 0 and ((bullish and price >= vwap) or ((not bullish) and price <= vwap)):
            score += 15
        else:
            reasons.append("price is not favorably placed against VWAP")

        if bool(snapshot.get("macd_positive")) == bullish:
            score += 12
        else:
            reasons.append("MACD is not aligned with trade direction")

        if bool(snapshot.get("volume_confirmed")):
            score += 10
        else:
            reasons.append("volume confirmation is weak")

        levels = self._levels(snapshot)
        room = self._room_to_level(price, levels, bullish)
        min_room = settings.min_directional_room_pct / 100
        if room >= 0.004:
            score += 15
        elif room >= 0.002:
            score += 8
        else:
            reasons.append("nearest support/resistance leaves too little room")
        if side.upper() == "BUY" and room < min_room:
            reasons.append("directional option buying has insufficient room to nearest level")

        if self._is_breakout_or_rejection(price, levels, bullish):
            score += 15
        else:
            reasons.append("price has not confirmed breakout/rejection around key levels")

        rsi = float(snapshot.get("rsi") or 50.0)
        if bullish and 48 <= rsi <= 68:
            score += 15
        elif (not bullish) and 32 <= rsi <= 52:
            score += 15
        else:
            reasons.append("RSI is outside preferred momentum zone")

        passed = score >= 55 and not (side.upper() == "BUY" and room < min_room)
        if not passed:
            reasons.append("price action score is below threshold")

        return {
            "score": min(100, score),
            "passed": passed,
            "reasons": reasons,
            "details": {"levels": levels, "room_to_level_pct": round(room * 100, 2)},
        }

    def _levels(self, snapshot: Dict[str, Any]) -> Dict[str, float]:
        pdh = float(snapshot.get("previous_day_high") or 0.0)
        pdl = float(snapshot.get("previous_day_low") or 0.0)
        pdc = float(snapshot.get("previous_day_close") or 0.0)
        pivot = (pdh + pdl + pdc) / 3 if pdh and pdl and pdc else 0.0
        bc = (pdh + pdl) / 2 if pdh and pdl else 0.0
        tc = (pivot - bc) + pivot if pivot and bc else 0.0
        return {
            "previous_day_high": pdh,
            "previous_day_low": pdl,
            "previous_day_close": pdc,
            "day_high": float(snapshot.get("day_high") or 0.0),
            "day_low": float(snapshot.get("day_low") or 0.0),
            "pivot": round(pivot, 2),
            "cpr_top": round(max(tc, bc), 2) if tc and bc else 0.0,
            "cpr_bottom": round(min(tc, bc), 2) if tc and bc else 0.0,
        }

    def _room_to_level(self, price: float, levels: Dict[str, float], bullish: bool) -> float:
        if bullish:
            resistance_candidates = [levels.get("previous_day_high", 0.0), levels.get("day_high", 0.0)]
            above = [level for level in resistance_candidates if level > price]
            if not above:
                return 0.01
            return (min(above) - price) / price
        support_candidates = [levels.get("previous_day_low", 0.0), levels.get("day_low", 0.0)]
        below = [level for level in support_candidates if 0 < level < price]
        if not below:
            return 0.01
        return (price - max(below)) / price

    def _is_breakout_or_rejection(self, price: float, levels: Dict[str, float], bullish: bool) -> bool:
        pivot = levels.get("pivot", 0.0)
        cpr_top = levels.get("cpr_top", 0.0)
        cpr_bottom = levels.get("cpr_bottom", 0.0)
        if bullish:
            return bool((pivot and price >= pivot) or (cpr_top and price >= cpr_top))
        return bool((pivot and price <= pivot) or (cpr_bottom and price <= cpr_bottom))
