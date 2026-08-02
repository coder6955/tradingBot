from __future__ import annotations

from typing import Any

from app.config import settings


class MomentumPhaseService:
    """Classify the lifecycle of a directional move without treating momentum as one boolean."""

    PHASES = (
        "formation",
        "acceleration",
        "breakout",
        "confirmation",
        "continuation",
        "exhaustion",
        "failure",
    )

    def evaluate(
        self,
        *,
        trend: str,
        snapshot: dict[str, Any],
        premium_eval: dict[str, Any],
        price_action: dict[str, Any],
        banknifty_eval: dict[str, Any],
        multi_timeframe: dict[str, Any],
        volatility_eval: dict[str, Any],
    ) -> dict[str, Any]:
        bullish = trend.lower() == "bullish"
        rsi = self._float(snapshot.get("rsi"), 50.0)
        adx = self._float(snapshot.get("adx"), 0.0)
        volume = bool(snapshot.get("volume_confirmed"))
        premium = self._dict(premium_eval)
        premium_details = self._dict(premium.get("details"))
        breakout = bool(premium_details.get("breakout"))
        premium_volume = bool(
            premium_details.get("volume_expansion")
            or premium_details.get("participation_confirmed")
        )
        premium_change = self._float(premium_details.get("premium_change_pct"), 0.0)
        price_details = self._dict(price_action.get("details"))
        hard_block = bool(price_details.get("hard_block"))
        mtf_score = self._float(multi_timeframe.get("alignment_score"), 0.0)
        mtf_regime = str(multi_timeframe.get("regime") or "unknown")
        bank_details = self._dict(banknifty_eval.get("details"))
        opening = self._dict(bank_details.get("openingRangeStatus"))
        opening_state = str(opening.get("status") or "unknown")
        directional_open = opening_state == ("breakout" if bullish else "breakdown")
        failed_open = opening_state in {"failed_breakout", "failed_breakdown"}
        vol_class = str(volatility_eval.get("classification") or "unknown")

        failure = hard_block or failed_open or premium_change < -2.0
        exhausted = (
            (bullish and rsi >= settings.momentum_exhaustion_rsi)
            or ((not bullish) and rsi <= 100 - settings.momentum_exhaustion_rsi)
            or str(volatility_eval.get("main_risk") or "") == "iv_crush"
        ) and breakout
        if failure:
            phase = "failure"
        elif exhausted:
            phase = "exhaustion"
        elif breakout and premium_volume and mtf_score >= 60:
            phase = "confirmation"
        elif breakout:
            phase = "breakout"
        elif directional_open and adx >= 24 and volume:
            phase = "acceleration"
        elif mtf_regime in {"trend_expansion", "directional_acceptance"} and adx >= 20:
            phase = "continuation"
        else:
            phase = "formation"

        score = 35
        score += 15 if adx >= 20 else 0
        score += 10 if volume else 0
        score += 15 if premium_volume else 0
        score += 15 if breakout else 0
        score += round(max(-15.0, min(15.0, (mtf_score - 50) * 0.3)))
        if vol_class in {"expansion_supported", "cheap", "fair"}:
            score += 5
        if phase == "exhaustion":
            score -= 30
        elif phase == "failure":
            score -= 45
        score = max(0, min(100, score))
        actionable = (
            phase in {"acceleration", "breakout", "confirmation", "continuation"}
            and score >= settings.momentum_min_entry_score
        )
        abstention = (
            "MOMENTUM_EXHAUSTED"
            if phase == "exhaustion"
            else "MOMENTUM_FAILED"
            if phase == "failure"
            else None
        )
        invalidation = [
            "underlying_loses_directional_vwap_acceptance",
            "premium_loses_breakout_level_and_option_vwap",
            "bid_depth_or_spread_quality_deteriorates",
        ]
        reasons = [f"momentum_phase_{phase}"]
        if not actionable and phase == "formation":
            reasons.append("momentum_still_forming")
        if exhausted:
            reasons.append("move_is_extended_or_iv_crush_risk_is_high")
        if failure:
            reasons.append("directional_structure_or_premium_participation_failed")
        return {
            "phase": phase,
            "score": score,
            "passed": actionable,
            "suitable_for_entry": actionable,
            "abstention_code": abstention,
            "reasons": reasons,
            "invalidation": invalidation,
            "details": {
                "rsi": rsi,
                "adx": adx,
                "underlying_volume_confirmed": volume,
                "premium_breakout": breakout,
                "premium_volume_expansion": premium_volume,
                "premium_change_pct": premium_change,
                "mtf_alignment_score": mtf_score,
                "opening_state": opening_state,
                "volatility_classification": vol_class,
            },
        }

    def _dict(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value if value is not None else default)
        except (TypeError, ValueError):
            return default
