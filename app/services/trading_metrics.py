from __future__ import annotations

from datetime import datetime
from typing import Any, Callable


WIN_OUTCOMES = {"target", "target_1", "target_2", "target_3", "winner", "trailing_stop"}
LOSS_OUTCOMES = {"stop_loss", "false_signal", "loser"}


def summarize_returns(
    rows: list[Any],
    *,
    pnl_fn: Callable[[Any], float],
    outcome_fn: Callable[[Any], str | None],
    time_in_trade_fn: Callable[[Any], float | None] | None = None,
) -> dict[str, Any]:
    if not rows:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "open_or_unclassified": 0,
            "win_rate_pct": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
            "expectancy": 0.0,
            "profit_factor": None,
            "max_drawdown": 0.0,
            "avg_time_in_trade_minutes": 0.0,
            "total_pnl": 0.0,
        }

    wins: list[float] = []
    losses: list[float] = []
    returns: list[float] = []
    times: list[float] = []
    unclassified = 0
    for row in rows:
        pnl = pnl_fn(row)
        outcome = str(outcome_fn(row) or "").lower()
        if outcome in WIN_OUTCOMES or pnl > 0:
            wins.append(pnl)
        elif outcome in LOSS_OUTCOMES or pnl < 0:
            losses.append(pnl)
        else:
            unclassified += 1
        returns.append(pnl)
        if time_in_trade_fn:
            minutes = time_in_trade_fn(row)
            if minutes is not None:
                times.append(minutes)

    gross_win = sum(value for value in wins if value > 0)
    gross_loss = abs(sum(value for value in losses if value < 0))
    closed_count = len(wins) + len(losses)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "open_or_unclassified": unclassified,
        "win_rate_pct": round((len(wins) / closed_count) * 100, 2) if closed_count else 0.0,
        "average_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "average_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round((sum(wins) + sum(losses)) / closed_count, 2) if closed_count else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown": round(max_drawdown(returns), 2),
        "avg_time_in_trade_minutes": round(sum(times) / len(times), 2) if times else 0.0,
        "total_pnl": round(sum(returns), 2),
    }


def summarize_groups(
    rows: list[Any],
    key_fn: Callable[[Any], str],
    *,
    pnl_fn: Callable[[Any], float],
    outcome_fn: Callable[[Any], str | None],
    time_in_trade_fn: Callable[[Any], float | None] | None = None,
) -> dict[str, Any]:
    groups: dict[str, list[Any]] = {}
    for row in rows:
        groups.setdefault(key_fn(row), []).append(row)
    return {
        key: summarize_returns(items, pnl_fn=pnl_fn, outcome_fn=outcome_fn, time_in_trade_fn=time_in_trade_fn)
        for key, items in sorted(groups.items())
    }


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return abs(drawdown)


def minutes_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 60)


def time_bucket(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    total = value.hour * 60 + value.minute
    if total < 10 * 60:
        return "open_early"
    if total < 11 * 60 + 30:
        return "morning"
    if total < 13 * 60 + 30:
        return "midday"
    if total < 14 * 60 + 45:
        return "afternoon"
    return "closing"


def score_bucket(score: int | float | None) -> str:
    value = float(score or 0)
    if value >= 90:
        return "score_90_plus"
    if value >= 80:
        return "score_80_89"
    if value >= 70:
        return "score_70_79"
    return "score_below_70"
