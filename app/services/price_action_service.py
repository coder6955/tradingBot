from __future__ import annotations

from typing import Any, Dict, List

from app.config import settings


class PriceActionService:
    """Evaluate price structure around the underlying before selecting an option."""

    def evaluate(
        self, snapshot: Dict[str, Any], trend: str, side: str
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        score = 0
        price = float(snapshot.get("price") or 0.0)
        bullish = trend.lower() == "bullish"

        if price <= 0:
            return {
                "score": 0,
                "passed": False,
                "reasons": ["underlying price is unavailable"],
                "details": {},
            }

        ema_alignment = snapshot.get("ema_alignment")
        if ema_alignment is not None and bool(ema_alignment) == bullish:
            score += 18
        else:
            reasons.append(
                "EMA trend alignment is unavailable"
                if ema_alignment is None
                else "EMA trend alignment is weak"
            )

        vwap = float(snapshot.get("vwap") or 0.0)
        if vwap > 0 and (
            (bullish and price >= vwap) or ((not bullish) and price <= vwap)
        ):
            score += 15
        else:
            reasons.append("price is not favorably placed against VWAP")

        macd_positive = snapshot.get("macd_positive")
        if macd_positive is not None and bool(macd_positive) == bullish:
            score += 12
        else:
            reasons.append(
                "MACD direction is unavailable"
                if macd_positive is None
                else "MACD is not aligned with trade direction"
            )

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
            reasons.append(
                "directional option buying has insufficient room to nearest level"
            )

        breakout_confirmed = self._is_breakout_or_rejection(price, levels, bullish)
        if breakout_confirmed:
            score += 15
        else:
            reasons.append(
                "price has not confirmed breakout/rejection around key levels"
            )

        candle_confirmed = self._candle_confirmed(snapshot, levels, bullish)
        if candle_confirmed:
            score += 8
        elif settings.require_candle_confirmation:
            reasons.append(
                f"{settings.candle_confirmation_timeframe} candle close has not confirmed trade direction"
            )

        rsi = float(snapshot.get("rsi") or 50.0)
        if bullish and 48 <= rsi <= 68:
            score += 15
        elif (not bullish) and 32 <= rsi <= 52:
            score += 15
        else:
            reasons.append("RSI is outside preferred momentum zone")

        chop = self._banknifty_chop_reason(snapshot, levels)
        if chop:
            reasons.append(chop)
            score = max(0, score - 20)

        hard_block_reasons: List[str] = []
        if (
            side.upper() == "BUY"
            and room < min_room
            and settings.enable_directional_room_hard_gate
            and not (breakout_confirmed and candle_confirmed)
        ):
            hard_block_reasons.append(
                "directional option buying has insufficient room to nearest level"
            )
        if chop:
            hard_block_reasons.append(chop)
        passed = not hard_block_reasons
        if score < settings.min_price_action_score:
            reasons.append("price action score is below preferred threshold")

        return {
            "score": min(100, score),
            "passed": passed,
            "reasons": reasons,
            "details": {
                "levels": levels,
                "room_to_level_pct": round(room * 100, 2),
                "candle_confirmed": candle_confirmed,
                "breakout_confirmed": breakout_confirmed,
                "directional_room_is_hard_gate": settings.enable_directional_room_hard_gate,
                "chop_filter": chop or "",
                "hard_block": bool(hard_block_reasons),
                "hard_block_reasons": list(dict.fromkeys(hard_block_reasons)),
                "indicator_availability": {
                    "ema_alignment": "unknown"
                    if ema_alignment is None
                    else "available",
                    "macd_positive": "unknown"
                    if macd_positive is None
                    else "available",
                },
            },
        }

    def _banknifty_chop_reason(
        self, snapshot: Dict[str, Any], levels: Dict[str, float]
    ) -> str:
        if str(snapshot.get("symbol") or "").upper() != "BANKNIFTY":
            return ""
        price = float(snapshot.get("price") or 0.0)
        adx = float(snapshot.get("adx") or 0.0)
        cpr_top = float(levels.get("cpr_top") or 0.0)
        cpr_bottom = float(levels.get("cpr_bottom") or 0.0)
        day_high = float(levels.get("day_high") or 0.0)
        day_low = float(levels.get("day_low") or 0.0)
        inside_cpr = cpr_bottom > 0 and cpr_top > 0 and cpr_bottom <= price <= cpr_top
        day_range_pct = (
            ((day_high - day_low) / price) * 100
            if price > 0 and day_high > day_low
            else 0.0
        )
        if inside_cpr and adx < 18:
            return "Bank Nifty is inside CPR with weak trend strength"
        if (
            adx < 12
            and day_range_pct < 0.35
            and not bool(snapshot.get("volume_confirmed"))
        ):
            return "Bank Nifty is too compressed/choppy for directional option buying"
        return ""

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

    def _room_to_level(
        self, price: float, levels: Dict[str, float], bullish: bool
    ) -> float:
        if bullish:
            resistance_candidates = [
                levels.get("previous_day_high", 0.0),
                levels.get("day_high", 0.0),
            ]
            above = [level for level in resistance_candidates if level > price]
            if not above:
                return 0.01
            return (min(above) - price) / price
        support_candidates = [
            levels.get("previous_day_low", 0.0),
            levels.get("day_low", 0.0),
        ]
        below = [level for level in support_candidates if 0 < level < price]
        if not below:
            return 0.01
        return (price - max(below)) / price

    def _is_breakout_or_rejection(
        self, price: float, levels: Dict[str, float], bullish: bool
    ) -> bool:
        pivot = levels.get("pivot", 0.0)
        cpr_top = levels.get("cpr_top", 0.0)
        cpr_bottom = levels.get("cpr_bottom", 0.0)
        if bullish:
            return bool((pivot and price >= pivot) or (cpr_top and price >= cpr_top))
        return bool((pivot and price <= pivot) or (cpr_bottom and price <= cpr_bottom))

    def _candle_confirmed(
        self, snapshot: Dict[str, Any], levels: Dict[str, float], bullish: bool
    ) -> bool:
        close = float(
            snapshot.get("last_candle_close")
            or snapshot.get("candle_close")
            or snapshot.get("price")
            or 0.0
        )
        if close <= 0:
            return False
        pivot = levels.get("pivot", 0.0)
        cpr_top = levels.get("cpr_top", 0.0)
        cpr_bottom = levels.get("cpr_bottom", 0.0)
        if bullish:
            return bool((pivot and close >= pivot) or (cpr_top and close >= cpr_top))
        return bool((pivot and close <= pivot) or (cpr_bottom and close <= cpr_bottom))
