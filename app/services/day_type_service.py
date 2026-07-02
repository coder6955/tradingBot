from __future__ import annotations

from datetime import datetime, time
from typing import Any

from app.config import settings
from app.services.database import Candle, get_session
from app.services.time_utils import ist_today


class DayTypeService:
    """Classify intraday structure so option buying is favored on expansion days."""

    def evaluate(self, *, symbol: str, trend: str, timeframe: str = "5minute") -> dict[str, Any]:
        if not settings.enable_day_type_filter:
            return {"enabled": False, "score": 100, "passed": True, "reasons": [], "details": {}}

        candles = self._today_candles(symbol=symbol.upper(), timeframe=timeframe)
        if len(candles) < 6:
            return {
                "enabled": True,
                "score": 50,
                "passed": False,
                "reasons": ["not enough intraday candles to classify day type"],
                "details": {"candles": len(candles)},
            }

        bullish = trend.lower() == "bullish"
        day_open = float(candles[0].open_price)
        last = candles[-1]
        last_close = float(last.close_price)
        day_high = max(float(candle.high_price) for candle in candles)
        day_low = min(float(candle.low_price) for candle in candles)
        day_range_pct = ((day_high - day_low) / max(day_open, 0.01)) * 100
        close_location = (last_close - day_low) / max(day_high - day_low, 0.01)
        opening = self._opening_range(candles)
        broke_or_high = last_close > opening["high"]
        broke_or_low = last_close < opening["low"]
        ema_like_slope = float(candles[-1].close_price) - float(candles[max(0, len(candles) - 6)].close_price)
        directional = (bullish and close_location >= 0.65 and ema_like_slope > 0) or ((not bullish) and close_location <= 0.35 and ema_like_slope < 0)
        opening_break = (bullish and broke_or_high) or ((not bullish) and broke_or_low)
        rotating = opening["low"] <= last_close <= opening["high"] and day_range_pct < 0.65

        score = 40
        reasons: list[str] = []
        day_type = "range"
        if directional and opening_break:
            score = 90
            day_type = "trend_expansion"
        elif directional:
            score = 72
            day_type = "directional_acceptance"
        elif rotating:
            score = 35
            day_type = "rotation_range"
            reasons.append("day type is rotational; option buying edge is weaker")
        else:
            score = 45
            day_type = "mixed"
            reasons.append("day has not confirmed expansion or directional acceptance")

        expansion_confirmed = day_type in {"trend_expansion", "directional_acceptance"}
        passed = score >= settings.min_day_type_score and expansion_confirmed
        if not expansion_confirmed:
            reasons.append("option buying requires an expansion or directional acceptance day")
        if not passed:
            reasons.append("day type score is below threshold")

        return {
            "enabled": True,
            "score": score,
            "passed": passed,
            "reasons": reasons,
            "details": {
                "day_type": day_type,
                "expansion_confirmed": expansion_confirmed,
                "day_open": round(day_open, 2),
                "day_high": round(day_high, 2),
                "day_low": round(day_low, 2),
                "last_close": round(last_close, 2),
                "day_range_pct": round(day_range_pct, 2),
                "close_location": round(close_location, 2),
                "opening_range": opening,
            },
        }

    def _today_candles(self, *, symbol: str, timeframe: str) -> list[Candle]:
        session = get_session()
        try:
            today = ist_today()
            start = datetime.combine(today, time.min)
            end = datetime.combine(today, time.max)
            return (
                session.query(Candle)
                .filter(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe, Candle.timestamp >= start, Candle.timestamp <= end)
                .order_by(Candle.timestamp.asc())
                .all()
            )
        finally:
            session.close()

    def _opening_range(self, candles: list[Candle]) -> dict[str, float]:
        start = candles[0].timestamp
        end_minute = start.minute + settings.opening_range_minutes
        opening: list[Candle] = []
        for candle in candles:
            delta_minutes = int((candle.timestamp - start).total_seconds() / 60)
            if delta_minutes <= settings.opening_range_minutes:
                opening.append(candle)
        opening = opening or candles[:6]
        return {
            "high": round(max(float(candle.high_price) for candle in opening), 2),
            "low": round(min(float(candle.low_price) for candle in opening), 2),
        }
