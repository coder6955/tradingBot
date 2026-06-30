from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from app.config import settings


class MarketRegimeService:
    """Evaluate broad-market conditions before any symbol-level option trade."""

    def evaluate(
        self,
        symbol: str,
        trend: str,
        side: str,
        nifty: Dict[str, Any] | None,
        banknifty: Dict[str, Any] | None,
        vix: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        score = 50

        calendar = self.calendar_check(symbol)
        if not calendar["passed"]:
            reasons.extend(calendar["reasons"])

        bullish = trend.lower() == "bullish"
        nifty_bullish = bool((nifty or {}).get("trend_bullish"))
        bank_bullish = bool((banknifty or {}).get("trend_bullish"))
        if bullish and nifty_bullish:
            score += 15
        elif not bullish and not nifty_bullish:
            score += 15
        else:
            score -= 10
            reasons.append("symbol trend is not aligned with NIFTY regime")

        if symbol.upper() in {"BANKNIFTY", "HDFCBANK", "ICICIBANK", "AXISBANK", "SBIN"}:
            if bullish == bank_bullish:
                score += 10
            else:
                score -= 10
                reasons.append("banking symbol is not aligned with BANKNIFTY regime")

        vix_value = float((vix or {}).get("price") or 0.0)
        if vix_value > 0:
            limit = settings.max_vix_for_selling if side.upper() == "SELL" else settings.max_vix_for_buying
            if vix_value <= limit:
                score += 15
            else:
                score -= 20
                reasons.append(f"India VIX {vix_value:.2f} is above configured limit {limit:.2f}")
        else:
            reasons.append("India VIX was unavailable; regime score reduced")
            score -= 5

        passed = score >= settings.min_market_regime_score and calendar["passed"]
        if score < settings.min_market_regime_score:
            reasons.append("market regime score is below threshold")

        return {
            "score": max(0, min(100, score)),
            "passed": passed,
            "reasons": reasons,
            "details": {
                "nifty_trend_bullish": nifty_bullish,
                "banknifty_trend_bullish": bank_bullish,
                "vix": vix_value,
                "calendar": calendar,
            },
        }

    def calendar_check(self, symbol: str) -> Dict[str, Any]:
        reasons: List[str] = []
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.date().isoformat()

        blocked_dates = {item.strip() for item in settings.blocked_event_dates.split(",") if item.strip()}
        if today in blocked_dates:
            reasons.append(f"{today} is configured as a blocked event date")

        blocked_symbols = {item.strip().upper() for item in settings.blocked_symbols.split(",") if item.strip()}
        if symbol.upper() in blocked_symbols:
            reasons.append(f"{symbol.upper()} is configured as blocked")

        if settings.enforce_market_hours:
            market_open = self._parse_time(settings.market_open_time)
            market_close = self._parse_time(settings.market_close_time)
            current = now.time()
            if now.weekday() >= 5:
                reasons.append("market is closed on weekends")
            elif current < market_open or current > market_close:
                reasons.append("current time is outside configured trade-entry window")

        return {"passed": not reasons, "reasons": reasons, "timestamp": now.isoformat()}

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
