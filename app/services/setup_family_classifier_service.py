from __future__ import annotations

from typing import Any

from app.services.trade_setup_service import OptionContract


class SetupFamilyClassifierService:
    """Classify Bank Nifty option-buying setups for later research.

    The classifier is intentionally descriptive. It does not approve, reject,
    score, or alter trades; it only adds stable labels to saved decisions.
    """

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
        return {
            "name": name,
            "group": group,
            "reasons": reasons,
            "evidence": evidence or {},
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
