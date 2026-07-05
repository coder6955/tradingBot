from __future__ import annotations

from datetime import datetime, time
from typing import Any, Callable

from app.config import settings
from app.services.time_utils import ist_now_naive
from app.services.trade_setup_service import OptionContract


class BankNiftyRegimeFilterService:
    """Bank Nifty option-buying no-trade filter.

    This service intentionally does not create signals or boost score. It only
    answers whether the current Bank Nifty regime is suitable for buying
    options using already-computed scanner context.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or ist_now_naive

    def evaluate(
        self,
        *,
        symbol: str,
        trend: str,
        snapshot: dict[str, Any],
        contract: OptionContract,
        prices: dict[str, float],
        premium_eval: dict[str, Any],
        day_type_eval: dict[str, Any],
        time_bucket_eval: dict[str, Any],
        banknifty_eval: dict[str, Any],
        volatility_eval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not settings.enable_banknifty_regime_filter or symbol.upper() != "BANKNIFTY":
            return {"enabled": False, "passed": True, "score": 100, "classification": "disabled", "hard_reasons": [], "soft_reasons": [], "details": {}}

        bullish = trend.lower() == "bullish"
        bank_details = banknifty_eval.get("details", {}) if isinstance(banknifty_eval.get("details"), dict) else {}
        premium_details = premium_eval.get("details", {}) if isinstance(premium_eval.get("details"), dict) else {}
        volatility_details = volatility_eval.get("details", {}) if isinstance(volatility_eval, dict) and isinstance(volatility_eval.get("details"), dict) else {}

        opening = self._opening_structure(bank_details, bullish)
        gap = self._gap_context(snapshot, bullish)
        vwap = self._vwap_context(snapshot, premium_details, bullish)
        compression = self._compression_expansion(day_type_eval, premium_details)
        expiry = self._expiry_context(bank_details, premium_eval, volatility_eval)
        time_edge = self._time_of_day_context(time_bucket_eval, premium_eval, day_type_eval)
        decay = self._decay_context(prices, premium_eval, volatility_eval, volatility_details)

        components = {
            "opening_structure": opening,
            "gap_context": gap,
            "vwap_context": vwap,
            "compression_expansion": compression,
            "expiry_behavior": expiry,
            "time_of_day_edge": time_edge,
            "premium_decay_environment": decay,
        }
        hard_reasons: list[str] = []
        soft_reasons: list[str] = []
        score = 100
        for component in components.values():
            hard_reasons.extend(component.get("hard_reasons", []))
            soft_reasons.extend(component.get("soft_reasons", []))
            score -= int(component.get("penalty", 0))

        hard_reasons = list(dict.fromkeys(str(reason) for reason in hard_reasons if str(reason)))
        soft_reasons = list(dict.fromkeys(str(reason) for reason in soft_reasons if str(reason)))
        score = max(0, min(100, score))
        if hard_reasons:
            classification = "NO_BUY_REGIME"
        elif score >= 80:
            classification = "OPTION_BUYING_FAVORABLE"
        elif score >= 65:
            classification = "SELECTIVE_OPTION_BUYING"
        else:
            classification = "WEAK_OPTION_BUYING_REGIME"

        return {
            "enabled": True,
            "passed": not hard_reasons and score >= settings.min_banknifty_regime_score,
            "score": score,
            "classification": classification,
            "hard_reasons": hard_reasons,
            "soft_reasons": soft_reasons,
            "details": {
                **components,
                "min_score": settings.min_banknifty_regime_score,
                "verdict": self._verdict(classification, hard_reasons, soft_reasons),
            },
        }

    def _opening_structure(self, bank_details: dict[str, Any], bullish: bool) -> dict[str, Any]:
        opening = bank_details.get("openingRangeStatus", {}) if isinstance(bank_details.get("openingRangeStatus"), dict) else {}
        status = str(opening.get("status") or "unknown")
        large_wick = bool(opening.get("largeWick"))
        hard: list[str] = []
        soft: list[str] = []
        penalty = 0
        if status in {"failed_breakout", "failed_breakdown"} or (large_wick and status in {"opposite_side", "inside_opening_range"}):
            hard.append("opening_trap_structure")
            penalty += 35
        elif status in {"inside_opening_range", "pre_opening_range_complete"}:
            hard.append(str(opening.get("reason") or "opening_range_not_ready"))
            penalty += 30
        elif status in {"breakout", "breakdown"}:
            expected = "breakout" if bullish else "breakdown"
            if status != expected:
                hard.append("opening_drive_is_opposite_to_trade_direction")
                penalty += 30
        elif status == "unavailable":
            soft.append("opening structure unavailable")
            penalty += 5
        return {"state": status, "large_wick": large_wick, "hard_reasons": hard, "soft_reasons": soft, "penalty": penalty}

    def _gap_context(self, snapshot: dict[str, Any], bullish: bool) -> dict[str, Any]:
        previous_close = self._float(snapshot.get("previous_day_close"))
        day_open = self._float(snapshot.get("day_open"))
        price = self._float(snapshot.get("price"))
        hard: list[str] = []
        soft: list[str] = []
        penalty = 0
        gap_pct = 0.0
        if previous_close > 0 and day_open > 0:
            gap_pct = ((day_open - previous_close) / previous_close) * 100
        if abs(gap_pct) >= settings.banknifty_significant_gap_pct and price > 0:
            gap_direction = "gap_up" if gap_pct > 0 else "gap_down"
            failed_gap = (gap_pct > 0 and price < day_open and bullish) or (gap_pct < 0 and price > day_open and not bullish)
            opposite_gap = (gap_pct < 0 and bullish) or (gap_pct > 0 and not bullish)
            if failed_gap:
                hard.append(f"{gap_direction}_trap_no_option_buying")
                penalty += 25
            elif opposite_gap:
                soft.append("gap context is opposite to trade direction")
                penalty += 8
        return {"gap_pct": round(gap_pct, 3), "hard_reasons": hard, "soft_reasons": soft, "penalty": penalty}

    def _vwap_context(self, snapshot: dict[str, Any], premium_details: dict[str, Any], bullish: bool) -> dict[str, Any]:
        price = self._float(snapshot.get("price"))
        vwap = self._float(snapshot.get("vwap"))
        premium = self._float(premium_details.get("last_close") or premium_details.get("last_price"))
        premium_vwap = self._float(premium_details.get("option_vwap"))
        hard: list[str] = []
        penalty = 0
        if price > 0 and vwap > 0:
            supports = price >= vwap if bullish else price <= vwap
            if not supports:
                hard.append("banknifty_vwap_reclaim_not_confirmed" if bullish else "banknifty_vwap_rejection_not_confirmed")
                penalty += 30
        if premium > 0 and premium_vwap > 0 and premium < premium_vwap:
            hard.append("option_premium_below_vwap")
            penalty += 25
        return {"banknifty_price": price, "banknifty_vwap": vwap, "option_premium": premium, "option_vwap": premium_vwap, "hard_reasons": hard, "soft_reasons": [], "penalty": penalty}

    def _compression_expansion(self, day_type_eval: dict[str, Any], premium_details: dict[str, Any]) -> dict[str, Any]:
        details = day_type_eval.get("details", {}) if isinstance(day_type_eval.get("details"), dict) else {}
        day_type = str(details.get("day_type") or "unknown")
        day_range_pct = self._float(details.get("day_range_pct"))
        breakout = bool(premium_details.get("breakout"))
        volume_expansion = bool(premium_details.get("volume_expansion"))
        hard: list[str] = []
        soft: list[str] = []
        penalty = 0
        compressed = day_type in {"rotation_range", "range"} or (0 < day_range_pct < settings.banknifty_compression_day_range_pct)
        if compressed and not (breakout and volume_expansion):
            hard.append("range_compression_without_expansion")
            penalty += 30
        elif compressed:
            soft.append("range compression is expanding; require clean entry timing")
            penalty += 3
        return {"day_type": day_type, "day_range_pct": day_range_pct, "premium_breakout": breakout, "premium_volume_expansion": volume_expansion, "hard_reasons": hard, "soft_reasons": soft, "penalty": penalty}

    def _expiry_context(self, bank_details: dict[str, Any], premium_eval: dict[str, Any], volatility_eval: dict[str, Any] | None) -> dict[str, Any]:
        dte = bank_details.get("dteMode", {}) if isinstance(bank_details.get("dteMode"), dict) else {}
        risk = str(dte.get("risk") or "unknown")
        premium_score = int(premium_eval.get("score") or 0)
        vol_score = int(volatility_eval.get("score") or 100) if isinstance(volatility_eval, dict) else 100
        hard: list[str] = []
        soft: list[str] = []
        penalty = 0
        if risk == "near_expiry" and premium_score < settings.banknifty_expiry_min_premium_score:
            hard.append("expiry_day_without_strong_premium_expansion")
            penalty += 30
        elif risk == "far_expiry" and vol_score < settings.min_volatility_edge_score:
            soft.append("far-expiry premium needs stronger volatility edge")
            penalty += 8
        return {"days_to_expiry": dte.get("daysToExpiry"), "risk": risk, "premium_score": premium_score, "volatility_score": vol_score, "hard_reasons": hard, "soft_reasons": soft, "penalty": penalty}

    def _time_of_day_context(self, time_bucket_eval: dict[str, Any], premium_eval: dict[str, Any], day_type_eval: dict[str, Any]) -> dict[str, Any]:
        now = self.clock().time()
        cutoff = self._parse_time(settings.banknifty_late_trade_cutoff_time)
        details = day_type_eval.get("details", {}) if isinstance(day_type_eval.get("details"), dict) else {}
        day_type = str(details.get("day_type") or "unknown")
        premium_score = int(premium_eval.get("score") or 0)
        hard: list[str] = []
        soft: list[str] = []
        penalty = 0
        if now >= cutoff and (premium_score < settings.banknifty_late_trade_min_premium_score or day_type not in {"trend_expansion", "directional_acceptance"}):
            hard.append("late_day_premium_decay_environment")
            penalty += 25
        if settings.enable_time_bucket_filter and not time_bucket_eval.get("passed", False):
            hard.extend(str(reason) for reason in time_bucket_eval.get("reasons", ["time bucket edge failed"]))
            penalty += 20
        elif not settings.enable_time_bucket_filter and time_bucket_eval.get("reasons"):
            soft.extend(str(reason) for reason in time_bucket_eval.get("reasons", []))
            penalty += 3
        return {"current_time": now.strftime("%H:%M"), "late_cutoff": settings.banknifty_late_trade_cutoff_time, "hard_reasons": hard, "soft_reasons": soft, "penalty": penalty}

    def _decay_context(
        self,
        prices: dict[str, float],
        premium_eval: dict[str, Any],
        volatility_eval: dict[str, Any] | None,
        volatility_details: dict[str, Any],
    ) -> dict[str, Any]:
        risk_reward = self._float(prices.get("risk_reward"))
        premium_score = int(premium_eval.get("score") or 0)
        main_risk = str(volatility_eval.get("main_risk") or "") if isinstance(volatility_eval, dict) else ""
        best_coverage = self._float(volatility_details.get("best_expected_move_coverage"))
        hard: list[str] = []
        soft: list[str] = []
        penalty = 0
        if risk_reward and risk_reward < settings.min_remaining_risk_reward:
            hard.append("low_rr_premium_decay_environment")
            penalty += 30
        if main_risk in {"iv_crush", "overpriced_premium"}:
            hard.append(main_risk)
            penalty += 30
        if best_coverage and best_coverage < settings.vol_edge_min_expected_move_coverage and premium_score < settings.banknifty_expiry_min_premium_score:
            hard.append("expected_move_too_small_for_option_buying")
            penalty += 25
        elif premium_score < settings.min_option_premium_confirmation_score:
            soft.append("premium expansion viability is weak")
            penalty += 8
        return {"risk_reward": risk_reward, "premium_score": premium_score, "volatility_main_risk": main_risk, "best_expected_move_coverage": best_coverage, "hard_reasons": hard, "soft_reasons": soft, "penalty": penalty}

    def _verdict(self, classification: str, hard_reasons: list[str], soft_reasons: list[str]) -> str:
        if hard_reasons:
            return "Do not buy Bank Nifty options in this regime: " + "; ".join(hard_reasons[:3])
        if soft_reasons:
            return f"{classification}: valid only with strict entry timing; cautions: " + "; ".join(soft_reasons[:3])
        return f"{classification}: Bank Nifty regime supports option buying if all existing gates pass"

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
