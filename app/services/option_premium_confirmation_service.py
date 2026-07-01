from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.database import Candle, OptionQuoteSnapshot, get_session
from app.services.trade_setup_service import OptionContract


class OptionPremiumConfirmationService:
    """Confirm that the selected option premium itself is participating in the move."""

    def evaluate(self, *, contract: OptionContract, side: str = "BUY", timeframe: str = "5minute") -> dict[str, Any]:
        if not settings.enable_option_premium_confirmation or side.upper() != "BUY":
            return {"enabled": False, "score": 100, "passed": True, "reasons": [], "details": {}}

        candles = self._recent_candles(contract.tradingsymbol, timeframe, settings.option_premium_lookback_candles + 1)
        reasons: list[str] = []
        if len(candles) < max(3, settings.option_premium_lookback_candles // 2):
            return self._evaluate_snapshots(contract, candle_count=len(candles))

        closes = [float(candle.close_price) for candle in candles]
        volumes = [float(candle.volume) for candle in candles]
        last_close = closes[-1]
        prev_close = closes[-2]
        recent_high = max(float(candle.high_price) for candle in candles[:-1])
        avg_volume = sum(volumes[:-1]) / max(len(volumes[:-1]), 1)
        premium_change_pct = ((last_close - closes[0]) / max(closes[0], 0.01)) * 100
        last_change_pct = ((last_close - prev_close) / max(prev_close, 0.01)) * 100
        breakout = last_close >= recent_high
        volume_expansion = volumes[-1] >= avg_volume * 1.10 if avg_volume > 0 else False
        spread_pct = self._spread_pct(contract)

        score = 20
        if premium_change_pct > 4:
            score += 25
        else:
            reasons.append("option premium momentum is weak")
        if last_change_pct > 0:
            score += 15
        else:
            reasons.append("latest option candle is not positive")
        if breakout:
            score += 20
        else:
            reasons.append("option premium has not broken recent high")
        if volume_expansion:
            score += 10
        else:
            reasons.append("option premium volume expansion is weak")
        if spread_pct <= settings.max_bid_ask_spread_pct:
            score += 10
        else:
            reasons.append("selected option spread is not suitable for premium confirmation")

        score = min(100, score)
        participation_confirmed = premium_change_pct > 2 and last_change_pct > 0 and (breakout or volume_expansion)
        passed = score >= settings.min_option_premium_confirmation_score and participation_confirmed
        if not participation_confirmed:
            reasons.append("selected option premium has not confirmed real participation")
        if not passed:
            reasons.append("option premium confirmation score is below threshold")

        return {
            "enabled": True,
            "score": score,
            "passed": passed,
            "reasons": list(dict.fromkeys(reasons)),
            "details": {
                "tradingsymbol": contract.tradingsymbol,
                "candles": len(candles),
                "first_close": round(closes[0], 2),
                "last_close": round(last_close, 2),
                "premium_change_pct": round(premium_change_pct, 2),
                "last_change_pct": round(last_change_pct, 2),
                "recent_high": round(recent_high, 2),
                "breakout": breakout,
                "volume_expansion": volume_expansion,
                "participation_confirmed": participation_confirmed,
                "spread_pct": round(spread_pct, 2),
            },
        }

    def _recent_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()

    def _spread_pct(self, contract: OptionContract) -> float:
        if not contract.bid or not contract.ask or not contract.last_price:
            return 100.0
        return ((contract.ask - contract.bid) / max(contract.last_price, 0.01)) * 100

    def _evaluate_snapshots(self, contract: OptionContract, *, candle_count: int) -> dict[str, Any]:
        snapshots = self._recent_snapshots(contract.tradingsymbol, settings.option_premium_lookback_candles + 1)
        if len(snapshots) < 3:
            return {
                "enabled": True,
                "score": 45,
                "passed": False,
                "reasons": ["not enough option premium candles/snapshots for confirmation"],
                "details": {"tradingsymbol": contract.tradingsymbol, "candles": candle_count, "snapshots": len(snapshots)},
            }
        prices = [float(item.last_price or 0) for item in snapshots if float(item.last_price or 0) > 0]
        if len(prices) < 3:
            return {
                "enabled": True,
                "score": 45,
                "passed": False,
                "reasons": ["option premium snapshots have insufficient price data"],
                "details": {"tradingsymbol": contract.tradingsymbol, "snapshots": len(snapshots)},
            }
        first = prices[0]
        last = prices[-1]
        prev = prices[-2]
        premium_change_pct = ((last - first) / max(first, 0.01)) * 100
        last_change_pct = ((last - prev) / max(prev, 0.01)) * 100
        rising = premium_change_pct > 2 and last_change_pct > 0
        spread_pct = self._spread_pct(contract)
        score = 35
        reasons: list[str] = []
        if rising:
            score += 35
        else:
            reasons.append("option premium snapshots do not show rising momentum")
        if spread_pct <= settings.max_bid_ask_spread_pct:
            score += 15
        else:
            reasons.append("selected option spread is not suitable for premium confirmation")
        score = min(100, score)
        passed = score >= settings.min_option_premium_confirmation_score and rising
        if not passed:
            reasons.append("option premium confirmation score is below threshold")
        return {
            "enabled": True,
            "score": score,
            "passed": passed,
            "reasons": list(dict.fromkeys(reasons)),
            "details": {
                "tradingsymbol": contract.tradingsymbol,
                "source": "snapshots",
                "candles": candle_count,
                "snapshots": len(snapshots),
                "first_price": round(first, 2),
                "last_price": round(last, 2),
                "premium_change_pct": round(premium_change_pct, 2),
                "last_change_pct": round(last_change_pct, 2),
                "participation_confirmed": rising,
                "spread_pct": round(spread_pct, 2),
            },
        }

    def _recent_snapshots(self, symbol: str, limit: int) -> list[OptionQuoteSnapshot]:
        session = get_session()
        try:
            rows = (
                session.query(OptionQuoteSnapshot)
                .filter(OptionQuoteSnapshot.tradingsymbol == symbol)
                .order_by(OptionQuoteSnapshot.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()
