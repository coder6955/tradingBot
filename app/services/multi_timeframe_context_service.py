from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.services.database import Candle, get_session
from app.services.completed_structure_service import classify_completed_structure
from app.services.time_utils import ist_now_naive


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
    last_completed_at: str
    last_open: float
    last_high: float
    last_low: float
    last_close: float
    structure_phase: str
    breakout_accepted: bool


class MultiTimeframeContextService:
    """Assign one responsibility to each timeframe and report alignment.

    This service is deliberately read-only. Both completed 1-minute and
    completed 5-minute evidence are required; missing data never manufactures
    confirmation.
    """

    RESPONSIBILITIES = {
        "5minute": "setup_direction_regime_day_structure_and_completed_confirmation",
        "1minute": "entry_timing_and_fast_confirmation",
    }
    ALIASES = {
        "5minute": ("5minute", "5min"),
        "1minute": ("1minute", "1min"),
    }

    def load_completed_candles(self, symbol: str, *, as_of: datetime | None = None) -> dict[str, list[Candle]]:
        """Load both active timeframes in one bounded database query."""
        return self._load_bulk(symbol, as_of=as_of)

    def evaluate(
        self,
        *,
        symbol: str,
        trend: str,
        snapshot: dict[str, Any] | None = None,
        candle_sets: dict[str, list[Candle]] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        desired = "bullish" if trend.lower() == "bullish" else "bearish"
        frames: list[_Frame] = []
        errors: list[str] = []
        loaded = candle_sets if candle_sets is not None else self._load_bulk(symbol, as_of=as_of)
        evaluation_at = (as_of or ist_now_naive()).replace(tzinfo=None)
        opening_session = self._is_opening_session(evaluation_at)
        for timeframe, responsibility in self.RESPONSIBILITIES.items():
            try:
                candles = list(loaded.get(timeframe, []))[-80:]
            except Exception as exc:  # database loss must be observable but must not crash scanning
                errors.append(f"{timeframe}_load_failed:{type(exc).__name__}")
                candles = []
            required = self._required_candles(timeframe, opening_session)
            if len(candles) >= required:
                frames.append(self._frame(timeframe, responsibility, candles, min_candles=required, opening_session=opening_session))

        available = len(frames)
        aligned_weight = 0.0
        opposed_weight = 0.0
        total_weight = 0.0
        weights = {"5minute": 1.4, "1minute": 1.0}
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
        if five and five.strength >= 0.55 and five.direction != "neutral":
            regime = "trend_expansion" if five.atr_pct >= self._median_atr(frames) else "directional_acceptance"
        elif five and five.strength <= 0.22:
            regime = "compression_range"
        elif available == 2 and len({row.direction for row in frames if row.direction != "neutral"}) > 1:
            regime = "transition"
        else:
            regime = "balanced_rotation" if available else "unknown"

        passed = available == 2 and all(frame.direction == desired for frame in frames)
        reasons: list[str] = []
        if available < 2:
            reasons.append("one_minute_or_five_minute_completed_candles_missing")
        if available == 2 and not passed:
            reasons.append("one_minute_and_five_minute_direction_disagree")
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
            "timeframes_used": ["1minute", "5minute"],
            "hard_block": False,
            "opening_session": opening_session,
            "readiness_policy": {
                "1minute_required": self._required_candles("1minute", opening_session),
                "5minute_required": self._required_candles("5minute", opening_session),
            },
        }

    def _load_bulk(self, symbol: str, *, as_of: datetime | None = None) -> dict[str, list[Candle]]:
        cutoff = (as_of or ist_now_naive()).replace(tzinfo=None)
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(
                    Candle.symbol.in_(self._symbols(symbol)),
                    Candle.timeframe.in_(("1minute", "5minute")),
                    Candle.is_generated == 0,
                    Candle.timestamp >= cutoff - timedelta(days=10),
                    Candle.timestamp <= cutoff,
                )
                .order_by(Candle.timestamp.asc())
                .all()
            )
            grouped: dict[str, list[Candle]] = {"1minute": [], "5minute": []}
            for row in rows:
                timeframe = str(row.timeframe)
                completion = row.timestamp.replace(tzinfo=None) + timedelta(minutes=1 if timeframe == "1minute" else 5)
                if timeframe in grouped and completion <= cutoff:
                    grouped[timeframe].append(row)
            completed_rows = [row for values in grouped.values() for row in values]
            if not completed_rows:
                return grouped
            latest_session = max(row.timestamp.replace(tzinfo=None).date() for row in completed_rows)
            return {
                name: [row for row in values if row.timestamp.replace(tzinfo=None).date() == latest_session][-80:]
                for name, values in grouped.items()
            }
        finally:
            session.close()

    def _symbols(self, symbol: str) -> tuple[str, ...]:
        value = symbol.upper()
        if value == "BANKNIFTY":
            return ("BANKNIFTY", "NIFTY BANK")
        return (value,)

    def _frame(
        self,
        timeframe: str,
        responsibility: str,
        candles: list[Candle],
        *,
        min_candles: int,
        opening_session: bool,
    ) -> _Frame:
        opens = [float(row.open_price) for row in candles]
        closes = [float(row.close_price) for row in candles]
        highs = [float(row.high_price) for row in candles]
        lows = [float(row.low_price) for row in candles]
        volumes = [float(row.volume or 0.0) for row in candles]
        structure = classify_completed_structure(
            closes,
            opens=opens,
            highs=highs,
            lows=lows,
            volumes=volumes,
            min_candles=min_candles,
            opening_session=opening_session,
        )
        strength = float(structure["strength"])
        direction = str(structure["direction"])
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
        last = candles[-1]
        timestamp = last.timestamp.replace(tzinfo=None) if isinstance(last.timestamp, datetime) else datetime.fromisoformat(str(last.timestamp)).replace(tzinfo=None)
        return _Frame(
            timeframe,
            responsibility,
            direction,
            score,
            round(strength, 3),
            round(atr_pct, 3),
            round(position, 3),
            len(candles),
            timestamp.isoformat(sep=" "),
            float(last.open_price),
            float(last.high_price),
            float(last.low_price),
            float(last.close_price),
            str(structure.get("phase") or "balance"),
            bool(structure.get("breakout_accepted")),
        )

    def _required_candles(self, timeframe: str, opening_session: bool) -> int:
        if opening_session:
            if timeframe == "1minute":
                return max(3, int(settings.opening_structure_min_1m_candles))
            return max(3, int(settings.opening_structure_min_5m_candles))
        return max(6, int(settings.structure_min_completed_candles))

    def _is_opening_session(self, value: datetime) -> bool:
        try:
            hour, minute = (int(part) for part in settings.opening_structure_end_time.split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 9, 45
        session_open = value.replace(hour=9, minute=15, second=0, microsecond=0).time()
        opening_end = value.replace(hour=hour, minute=minute, second=0, microsecond=0).time()
        return session_open <= value.time() < opening_end

    def _median_atr(self, frames: list[_Frame]) -> float:
        values = sorted(frame.atr_pct for frame in frames if frame.atr_pct > 0)
        return values[len(values) // 2] if values else 0.0
