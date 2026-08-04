from __future__ import annotations

from typing import Any, Sequence

from app.config import settings


class EntryOpportunityService:
    """One normalized executable-opportunity model shared by every entry path."""

    def evaluate(
        self,
        *,
        current: float,
        trigger: float,
        base: float,
        stop: float,
        target: float,
        spread_pct: float,
        observations: Sequence[float] = (),
        volatility_scale: float | None = None,
        expected_move_coverage: float | None = None,
        room_to_level_pct: float | None = None,
        breakout_accepted: bool = False,
    ) -> dict[str, Any]:
        risk = current - stop
        reward = target - current
        remaining_rr = reward / risk if risk > 0 and reward > 0 else 0.0
        target_room_pct = reward / max(current, 0.01) * 100 if reward > 0 else 0.0
        chase_points = max(0.0, current - trigger)
        values = [float(value) for value in observations if float(value) > 0]
        baseline_values = values
        if len(values) >= 2 and abs(values[-1] - current) <= max(0.05, current * 0.001):
            baseline_values = values[:-1]
        observed_range = (
            max(baseline_values) - min(baseline_values)
            if len(baseline_values) >= 2
            else 0.0
        )
        spread_scale = trigger * max(spread_pct, 0.05) / 100.0
        scale = max(
            float(volatility_scale or 0.0),
            observed_range,
            spread_scale * 3.0,
            trigger * 0.002,
            0.15,
        )
        normalized_chase = chase_points / scale
        normalized_remaining = reward / scale if reward > 0 else 0.0
        move_from_base_scales = max(0.0, current - base) / scale

        blockers: list[str] = []
        warnings: list[str] = []
        if min(current, trigger, stop, target) <= 0 or not (stop < current < target):
            blockers.append("entry_opportunity_price_plan_invalid")
        if spread_pct > settings.max_bid_ask_spread_pct:
            blockers.append("spread_widened_after_trigger")
        if normalized_chase > settings.normalized_entry_chase_max_atr:
            blockers.extend(
                ["entry_too_late", "chase_risk_high", "normalized_chase_risk_high"]
            )
        if remaining_rr < settings.min_remaining_risk_reward:
            blockers.append("remaining_rr_compressed")
            if current > trigger:
                blockers.extend(["entry_too_late", "chase_risk_high"])
        if target_room_pct < settings.min_target1_room_pct:
            blockers.append("insufficient_target_room_after_entry")

        # Context estimates are uncertain ranking evidence. Actual executable
        # reward/risk and chase distance remain the controlling hard tests.
        if (
            expected_move_coverage is not None
            and expected_move_coverage < settings.min_entry_expected_move_coverage
        ):
            warnings.append("expected_move_coverage_weak")
        if (
            room_to_level_pct is not None
            and room_to_level_pct < settings.min_entry_room_to_level_pct
        ):
            warnings.append("nearest_level_room_too_small")

        return {
            "passed": not blockers,
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": list(dict.fromkeys(warnings)),
            "normalized_chase": round(normalized_chase, 4),
            "normalized_remaining_opportunity": round(normalized_remaining, 4),
            "normalized_move_from_base": round(move_from_base_scales, 4),
            "opportunity_scale": round(scale, 4),
            "remaining_risk_reward": round(remaining_rr, 4),
            "target_room_pct": round(target_room_pct, 4),
            "breakout_accepted": bool(breakout_accepted),
        }
