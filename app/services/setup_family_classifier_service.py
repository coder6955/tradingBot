from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.trade_setup_service import OptionContract


class SetupFamilyClassifierService:
    """Classify a setup and attach an explicit regime-specific trade policy."""

    def classify(
        self,
        *,
        symbol: str,
        trend: str,
        side: str,
        snapshot: dict[str, Any] | None,
        factor_scores: dict[str, Any],
        contract: OptionContract | None = None,
    ) -> dict[str, Any]:
        if symbol.upper() != "BANKNIFTY" or side.upper() != "BUY":
            return self._result("non_banknifty_or_non_buy_setup", "other", ["outside Bank Nifty option-buying taxonomy"])

        bullish = trend.lower() == "bullish"
        premium = self._dict(factor_scores.get("option_premium_confirmation"))
        premium_details = self._dict(premium.get("details"))
        bank = self._dict(factor_scores.get("banknifty_intelligence"))
        bank_details = self._dict(bank.get("details"))
        regime = self._dict(factor_scores.get("banknifty_regime_filter"))
        regime_details = self._dict(regime.get("details"))
        day_type = self._day_type(factor_scores, bank_details)
        opening = self._dict(bank_details.get("openingRangeStatus"))
        dte = self._dict(bank_details.get("dteMode"))
        vwap = self._vwap_context(snapshot or {}, premium_details, bullish)
        compression = self._dict(regime_details.get("compression_expansion"))
        premium_breakout = bool(premium_details.get("breakout"))
        premium_volume = bool(premium_details.get("volume_expansion"))
        premium_score = int(premium.get("score") or 0)
        evidence = {
            "trend": trend,
            "action": "BUY_CE" if bullish else "BUY_PE",
            "opening_status": opening.get("status"),
            "day_type": day_type,
            "dte_risk": dte.get("risk"),
            "dte_mode": dte.get("mode"),
            "premium_breakout": premium_breakout,
            "premium_volume_expansion": premium_volume,
            "premium_score": premium_score,
            "banknifty_vwap_supports": vwap["banknifty_supports"],
            "premium_vwap_supports": vwap["premium_supports"],
            "contract": contract.tradingsymbol if contract else self._nested(factor_scores, "contract", "tradingsymbol"),
            "market_state": self._nested(factor_scores, "market_regime", "regime"),
            "market_state_confidence": self._nested(factor_scores, "market_regime", "confidence"),
            "momentum_phase": self._nested(factor_scores, "momentum_phase", "phase"),
            "mtf_alignment_score": self._nested(factor_scores, "multi_timeframe", "alignment_score"),
        }

        if dte.get("risk") == "near_expiry" and premium_score >= 75 and (premium_breakout or premium_volume):
            return self._result("expiry_scalp_setup", "expiry", ["near expiry", "strong premium participation"], evidence)

        opening_status = str(opening.get("status") or "")
        if (bullish and opening_status == "breakout") or ((not bullish) and opening_status == "breakdown"):
            return self._result("opening_breakout_continuation", "opening_drive", ["opening range continuation supports direction"], evidence)

        if bullish and opening_status == "failed_breakdown":
            return self._result("reversal_after_failed_breakdown", "reversal", ["failed downside auction", "bullish reversal context"], evidence)
        if (not bullish) and opening_status == "failed_breakout":
            return self._result("reversal_after_failed_breakout", "reversal", ["failed upside auction", "bearish reversal context"], evidence)

        compressed = str(compression.get("day_type") or day_type) in {"rotation_range", "range"}
        if compressed and premium_breakout and premium_volume:
            return self._result("range_breakout_after_compression", "compression_expansion", ["range/compression expanding with premium participation"], evidence)

        if vwap["banknifty_supports"] and vwap["premium_supports"]:
            if bullish:
                return self._result("vwap_reclaim_continuation", "vwap_continuation", ["Bank Nifty and option premium are above VWAP"], evidence)
            return self._result("vwap_rejection_continuation", "vwap_continuation", ["Bank Nifty is below VWAP and option premium supports PE"], evidence)

        if day_type in {"trend_expansion", "directional_acceptance"}:
            return self._result("trend_day_addon_entry", "trend_day", ["directional/trend day context"], evidence)

        label = "normal_day_call_continuation" if bullish else "normal_day_put_continuation"
        return self._result(label, "normal_day", ["valid directional setup without a stronger specialized family"], evidence)

    def _result(self, name: str, group: str, reasons: list[str], evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        policy = self._policy(name, group, evidence or {})
        return {
            "name": name,
            "group": group,
            "reasons": reasons,
            "evidence": evidence or {},
            "eligible": policy["eligible"],
            "policy_score": policy["policy_score"],
            "score_adjustment": policy["score_adjustment"],
            "minimum_score": policy["minimum_score"],
            "abstention_code": policy["abstention_code"],
            "entry_policy": policy["entry_policy"],
            "exit_profile": policy["exit_profile"],
            "invalidation": policy["invalidation"],
            "taxonomy": [
                "opening_breakout_continuation",
                "vwap_reclaim_continuation",
                "vwap_rejection_continuation",
                "reversal_after_failed_breakdown",
                "reversal_after_failed_breakout",
                "range_breakout_after_compression",
                "trend_day_addon_entry",
                "expiry_scalp_setup",
                "normal_day_call_continuation",
                "normal_day_put_continuation",
            ],
        }

    def _policy(self, name: str, group: str, evidence: dict[str, Any]) -> dict[str, Any]:
        market_state = str(evidence.get("market_state") or "unknown")
        momentum = str(evidence.get("momentum_phase") or "unknown")
        confidence = self._float(evidence.get("market_state_confidence"))
        mtf = self._float(evidence.get("mtf_alignment_score"))
        base = {
            "opening_drive": 78,
            "compression_expansion": 76,
            "vwap_continuation": 72,
            "trend_day": 74,
            "reversal": 66,
            "expiry": 62,
            "normal_day": 55,
            "other": 35,
        }.get(group, 50)
        score = base
        if market_state in {"trend_expansion", "directional_acceptance", "compression_breakout"}:
            score += 8
        elif market_state in {"balanced_rotation", "data_uncertain", "execution_untradable"}:
            score -= 25
        if momentum in {"confirmation", "acceleration", "continuation"}:
            score += 8
        elif momentum in {"exhaustion", "failure"}:
            score -= 35
        if mtf >= 65:
            score += 5
        if confidence and confidence < 0.45:
            score -= 10
        score = max(0, min(100, score))
        unsuitable_state = market_state in {"balanced_rotation", "data_uncertain", "execution_untradable"}
        exhausted = momentum in {"exhaustion", "failure"}
        eligible = score >= settings.setup_policy_min_score and not unsuitable_state and not exhausted
        abstention = None
        if market_state == "balanced_rotation":
            abstention = "SETUP_FAMILY_NOT_ALLOWED_IN_RANGE"
        elif market_state == "data_uncertain":
            abstention = "SETUP_CONTEXT_UNCERTAIN"
        elif market_state == "execution_untradable":
            abstention = "CONTRACT_NOT_EXECUTABLE"
        elif exhausted:
            abstention = "SETUP_PHASE_NO_LONGER_ACTIONABLE"
        elif not eligible:
            abstention = "SETUP_POLICY_SCORE_BELOW_THRESHOLD"

        profiles = {
            "opening_drive": {"time_stop_minutes": 12, "trail_after_r": 1.0, "target_style": "scale_on_expansion"},
            "compression_expansion": {"time_stop_minutes": 15, "trail_after_r": 1.2, "target_style": "measured_move"},
            "vwap_continuation": {"time_stop_minutes": 15, "trail_after_r": 1.0, "target_style": "vwap_structure"},
            "trend_day": {"time_stop_minutes": 22, "trail_after_r": 1.5, "target_style": "runner"},
            "reversal": {"time_stop_minutes": 10, "trail_after_r": 0.8, "target_style": "fast_mean_reversion"},
            "expiry": {"time_stop_minutes": 7, "trail_after_r": 0.7, "target_style": "gamma_scalp"},
            "normal_day": {"time_stop_minutes": 12, "trail_after_r": 1.0, "target_style": "fixed_structure"},
        }
        return {
            "eligible": eligible,
            "policy_score": score,
            "score_adjustment": round((score - 50) * 0.2),
            "minimum_score": settings.setup_policy_min_score,
            "abstention_code": abstention,
            "entry_policy": {
                "allowed_market_states": self._allowed_states(group),
                "allowed_momentum_phases": ["acceleration", "breakout", "confirmation", "continuation"],
                "requires_premium_participation": True,
                "requires_executable_ask": True,
            },
            "exit_profile": profiles.get(group, profiles["normal_day"]),
            "invalidation": [
                "underlying_structure_invalidates_setup_family",
                "option_premium_loses_vwap_and_breakout_support",
                "momentum_phase_changes_to_exhaustion_or_failure",
            ],
        }

    def _allowed_states(self, group: str) -> list[str]:
        if group == "reversal":
            return ["failed_auction_reversal", "transition"]
        if group == "compression_expansion":
            return ["compression_breakout", "trend_expansion"]
        return ["trend_expansion", "directional_acceptance", "compression_breakout"]

    def _vwap_context(self, snapshot: dict[str, Any], premium_details: dict[str, Any], bullish: bool) -> dict[str, bool]:
        price = self._float(snapshot.get("price"))
        vwap = self._float(snapshot.get("vwap"))
        premium = self._float(premium_details.get("last_close") or premium_details.get("last_price"))
        premium_vwap = self._float(premium_details.get("option_vwap"))
        bank_supports = price > 0 and vwap > 0 and ((bullish and price >= vwap) or ((not bullish) and price <= vwap))
        premium_supports = premium > 0 and premium_vwap > 0 and premium >= premium_vwap
        return {"banknifty_supports": bank_supports, "premium_supports": premium_supports}

    def _day_type(self, factor_scores: dict[str, Any], bank_details: dict[str, Any]) -> str:
        day_eval = self._dict(factor_scores.get("day_type"))
        day_details = self._dict(day_eval.get("details"))
        return str(
            day_details.get("day_type")
            or bank_details.get("dayType")
            or self._nested(factor_scores, "banknifty_regime_filter", "details", "compression_expansion", "day_type")
            or "unknown"
        )

    def _dict(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _nested(self, value: dict[str, Any], *keys: str) -> Any:
        current: Any = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
