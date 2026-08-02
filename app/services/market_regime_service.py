from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from app.config import settings


class MarketRegimeService:
    """Evaluate broad-market conditions before any symbol-level option trade."""

    def evaluate(
        self,
        symbol: str,
        trend: str,
        side: str,
        nifty: Dict[str, Any] | None,
        banknifty: Dict[str, Any] | None,
        vix: Dict[str, Any] | None,
        *,
        snapshot: Dict[str, Any] | None = None,
        multi_timeframe: Dict[str, Any] | None = None,
        banknifty_eval: Dict[str, Any] | None = None,
        volatility_eval: Dict[str, Any] | None = None,
        day_type_eval: Dict[str, Any] | None = None,
        price_action_eval: Dict[str, Any] | None = None,
        premium_eval: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        score = 50

        calendar = self.calendar_check(symbol)
        if not calendar["passed"]:
            reasons.extend(calendar["reasons"])

        bullish = trend.lower() == "bullish"
        nifty_bullish = bool((nifty or {}).get("trend_bullish"))
        bank_bullish = bool((banknifty or {}).get("trend_bullish"))
        if bullish and nifty_bullish:
            score += 15
        elif not bullish and not nifty_bullish:
            score += 15
        else:
            score -= 10
            reasons.append("symbol trend is not aligned with NIFTY regime")

        if symbol.upper() in {"BANKNIFTY", "HDFCBANK", "ICICIBANK", "AXISBANK", "SBIN"}:
            if bullish == bank_bullish:
                score += 10
            else:
                score -= 10
                reasons.append("banking symbol is not aligned with BANKNIFTY regime")

        vix_value = float((vix or {}).get("price") or 0.0)
        if vix_value > 0:
            limit = (
                settings.max_vix_for_selling
                if side.upper() == "SELL"
                else settings.max_vix_for_buying
            )
            if vix_value <= limit:
                score += 15
            else:
                score -= 20
                reasons.append(
                    f"India VIX {vix_value:.2f} is above configured limit {limit:.2f}"
                )
        else:
            reasons.append("India VIX was unavailable; regime score reduced")
            score -= 5

        dimensions = self._dimensions(
            trend=trend,
            snapshot=snapshot or banknifty or {},
            multi_timeframe=multi_timeframe or {},
            banknifty_eval=banknifty_eval or {},
            volatility_eval=volatility_eval or {},
            day_type_eval=day_type_eval or {},
            price_action_eval=price_action_eval or {},
            premium_eval=premium_eval or {},
        )
        if (
            settings.enable_hierarchical_market_state
            and dimensions["available_dimensions"]
        ):
            # Preserve the broad-market score while adding independent structure,
            # volatility, participation, location, and execution evidence.
            score = round(score * 0.45 + dimensions["score"] * 0.55)
        state = self._state(dimensions, trend)
        confidence = dimensions["confidence"]
        uncertainty = dimensions["uncertainty"]
        option_buying_suitable = (
            state
            in {
                "trend_expansion",
                "directional_acceptance",
                "compression_breakout",
                "failed_auction_reversal",
            }
            and confidence >= settings.market_state_min_confidence
        )
        enriched_context_supplied = any(
            value
            for value in (
                snapshot,
                multi_timeframe,
                banknifty_eval,
                volatility_eval,
                day_type_eval,
                price_action_eval,
                premium_eval,
            )
        )
        safety_block = enriched_context_supplied and state in {
            "data_uncertain",
            "execution_untradable",
        }
        abstention_code = self._abstention_code(
            state, option_buying_suitable, uncertainty
        )

        passed = (
            score >= settings.min_market_regime_score
            and calendar["passed"]
            and not safety_block
        )
        if score < settings.min_market_regime_score:
            reasons.append("market regime score is below threshold")
        if safety_block:
            reasons.append(abstention_code or "market_state_untradable")

        return {
            "score": max(0, min(100, score)),
            "passed": passed,
            "reasons": reasons,
            "regime": state,
            "confidence": confidence,
            "uncertainty": uncertainty,
            "option_buying_suitable": option_buying_suitable,
            "abstention_code": abstention_code,
            "invalidation": self._invalidation(state, trend),
            "dimensions": dimensions["dimensions"],
            "details": {
                "nifty_trend_bullish": nifty_bullish,
                "banknifty_trend_bullish": bank_bullish,
                "vix": vix_value,
                "calendar": calendar,
                "hierarchical_state_enabled": settings.enable_hierarchical_market_state,
                "available_dimensions": dimensions["available_dimensions"],
            },
        }

    def _dimensions(
        self,
        *,
        trend: str,
        snapshot: Dict[str, Any],
        multi_timeframe: Dict[str, Any],
        banknifty_eval: Dict[str, Any],
        volatility_eval: Dict[str, Any],
        day_type_eval: Dict[str, Any],
        price_action_eval: Dict[str, Any],
        premium_eval: Dict[str, Any],
    ) -> Dict[str, Any]:
        desired_bullish = trend.lower() == "bullish"
        mtf_available = int(multi_timeframe.get("available_timeframes") or 0)
        structure_score = float(multi_timeframe.get("alignment_score") or 0.0)
        if not mtf_available:
            structure_score = (
                65.0
                if bool(snapshot.get("trend_bullish")) == desired_bullish
                else 35.0
                if "trend_bullish" in snapshot
                else 50.0
            )

        volatility_score = float(volatility_eval.get("score") or 50.0)
        vol_available = bool(volatility_eval)
        premium = (
            premium_eval.get("details", {})
            if isinstance(premium_eval.get("details"), dict)
            else {}
        )
        participation_score = float(premium_eval.get("score") or 50.0)
        if snapshot.get("volume_confirmed"):
            participation_score = min(100.0, participation_score + 10.0)
        bank_details = (
            banknifty_eval.get("details", {})
            if isinstance(banknifty_eval.get("details"), dict)
            else {}
        )
        room = (
            bank_details.get("expectedMoveCheck", {})
            if isinstance(bank_details.get("expectedMoveCheck"), dict)
            else {}
        )
        location_score = 50.0
        if room:
            coverage = float(room.get("coverage") or room.get("bestCoverage") or 0.0)
            location_score = max(20.0, min(90.0, 35.0 + coverage * 35.0))
        price_details = (
            price_action_eval.get("details", {})
            if isinstance(price_action_eval.get("details"), dict)
            else {}
        )
        if price_details.get("hard_block"):
            location_score = min(location_score, 20.0)

        spread = float(premium.get("spread_pct") or 0.0)
        execution_score = 70.0 if premium else 50.0
        if spread > 0:
            execution_score = max(0.0, min(100.0, 100.0 - spread * 12.0))
        available = sum(
            (
                bool(snapshot),
                bool(mtf_available),
                vol_available,
                bool(premium),
                bool(price_action_eval or banknifty_eval),
            )
        )
        values = [
            structure_score,
            volatility_score,
            participation_score,
            location_score,
            execution_score,
        ]
        score = sum(values) / len(values)
        dispersion = sum(abs(value - score) for value in values) / (len(values) * 100.0)
        coverage_uncertainty = 1.0 - available / 5.0
        uncertainty = max(0.0, min(1.0, coverage_uncertainty * 0.7 + dispersion * 0.3))
        confidence = max(0.0, min(1.0, (score / 100.0) * (1.0 - uncertainty)))
        return {
            "dimensions": {
                "structure": {
                    "score": round(structure_score, 2),
                    "source": "multi_timeframe"
                    if mtf_available
                    else "snapshot_fallback",
                },
                "volatility": {
                    "score": round(volatility_score, 2),
                    "classification": volatility_eval.get("classification"),
                },
                "participation": {
                    "score": round(participation_score, 2),
                    "premium_breakout": bool(premium.get("breakout")),
                },
                "location": {
                    "score": round(location_score, 2),
                    "price_action_hard_block": bool(price_details.get("hard_block")),
                },
                "execution": {
                    "score": round(execution_score, 2),
                    "spread_pct": spread or None,
                },
            },
            "score": score,
            "confidence": round(confidence, 3),
            "uncertainty": round(uncertainty, 3),
            "available_dimensions": available,
        }

    def _state(self, dimensions: Dict[str, Any], trend: str) -> str:
        if (
            not dimensions["available_dimensions"]
            or dimensions["uncertainty"] > settings.market_state_max_uncertainty
        ):
            return "data_uncertain"
        values = dimensions["dimensions"]
        if values["execution"]["score"] < 35:
            return "execution_untradable"
        structure = values["structure"]["score"]
        participation = values["participation"]["score"]
        volatility = values["volatility"]["score"]
        location = values["location"]["score"]
        if structure >= 70 and participation >= 65 and volatility >= 55:
            return "trend_expansion"
        if structure >= 62 and participation >= 55:
            return "directional_acceptance"
        if participation >= 70 and volatility >= 60 and structure >= 50:
            return "compression_breakout"
        if location >= 65 and participation >= 55 and structure < 45:
            return "failed_auction_reversal"
        if structure < 45 and participation < 55:
            return "balanced_rotation"
        return "transition"

    def _abstention_code(
        self, state: str, suitable: bool, uncertainty: float
    ) -> str | None:
        if (
            state == "data_uncertain"
            or uncertainty > settings.market_state_max_uncertainty
        ):
            return "MARKET_STATE_DATA_UNCERTAIN"
        if state == "execution_untradable":
            return "EXECUTION_QUALITY_UNTRADABLE"
        if not suitable:
            return {
                "balanced_rotation": "RANGE_THETA_DISADVANTAGE",
                "transition": "REGIME_TRANSITION_UNCONFIRMED",
            }.get(state, "OPTION_BUYING_EDGE_NOT_CONFIRMED")
        return None

    def _invalidation(self, state: str, trend: str) -> List[str]:
        direction = "bullish" if trend.lower() == "bullish" else "bearish"
        return [
            f"{direction}_multi_timeframe_structure_breaks",
            "participation_falls_below_breakout_acceptance",
            "volatility_expansion_reverses_into_iv_crush",
            "execution_spread_or_depth_becomes_untradable",
            f"market_state_changes_from_{state}",
        ]

    def calendar_check(self, symbol: str) -> Dict[str, Any]:
        reasons: List[str] = []
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.date().isoformat()

        blocked_dates = {
            item.strip()
            for item in settings.blocked_event_dates.split(",")
            if item.strip()
        }
        if today in blocked_dates:
            reasons.append(f"{today} is configured as a blocked event date")

        blocked_symbols = {
            item.strip().upper()
            for item in settings.blocked_symbols.split(",")
            if item.strip()
        }
        if symbol.upper() in blocked_symbols:
            reasons.append(f"{symbol.upper()} is configured as blocked")

        if settings.enforce_market_hours:
            market_open = self._parse_time(settings.market_open_time)
            market_close = self._parse_time(settings.market_close_time)
            current = now.time()
            if now.weekday() >= 5:
                reasons.append("market is closed on weekends")
            elif current < market_open or current > market_close:
                reasons.append("current time is outside configured trade-entry window")

        return {"passed": not reasons, "reasons": reasons, "timestamp": now.isoformat()}

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
