from __future__ import annotations

from typing import List, Sequence, Tuple


def compute_ema(values: Sequence[float], period: int) -> List[float]:
    """Compute an exponential moving average for the provided values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    multiplier = 2.0 / (period + 1)
    ema_values: List[float] = []
    previous_ema: float | None = None
    for value in values:
        if previous_ema is None:
            previous_ema = float(value)
        else:
            previous_ema = (float(value) * multiplier) + (previous_ema * (1 - multiplier))
        ema_values.append(previous_ema)
    return ema_values


def compute_rsi(values: Sequence[float], period: int = 14) -> List[float]:
    """Compute the Relative Strength Index for the provided values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    changes = [float(values[idx]) - float(values[idx - 1]) for idx in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [abs(min(change, 0.0)) for change in changes]

    rsi_values = [50.0] * len(values)
    if len(values) <= period:
        return rsi_values

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        rsi_values[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_values[period] = 100 - (100 / (1 + rs))

    for idx in range(period + 1, len(values)):
        avg_gain = ((avg_gain * (period - 1)) + gains[idx - 1]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[idx - 1]) / period
        if avg_loss == 0:
            rsi_values[idx] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_values[idx] = 100 - (100 / (1 + rs))

    return rsi_values


def compute_macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[List[float], List[float]]:
    """Compute MACD line and signal line."""
    ema_fast = compute_ema(values, fast)
    ema_slow = compute_ema(values, slow)
    macd = [fast_val - slow_val for fast_val, slow_val in zip(ema_fast, ema_slow)]
    signal_line = compute_ema(macd, signal)
    return macd, signal_line


def compute_bollinger_bands(values: Sequence[float], period: int = 20) -> Tuple[List[float], List[float], List[float]]:
    """Compute Bollinger Bands."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return [], [], []

    upper: List[float] = []
    middle: List[float] = []
    lower: List[float] = []

    for idx in range(len(values)):
        window = values[max(0, idx - period + 1): idx + 1]
        if not window:
            continue
        mean = sum(window) / len(window)
        variance = sum((item - mean) ** 2 for item in window) / len(window)
        std_dev = variance ** 0.5
        upper.append(mean + (2 * std_dev))
        middle.append(mean)
        lower.append(mean - (2 * std_dev))

    return upper, middle, lower


def compute_supertrend(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 7, multiplier: float = 3.0) -> List[float]:
    """Compute a simplistic SuperTrend series using ATR-like bands."""
    if not highs or not lows or not closes:
        return []
    if period <= 0:
        raise ValueError("period must be positive")

    values: List[float] = []
    for idx in range(len(closes)):
        hl = float(highs[idx]) - float(lows[idx])
        prev_close = float(closes[idx - 1]) if idx > 0 else float(closes[idx])
        value = (hl + abs(float(highs[idx]) - prev_close) + abs(float(lows[idx]) - prev_close)) / 3.0
        values.append(value * multiplier)
    return values
