from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.config import settings
from app.services.entry_opportunity_service import EntryOpportunityService
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
        details = (
            premium_eval.get("details", {})
            if isinstance(premium_eval.get("details"), dict)
            else {}
        )
        current = self._current_premium(contract, prices, details)
        trigger = self._trigger_price(details)
        base = self._base_price(details, current)
        spread_pct = self._spread_pct(contract, details)
        entry = current
        stop = float(prices.get("stop_loss") or 0.0)
        target = float(prices.get("target_1") or 0.0)
        remaining_rr = self._remaining_risk_reward(
            entry=entry, stop=stop, target=target
        )
        target_room_pct = (
            ((target - entry) / max(entry, 0.01)) * 100
            if target > 0 and entry > 0
            else 0.0
        )
        distance_to_trigger_pct = (
            ((trigger - current) / max(trigger, 0.01)) * 100
            if trigger > 0 and current > 0
            else None
        )
        move_from_base_pct = (
            ((current - base) / max(base, 0.01)) * 100
            if base > 0 and current > 0
            else 0.0
        )
        room_to_level_pct = self._room_to_level_pct(price_action)
        expected_move_coverage = self._expected_move_coverage(banknifty_eval)
        opening_range_status = self._nested(
            banknifty_eval, "details", "openingRangeStatus"
        )
        top_bank_alignment = self._nested(banknifty_eval, "details", "topBankAlignment")

        blockers = self._safety_blockers(
            data_quality=data_quality,
            freshness=freshness,
            premium_eval=premium_eval,
            spread_pct=spread_pct,
            current=current,
            trigger=trigger,
            target=target,
            stop=stop,
        )
        breakout = bool(details.get("breakout")) or (trigger > 0 and current >= trigger)
        opening_state = (
            str(opening_range_status.get("status") or "")
            if isinstance(opening_range_status, dict)
            else ""
        )
        price_details = (
            price_action.get("details", {})
            if isinstance(price_action.get("details"), dict)
            else {}
        )
        breakout_accepted = breakout and (
            bool(details.get("breakout_accepted"))
            or bool(price_details.get("breakout_accepted"))
            or opening_state in {"breakout", "breakdown"}
        )
        observations = [
            float(value)
            for value in (details.get("recent_closes") or [])
            if self._positive_number(value)
        ]
        opportunity = EntryOpportunityService().evaluate(
            current=current,
            trigger=trigger,
            base=base,
            stop=stop,
            target=target,
            spread_pct=spread_pct,
            observations=observations,
            volatility_scale=self._float(details.get("premium_atr")),
            expected_move_coverage=expected_move_coverage,
            room_to_level_pct=room_to_level_pct,
            breakout_accepted=breakout_accepted,
        )
        chase_reasons = list(opportunity["blockers"])

        near_trigger = (
            distance_to_trigger_pct is not None
            and 0
            <= distance_to_trigger_pct
            <= settings.entry_armed_distance_to_trigger_pct
        )
        setup_forming = self._setup_forming(banknifty_eval, price_action, trend)
        premium_participating = (
            bool(details.get("participation_confirmed"))
            or float(details.get("premium_change_pct") or 0.0) > 0
        )

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
            "premium_distance_to_trigger_pct": round(distance_to_trigger_pct, 3)
            if distance_to_trigger_pct is not None
            else None,
            "premium_move_from_base_pct": round(move_from_base_pct, 3),
            "chase_risk": "high" if should_reject_as_late else "normal",
            "remaining_risk_reward": round(remaining_rr, 3),
            "target1_room_pct": round(target_room_pct, 3),
            "entry_valid_until": (
                ist_now_naive() + timedelta(seconds=valid_seconds)
            ).isoformat(sep=" "),
            "entry_should_wait": should_wait,
            "entry_should_reject_as_late": should_reject_as_late,
            "breakout": breakout,
            "setup_forming": setup_forming,
            "opening_range_status": opening_range_status,
            "top_bank_alignment": top_bank_alignment,
            "expected_move_coverage": expected_move_coverage,
            "room_to_level_pct": room_to_level_pct,
            "spread_pct": round(spread_pct, 3),
            "breakout_accepted": breakout_accepted,
            "entry_opportunity": opportunity,
            "opportunity_warnings": list(opportunity["warnings"]),
            "soft_confirmation_evidence": {
                "option_quality_score": option_quality.get("score"),
                "option_quality_passed": option_quality.get("passed"),
                "liquidity_score": liquidity_score,
                "premium_confirmation_score": premium_eval.get("score"),
                "premium_confirmation_passed": premium_eval.get("passed"),
            },
        }

    def _current_premium(
        self,
        contract: OptionContract,
        prices: dict[str, float],
        details: dict[str, Any],
    ) -> float:
        for value in (
            contract.ask,
            contract.last_price,
            prices.get("entry_price"),
            details.get("last_close"),
            details.get("last_price"),
        ):
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
            return (
                (contract.ask - contract.bid) / max(contract.last_price, 0.01)
            ) * 100
        return 100.0

    def _remaining_risk_reward(
        self, *, entry: float, stop: float, target: float
    ) -> float:
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
        premium_eval: dict[str, Any],
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
        premium_details = (
            premium_eval.get("details", {})
            if isinstance(premium_eval.get("details"), dict)
            else {}
        )
        premium_reasons = [str(reason) for reason in premium_eval.get("reasons", [])]
        if str(premium_details.get("source") or "") == "unavailable" or any(
            marker in reason
            for reason in premium_reasons
            for marker in ("stale_or_missing", "capture_gap", "queue_gap")
        ):
            reasons.extend(
                reason
                for reason in premium_reasons
                if any(
                    marker in reason
                    for marker in ("stale_or_missing", "capture_gap", "queue_gap")
                )
            )
            if not any("stale_or_missing" in reason for reason in reasons):
                reasons.append("premium_candles_stale_or_missing")
        if spread_pct > settings.max_bid_ask_spread_pct:
            reasons.append("entry_spread_too_wide")
        if current <= 0 or trigger <= 0 or target <= 0 or stop <= 0:
            reasons.append("entry_timing_price_inputs_missing")
        return reasons

    def _setup_forming(
        self, banknifty_eval: dict[str, Any], price_action: dict[str, Any], trend: str
    ) -> bool:
        return str(trend).lower() in {"bullish", "bearish"}

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
        details = (
            price_action.get("details", {})
            if isinstance(price_action.get("details"), dict)
            else {}
        )
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

    def _positive_number(self, value: Any) -> bool:
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
