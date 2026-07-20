from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.services.database import Candle, get_session


@dataclass(frozen=True)
class _Frame:
    timeframe: str
    responsibility: str
    direction: str
    score: int
    strength: float
    atr_pct: float
    range_position: float
    samples: int


class MultiTimeframeContextService:
    """Assign one responsibility to each timeframe and report alignment.

    This service is deliberately read-only. Missing frames increase uncertainty;
    they do not manufacture confirmation from a lower timeframe.
    """

    RESPONSIBILITIES = {
        "day": "primary_structure",
        "30minute": "structural_bias",
        "15minute": "session_character",
        "5minute": "trade_regime",
        "1minute": "entry_trigger",
    }
    ALIASES = {
        "day": ("day", "1day", "daily"),
        "30minute": ("30minute", "30min"),
        "15minute": ("15minute", "15min"),
        "5minute": ("5minute", "5min"),
        "1minute": ("1minute", "1min"),
    }

    def evaluate(self, *, symbol: str, trend: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        desired = "bullish" if trend.lower() == "bullish" else "bearish"
        frames: list[_Frame] = []
        errors: list[str] = []
        for timeframe, responsibility in self.RESPONSIBILITIES.items():
            try:
                candles = self._load(symbol, self.ALIASES[timeframe], 80)
            except Exception as exc:  # database loss must be observable but must not crash scanning
                errors.append(f"{timeframe}_load_failed:{type(exc).__name__}")
                candles = []
            if len(candles) >= 6:
                frames.append(self._frame(timeframe, responsibility, candles))

        available = len(frames)
        aligned_weight = 0.0
        opposed_weight = 0.0
        total_weight = 0.0
        weights = {"day": 1.2, "30minute": 1.2, "15minute": 1.0, "5minute": 1.3, "1minute": 0.7}
        for frame in frames:
            weight = weights[frame.timeframe]
            total_weight += weight
            if frame.direction == desired:
                aligned_weight += weight
            elif frame.direction != "neutral":
                opposed_weight += weight
        alignment = (aligned_weight / total_weight * 100.0) if total_weight else 0.0
        opposition = (opposed_weight / total_weight * 100.0) if total_weight else 0.0
        uncertainty = max(0.0, min(1.0, 1.0 - (available / len(self.RESPONSIBILITIES))))

        five = next((row for row in frames if row.timeframe == "5minute"), None)
        thirty = next((row for row in frames if row.timeframe == "30minute"), None)
        if five and five.strength >= 0.55 and five.direction != "neutral":
            regime = "trend_expansion" if five.atr_pct >= self._median_atr(frames) else "directional_acceptance"
        elif five and five.strength <= 0.22:
            regime = "compression_range"
        elif thirty and five and thirty.direction != five.direction and "neutral" not in {thirty.direction, five.direction}:
            regime = "transition"
        else:
            regime = "balanced_rotation" if available else "unknown"

        passed = available >= max(1, settings.mtf_min_timeframes) and alignment >= settings.mtf_min_alignment_score
        reasons: list[str] = []
        if available < settings.mtf_min_timeframes:
            reasons.append("mtf_context_insufficient_completed_candles")
        if available and alignment < settings.mtf_min_alignment_score:
            reasons.append("mtf_directional_alignment_is_weak")
        if opposition >= 50:
            reasons.append("higher_and_lower_timeframes_conflict")
        reasons.extend(errors)
        return {
            "passed": passed,
            "score": round(alignment),
            "alignment_score": round(alignment, 2),
            "opposition_score": round(opposition, 2),
            "regime": regime,
            "desired_direction": desired,
            "available_timeframes": available,
            "uncertainty": round(uncertainty, 3),
            "reasons": reasons,
            "frames": [frame.__dict__ for frame in frames],
            "responsibilities": {**self.RESPONSIBILITIES, "tick": "execution_and_fill_confirmation"},
            "hard_block": False,
        }

    def _load(self, symbol: str, aliases: tuple[str, ...], limit: int) -> list[Candle]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol.in_(self._symbols(symbol)), Candle.timeframe.in_(aliases), Candle.is_generated == 0)
                .order_by(Candle.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()

    def _symbols(self, symbol: str) -> tuple[str, ...]:
        value = symbol.upper()
        if value == "BANKNIFTY":
            return ("BANKNIFTY", "NIFTY BANK")
        return (value,)

    def _frame(self, timeframe: str, responsibility: str, candles: list[Candle]) -> _Frame:
        closes = [float(row.close_price) for row in candles]
        highs = [float(row.high_price) for row in candles]
        lows = [float(row.low_price) for row in candles]
        fast = sum(closes[-5:]) / 5
        slow_window = closes[-20:] if len(closes) >= 20 else closes
        slow = sum(slow_window) / len(slow_window)
        lookback = min(10, len(closes) - 1)
        change = (closes[-1] - closes[-1 - lookback]) / max(closes[-1 - lookback], 0.01)
        spread = abs(fast - slow) / max(slow, 0.01)
        strength = min(1.0, abs(change) * 45 + spread * 65)
        direction = "bullish" if fast > slow and change > 0 else "bearish" if fast < slow and change < 0 else "neutral"
        true_ranges = []
        previous = closes[0]
        for high, low, close in zip(highs[1:], lows[1:], closes[1:]):
            true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
            previous = close
        atr = sum(true_ranges[-14:]) / max(1, len(true_ranges[-14:]))
        atr_pct = (atr / max(closes[-1], 0.01)) * 100
        window_high = max(highs[-20:])
        window_low = min(lows[-20:])
        position = (closes[-1] - window_low) / max(window_high - window_low, 0.01)
        score = round(50 + (50 * strength if direction == "bullish" else -50 * strength if direction == "bearish" else 0))
        return _Frame(timeframe, responsibility, direction, score, round(strength, 3), round(atr_pct, 3), round(position, 3), len(candles))

    def _median_atr(self, frames: list[_Frame]) -> float:
        values = sorted(frame.atr_pct for frame in frames if frame.atr_pct > 0)
        return values[len(values) // 2] if values else 0.0
