from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.config import settings
from app.services.time_utils import ist_now_naive
from app.services.trade_setup_service import OptionContract


class EntryTimingService:
    """Classify whether a valid Bank Nifty option setup is early, actionable, or late."""

    WATCHING_SETUP = "WATCHING_SETUP"
    ARMED_FOR_ENTRY = "ARMED_FOR_ENTRY"
    ENTER_NOW = "ENTER_NOW"
    TOO_LATE = "TOO_LATE"
    NO_TRADE = "NO_TRADE"

    def evaluate(
        self,
        *,
        contract: OptionContract,
        prices: dict[str, float],
        premium_eval: dict[str, Any],
        data_quality: dict[str, Any],
        freshness: dict[str, Any],
        option_quality: dict[str, Any],
        banknifty_eval: dict[str, Any],
        price_action: dict[str, Any],
        liquidity_score: int,
        trend: str,
    ) -> dict[str, Any]:
        details = premium_eval.get("details", {}) if isinstance(premium_eval.get("details"), dict) else {}
        current = self._current_premium(contract, prices, details)
        trigger = self._trigger_price(details)
        base = self._base_price(details, current)
        spread_pct = self._spread_pct(contract, details)
        entry = current
        stop = float(prices.get("stop_loss") or 0.0)
        target = float(prices.get("target_1") or 0.0)
        remaining_rr = self._remaining_risk_reward(entry=entry, stop=stop, target=target)
        target_room_pct = ((target - entry) / max(entry, 0.01)) * 100 if target > 0 and entry > 0 else 0.0
        distance_to_trigger_pct = ((trigger - current) / max(trigger, 0.01)) * 100 if trigger > 0 and current > 0 else None
        trigger_chase_pct = ((current - trigger) / max(trigger, 0.01)) * 100 if trigger > 0 and current > trigger else 0.0
        move_from_base_pct = ((current - base) / max(base, 0.01)) * 100 if base > 0 and current > 0 else 0.0
        room_to_level_pct = self._room_to_level_pct(price_action)
        expected_move_coverage = self._expected_move_coverage(banknifty_eval)
        opening_range_status = self._nested(banknifty_eval, "details", "openingRangeStatus")
        top_bank_alignment = self._nested(banknifty_eval, "details", "topBankAlignment")

        blockers = self._safety_blockers(
            data_quality=data_quality,
            freshness=freshness,
            option_quality=option_quality,
            liquidity_score=liquidity_score,
            spread_pct=spread_pct,
            current=current,
            trigger=trigger,
            target=target,
            stop=stop,
        )
        chase_reasons = self._chase_reasons(
            trigger_chase_pct=trigger_chase_pct,
            move_from_base_pct=move_from_base_pct,
            remaining_rr=remaining_rr,
            target_room_pct=target_room_pct,
            expected_move_coverage=expected_move_coverage,
            room_to_level_pct=room_to_level_pct,
            spread_pct=spread_pct,
        )

        breakout = bool(details.get("breakout")) or (trigger > 0 and current >= trigger)
        near_trigger = (
            distance_to_trigger_pct is not None
            and 0 <= distance_to_trigger_pct <= settings.entry_armed_distance_to_trigger_pct
        )
        setup_forming = self._setup_forming(banknifty_eval, price_action, trend)
        premium_participating = bool(details.get("participation_confirmed")) or float(details.get("premium_change_pct") or 0.0) > 0

        if blockers:
            state = self.NO_TRADE
            reasons = blockers
        elif chase_reasons:
            state = self.TOO_LATE
            reasons = chase_reasons
        elif breakout:
            state = self.ENTER_NOW
            reasons = ["premium_breakout_confirmed"]
        elif setup_forming and near_trigger and premium_participating:
            state = self.ARMED_FOR_ENTRY
            reasons = ["waiting_for_entry_trigger", "premium_trigger_not_broken_yet"]
        elif setup_forming:
            state = self.WATCHING_SETUP
            reasons = ["setup_forming_but_premium_not_near_trigger"]
        else:
            state = self.NO_TRADE
            reasons = ["entry_setup_not_valid"]

        should_reject_as_late = state == self.TOO_LATE
        should_wait = state in {self.WATCHING_SETUP, self.ARMED_FOR_ENTRY}
        valid_seconds = max(settings.fast_exit_interval_seconds * 3, 10)
        return {
            "enabled": True,
            "state": state,
            "entry_timing_state": state,
            "passed": state == self.ENTER_NOW,
            "reasons": list(dict.fromkeys(reasons)),
            "entry_timing_reason": "; ".join(dict.fromkeys(reasons)),
            "entry_trigger_price": round(trigger, 2) if trigger > 0 else None,
            "current_premium": round(current, 2) if current > 0 else None,
            "premium_distance_to_trigger_pct": round(distance_to_trigger_pct, 3) if distance_to_trigger_pct is not None else None,
            "premium_move_from_base_pct": round(move_from_base_pct, 3),
            "chase_risk": "high" if should_reject_as_late else "normal",
            "remaining_risk_reward": round(remaining_rr, 3),
            "target1_room_pct": round(target_room_pct, 3),
            "entry_valid_until": (ist_now_naive() + timedelta(seconds=valid_seconds)).isoformat(sep=" "),
            "entry_should_wait": should_wait,
            "entry_should_reject_as_late": should_reject_as_late,
            "breakout": breakout,
            "setup_forming": setup_forming,
            "opening_range_status": opening_range_status,
            "top_bank_alignment": top_bank_alignment,
            "expected_move_coverage": expected_move_coverage,
            "room_to_level_pct": room_to_level_pct,
            "spread_pct": round(spread_pct, 3),
        }

    def _current_premium(self, contract: OptionContract, prices: dict[str, float], details: dict[str, Any]) -> float:
        for value in (contract.ask, contract.last_price, prices.get("entry_price"), details.get("last_close"), details.get("last_price")):
            try:
                current = float(value or 0.0)
                if current > 0:
                    return current
            except (TypeError, ValueError):
                continue
        return 0.0

    def _trigger_price(self, details: dict[str, Any]) -> float:
        candles = details.get("candles")
        for key in ("recent_high", "trigger_price", "last_close", "last_price"):
            try:
                value = float(details.get(key) or 0.0)
                if value > 0 and (key != "last_close" or not candles):
                    return value
            except (TypeError, ValueError):
                continue
        return 0.0

    def _base_price(self, details: dict[str, Any], current: float) -> float:
        candidates: list[float] = []
        for key in ("first_close", "option_vwap", "recent_low", "first_price"):
            try:
                value = float(details.get(key) or 0.0)
                if value > 0:
                    candidates.append(value)
            except (TypeError, ValueError):
                continue
        return min(candidates) if candidates else current

    def _spread_pct(self, contract: OptionContract, details: dict[str, Any]) -> float:
        try:
            detail_spread = float(details.get("spread_pct") or 0.0)
            if detail_spread > 0:
                return detail_spread
        except (TypeError, ValueError):
            pass
        if contract.bid > 0 and contract.ask > 0 and contract.last_price > 0:
            return ((contract.ask - contract.bid) / max(contract.last_price, 0.01)) * 100
        return 100.0

    def _remaining_risk_reward(self, *, entry: float, stop: float, target: float) -> float:
        risk = entry - stop
        reward = target - entry
        if risk <= 0 or reward <= 0:
            return 0.0
        return reward / risk

    def _safety_blockers(
        self,
        *,
        data_quality: dict[str, Any],
        freshness: dict[str, Any],
        option_quality: dict[str, Any],
        liquidity_score: int,
        spread_pct: float,
        current: float,
        trigger: float,
        target: float,
        stop: float,
    ) -> list[str]:
        reasons: list[str] = []
        if not data_quality.get("passed", False):
            reasons.append("entry_data_quality_failed")
        if not freshness.get("passed", True):
            reasons.append("entry_data_freshness_failed")
        if option_quality and not option_quality.get("passed", True):
            reasons.append("entry_option_quality_failed")
        if liquidity_score < settings.min_option_liquidity_score:
            reasons.append("entry_liquidity_failed")
        if spread_pct > settings.max_bid_ask_spread_pct:
            reasons.append("entry_spread_too_wide")
        if current <= 0 or trigger <= 0 or target <= 0 or stop <= 0:
            reasons.append("entry_timing_price_inputs_missing")
        return reasons

    def _chase_reasons(
        self,
        *,
        trigger_chase_pct: float,
        move_from_base_pct: float,
        remaining_rr: float,
        target_room_pct: float,
        expected_move_coverage: float | None,
        room_to_level_pct: float | None,
        spread_pct: float,
    ) -> list[str]:
        reasons: list[str] = []
        if trigger_chase_pct > settings.max_entry_chase_pct:
            reasons.extend(["entry_too_late", "chase_risk_high"])
        if move_from_base_pct > settings.max_premium_move_from_base_pct:
            reasons.extend(["entry_too_late", "chase_risk_high"])
        if remaining_rr < settings.min_remaining_risk_reward:
            reasons.append("reward_compressed")
        if target_room_pct < settings.min_target1_room_pct:
            reasons.append("insufficient_target_room_after_entry")
        if expected_move_coverage is not None and expected_move_coverage < settings.min_entry_expected_move_coverage:
            reasons.append("expected_move_coverage_weak")
        if room_to_level_pct is not None and room_to_level_pct < settings.min_entry_room_to_level_pct:
            reasons.append("nearest_level_room_too_small")
        if spread_pct > settings.max_bid_ask_spread_pct:
            reasons.append("spread_widened_after_breakout")
        return list(dict.fromkeys(reasons))

    def _setup_forming(self, banknifty_eval: dict[str, Any], price_action: dict[str, Any], trend: str) -> bool:
        bank_score = int(banknifty_eval.get("score") or 0)
        price_score = int(price_action.get("score") or 0)
        return bool(trend) and bank_score >= 50 and price_score >= 50

    def _expected_move_coverage(self, banknifty_eval: dict[str, Any]) -> float | None:
        value = self._nested(banknifty_eval, "details", "expectedMoveCheck")
        if isinstance(value, dict):
            for key in ("coverage", "coverageRatio", "expectedMoveCoverage"):
                try:
                    parsed = float(value.get(key) or 0.0)
                    if parsed > 0:
                        return parsed
                except (TypeError, ValueError):
                    continue
        return None

    def _room_to_level_pct(self, price_action: dict[str, Any]) -> float | None:
        details = price_action.get("details", {}) if isinstance(price_action.get("details"), dict) else {}
        try:
            value = float(details.get("room_to_level_pct") or 0.0)
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

    def _nested(self, value: dict[str, Any], *keys: str) -> Any:
        current: Any = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current
