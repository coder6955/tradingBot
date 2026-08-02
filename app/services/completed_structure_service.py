from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def classify_completed_structure(
    closes: Sequence[float],
    *,
    opens: Sequence[float] | None = None,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    volumes: Sequence[float] | None = None,
    min_candles: int = 6,
    opening_session: bool = False,
) -> dict[str, Any]:
    """Classify completed-candle price structure without indicator averages.

    Direction requires evidence distributed across multiple completed candles:
    an impulse with breadth plus either rising/falling swings, a controlled
    pullback, or accepted breakout.  A single large candle is intentionally
    insufficient.
    """

    close_values = [float(value) for value in closes]
    required = max(3 if opening_session else 6, int(min_candles))
    if len(close_values) < required:
        return {
            "ready": False,
            "direction": "neutral",
            "strength": 0.0,
            "available_candles": len(close_values),
            "required_candles": required,
            "reason": "insufficient_completed_candles_for_structure",
            "phase": "opening_discovery" if opening_session else "structure_discovery",
        }

    window_size = min(20, len(close_values))
    close_values = close_values[-window_size:]
    open_values = _aligned(opens, close_values, fallback_previous=True)
    high_values = _aligned(highs, close_values, fallback_close=True)
    low_values = _aligned(lows, close_values, fallback_close=True)
    volume_values = _aligned(volumes, close_values, fallback_zero=True)

    ranges = [
        max(high - low, abs(close - open_price), 0.01)
        for open_price, high, low, close in zip(
            open_values, high_values, low_values, close_values
        )
    ]
    typical_range = _median(ranges[:-1] or ranges)
    moves = [right - left for left, right in zip(close_values[:-1], close_values[1:])]
    lookback = min(8, len(moves))
    recent_moves = moves[-lookback:]
    up_breadth = sum(move > 0 for move in recent_moves) / max(1, lookback)
    down_breadth = sum(move < 0 for move in recent_moves) / max(1, lookback)
    net_points = close_values[-1] - close_values[-1 - lookback]
    impulse_atr = net_points / max(typical_range, 0.01)

    split = max(2, len(close_values) // 2)
    prior_high = max(high_values[:-2] or high_values[:-1])
    prior_low = min(low_values[:-2] or low_values[:-1])
    older_high = max(high_values[:split])
    older_low = min(low_values[:split])
    recent_high = max(high_values[split:])
    recent_low = min(low_values[split:])
    higher_swings = (
        recent_high > older_high and recent_low >= older_low - typical_range * 0.20
    )
    lower_swings = (
        recent_low < older_low and recent_high <= older_high + typical_range * 0.20
    )

    acceptance_buffer = typical_range * 0.08
    bullish_breakout = close_values[-1] > prior_high + acceptance_buffer
    bearish_breakout = close_values[-1] < prior_low - acceptance_buffer
    bullish_acceptance = bullish_breakout and (
        close_values[-2] >= prior_high - typical_range * 0.15 or up_breadth >= 0.60
    )
    bearish_acceptance = bearish_breakout and (
        close_values[-2] <= prior_low + typical_range * 0.15 or down_breadth >= 0.60
    )

    peak_index = close_values.index(max(close_values))
    trough_index = close_values.index(min(close_values))
    bullish_retracement = (
        (max(close_values) - close_values[-1])
        / max(max(close_values) - min(close_values), 0.01)
        if peak_index < len(close_values) - 1
        else 0.0
    )
    bearish_retracement = (
        (close_values[-1] - min(close_values))
        / max(max(close_values) - min(close_values), 0.01)
        if trough_index < len(close_values) - 1
        else 0.0
    )
    bullish_pullback_held = (
        net_points > 0 and bullish_retracement <= 0.45 and close_values[-1] > older_low
    )
    bearish_pullback_held = (
        net_points < 0 and bearish_retracement <= 0.45 and close_values[-1] < older_high
    )

    bullish_impulse = (
        impulse_atr >= (0.75 if opening_session else 1.15) and up_breadth >= 0.55
    )
    bearish_impulse = (
        impulse_atr <= (-0.75 if opening_session else -1.15) and down_breadth >= 0.55
    )
    bullish_votes = sum(
        (bullish_impulse, higher_swings, bullish_pullback_held, bullish_acceptance)
    )
    bearish_votes = sum(
        (bearish_impulse, lower_swings, bearish_pullback_held, bearish_acceptance)
    )
    min_votes = 2
    if (
        bullish_votes >= min_votes
        and bullish_votes > bearish_votes
        and up_breadth >= 0.50
    ):
        direction = "bullish"
        directional_agreement = up_breadth
    elif (
        bearish_votes >= min_votes
        and bearish_votes > bullish_votes
        and down_breadth >= 0.50
    ):
        direction = "bearish"
        directional_agreement = down_breadth
    else:
        direction = "neutral"
        directional_agreement = max(up_breadth, down_breadth)

    impulse_strength = min(1.0, abs(impulse_atr) / 4.0)
    evidence_strength = max(bullish_votes, bearish_votes) / 4.0
    strength = min(1.0, impulse_strength * 0.55 + evidence_strength * 0.45)
    if direction == "neutral":
        strength *= 0.55
    volume_expansion = False
    if len(volume_values) >= 3 and any(volume_values[:-1]):
        baseline = sum(volume_values[:-1]) / max(1, len(volume_values) - 1)
        volume_expansion = volume_values[-1] >= baseline * 1.15

    phase = "balance"
    if bullish_acceptance or bearish_acceptance:
        phase = "breakout_acceptance"
    elif bullish_pullback_held or bearish_pullback_held:
        phase = "pullback_continuation"
    elif bullish_impulse or bearish_impulse:
        phase = "impulse"
    if opening_session:
        phase = f"opening_{phase}"

    return {
        "ready": True,
        "direction": direction,
        "strength": round(strength, 3),
        "phase": phase,
        "opening_session": bool(opening_session),
        "impulse_atr": round(impulse_atr, 3),
        "directional_agreement": round(directional_agreement, 3),
        "swing_structure": "higher_high_higher_low"
        if higher_swings
        else "lower_high_lower_low"
        if lower_swings
        else "mixed",
        "pullback_held": bullish_pullback_held
        if direction == "bullish"
        else bearish_pullback_held
        if direction == "bearish"
        else False,
        "breakout": bullish_breakout
        if direction == "bullish"
        else bearish_breakout
        if direction == "bearish"
        else False,
        "breakout_accepted": bullish_acceptance
        if direction == "bullish"
        else bearish_acceptance
        if direction == "bearish"
        else False,
        "volume_expansion": volume_expansion,
        "bullish_evidence_votes": bullish_votes,
        "bearish_evidence_votes": bearish_votes,
        "available_candles": len(close_values),
        "required_candles": required,
        "reason": None,
    }


def _aligned(
    values: Sequence[float] | None,
    closes: list[float],
    *,
    fallback_previous: bool = False,
    fallback_close: bool = False,
    fallback_zero: bool = False,
) -> list[float]:
    if values is not None and len(values) >= len(closes):
        return [float(value) for value in values][-len(closes) :]
    if fallback_previous:
        return [
            closes[index - 1] if index else closes[0] for index in range(len(closes))
        ]
    if fallback_close:
        return list(closes)
    if fallback_zero:
        return [0.0] * len(closes)
    return list(closes)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.01
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
