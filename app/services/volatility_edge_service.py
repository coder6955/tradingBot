from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import mean, stdev
from typing import Any

from app.config import settings
from app.services.database import Candle, OptionQuoteSnapshot, get_session
from app.services.time_utils import ist_now_naive
from app.services.trade_setup_service import OptionContract


class VolatilityEdgeService:
    """Diagnose whether current volatility supports Bank Nifty option buying.

    This service is intentionally diagnostic-first. It does not place trades and
    it only becomes a hard gate when ENABLE_VOLATILITY_EDGE_HARD_GATE=true.
    """

    UNKNOWN_DATA_MISSING = "UNKNOWN_DATA_MISSING"

    def evaluate(
        self,
        *,
        symbol: str,
        contract: OptionContract | None,
        option_quality: dict[str, Any] | None = None,
        premium_eval: dict[str, Any] | None = None,
        market_snapshots: dict[str, dict[str, Any]] | None = None,
        prices: dict[str, float] | None = None,
        action: str = "",
        side: str = "BUY",
        expected_move_check: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not settings.enable_volatility_edge:
            return {
                "enabled": False,
                "passed": True,
                "score": 100,
                "classification": "disabled",
                "volatility_edge_for_option_buying": "disabled",
                "main_risk": "none",
                "reasons": [],
                "details": {},
            }

        reasons: list[str] = []
        selected_iv = self._selected_iv(option_quality)
        vix = self._snapshot_price((market_snapshots or {}).get("INDIAVIX"))
        banknifty_snapshot = (market_snapshots or {}).get("BANKNIFTY") or {}
        spot_price = self._snapshot_price(banknifty_snapshot)

        candles = self._recent_candles(symbol.upper(), settings.candle_confirmation_timeframe, limit=160)
        realized = self._realized_volatility(candles)
        if spot_price is None:
            spot_price = realized.get("last_close")

        iv_history = self._iv_history(contract, selected_iv, vix)
        expansion = self._expansion_state(contract, premium_eval)
        expected_move = self._expected_move_state(
            spot_price=spot_price,
            selected_iv=selected_iv,
            realized=realized,
            option_quality=option_quality,
            prices=prices,
            expected_move_check=expected_move_check,
        )

        if not candles:
            reasons.append(f"{self.UNKNOWN_DATA_MISSING}: Bank Nifty candle history unavailable")
        if selected_iv is None and vix is None:
            reasons.append(f"{self.UNKNOWN_DATA_MISSING}: selected option IV and India VIX unavailable")
        if realized["realized_volatility"] is None:
            reasons.append(f"{self.UNKNOWN_DATA_MISSING}: realized volatility unavailable")
        if iv_history["sample_count"] < settings.vol_edge_min_iv_samples:
            reasons.append(f"{self.UNKNOWN_DATA_MISSING}: insufficient IV history")

        iv_vs_rv = self._iv_vs_realized(selected_iv, vix, realized["realized_volatility"])
        score, scoring_reasons = self._score(iv_vs_rv, iv_history, expansion, expected_move)
        reasons.extend(scoring_reasons)
        classification = self._classification(iv_vs_rv, iv_history, expansion, expected_move, reasons, score)
        edge_label, main_risk = self._edge_label(classification, score, expected_move)
        passed = bool(score >= settings.min_volatility_edge_score and not self._has_unknown_only_block(reasons))

        details = {
            "action": action,
            "side": side.upper(),
            "selected_iv": self._round(selected_iv),
            "india_vix": self._round(vix),
            "iv_source": "selected_option" if selected_iv is not None else ("india_vix" if vix is not None else "unknown"),
            "iv_rank": iv_history["iv_rank"],
            "iv_percentile": iv_history["iv_percentile"],
            "iv_history_sample_count": iv_history["sample_count"],
            "iv_history_data_quality": iv_history["data_quality"],
            "iv_rank_source": iv_history["source"],
            "iv_to_rv_ratio": iv_vs_rv["iv_to_rv_ratio"],
            "iv_minus_rv": iv_vs_rv["iv_minus_rv"],
            "iv_vs_realized_label": iv_vs_rv["label"],
            "realized_volatility_intraday": realized["realized_volatility_intraday"],
            "realized_volatility_daily": realized["realized_volatility_daily"],
            "realized_volatility": realized["realized_volatility"],
            "realized_range_pct": realized["realized_range_pct"],
            "banknifty_atr_pct": realized["banknifty_atr_pct"],
            "volatility_source": realized["volatility_source"],
            "candles_used": realized["candles_used"],
            "iv_change_pct": expansion["iv_change_pct"],
            "vix_change_pct": expansion["vix_change_pct"],
            "premium_range_expansion_ratio": expansion["premium_range_expansion_ratio"],
            "iv_expansion_supported": expansion["iv_expansion_supported"],
            "iv_contraction_risk": expansion["iv_contraction_risk"],
            "iv_crush_risk": self._iv_crush_risk(iv_vs_rv, iv_history, expansion),
            "expected_move_from_iv": expected_move["expected_move_from_iv"],
            "expected_move_from_atr": expected_move["expected_move_from_atr"],
            "expected_move_from_recent_range": expected_move["expected_move_from_recent_range"],
            "required_move_for_target": expected_move["required_move_for_target"],
            "expected_move_coverage_iv": expected_move["expected_move_coverage_iv"],
            "expected_move_coverage_atr": expected_move["expected_move_coverage_atr"],
            "expected_move_coverage_range": expected_move["expected_move_coverage_range"],
            "best_expected_move_coverage": expected_move["best_expected_move_coverage"],
            "expected_move_label": expected_move["label"],
            "min_expected_move_coverage": settings.vol_edge_min_expected_move_coverage,
            "min_score": settings.min_volatility_edge_score,
            "hard_gate_enabled": settings.enable_volatility_edge_hard_gate,
        }
        return {
            "enabled": True,
            "passed": passed,
            "score": score,
            "classification": classification,
            "volatility_edge_for_option_buying": edge_label,
            "main_risk": main_risk,
            "reasons": list(dict.fromkeys(reasons)),
            "details": details,
        }

    def _recent_candles(self, symbol: str, timeframe: str, *, limit: int) -> list[Candle]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()

    def _realized_volatility(self, candles: list[Candle]) -> dict[str, Any]:
        closes = [float(candle.close_price) for candle in candles if float(candle.close_price or 0) > 0]
        highs = [float(candle.high_price) for candle in candles if float(candle.high_price or 0) > 0]
        lows = [float(candle.low_price) for candle in candles if float(candle.low_price or 0) > 0]
        intraday = self._annualized_log_return_vol(closes, periods_per_year=252 * 75)
        daily = self._daily_realized_vol(candles)
        last_close = closes[-1] if closes else None
        range_pct = None
        if highs and lows and last_close:
            range_pct = ((max(highs) - min(lows)) / max(last_close, 0.01)) * 100
        atr_pct = self._atr_pct(candles)
        source = "unknown"
        realized = None
        if daily is not None:
            source = "daily_realized"
            realized = daily
        elif intraday is not None:
            source = "intraday_realized"
            realized = intraday
        elif atr_pct is not None:
            source = "range_atr"
            realized = atr_pct * math.sqrt(252)
        return {
            "realized_volatility_intraday": self._round(intraday),
            "realized_volatility_daily": self._round(daily),
            "realized_volatility": self._round(realized),
            "realized_range_pct": self._round(range_pct),
            "banknifty_atr_pct": self._round(atr_pct),
            "volatility_source": source,
            "last_close": last_close,
            "candles_used": len(closes),
        }

    def _annualized_log_return_vol(self, closes: list[float], *, periods_per_year: int) -> float | None:
        returns = [math.log(closes[idx] / closes[idx - 1]) for idx in range(1, len(closes)) if closes[idx - 1] > 0 and closes[idx] > 0]
        if len(returns) < 8:
            return None
        return stdev(returns) * math.sqrt(periods_per_year) * 100

    def _daily_realized_vol(self, candles: list[Candle]) -> float | None:
        daily_close: dict[str, float] = {}
        for candle in candles:
            timestamp = candle.timestamp
            key = timestamp.date().isoformat() if isinstance(timestamp, datetime) else str(timestamp)[:10]
            daily_close[key] = float(candle.close_price)
        closes = [daily_close[key] for key in sorted(daily_close)]
        if len(closes) < 5:
            return None
        return self._annualized_log_return_vol(closes, periods_per_year=252)

    def _atr_pct(self, candles: list[Candle]) -> float | None:
        if len(candles) < 2:
            return None
        ranges: list[float] = []
        previous_close = float(candles[0].close_price)
        for candle in candles[1:]:
            high = float(candle.high_price)
            low = float(candle.low_price)
            close = float(candle.close_price)
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
            previous_close = close
        if not ranges or previous_close <= 0:
            return None
        return (mean(ranges[-14:]) / previous_close) * 100

    def _iv_history(self, contract: OptionContract | None, selected_iv: float | None, vix: float | None) -> dict[str, Any]:
        values: list[float] = []
        source = "unavailable"
        if contract is not None:
            cutoff = ist_now_naive() - timedelta(days=settings.vol_edge_iv_lookback_days)
            session = get_session()
            try:
                rows = (
                    session.query(OptionQuoteSnapshot.implied_volatility)
                    .filter(
                        OptionQuoteSnapshot.tradingsymbol == contract.tradingsymbol,
                        OptionQuoteSnapshot.timestamp >= cutoff,
                        OptionQuoteSnapshot.implied_volatility.isnot(None),
                    )
                    .order_by(OptionQuoteSnapshot.timestamp.asc())
                    .all()
                )
                values = [iv for raw in rows if (iv := self._normalize_iv(raw[0])) is not None]
                if values:
                    source = "selected_option_iv_history"
            finally:
                session.close()

        if len(values) < settings.vol_edge_min_iv_samples:
            vix_values = self._recent_vix_values()
            if len(vix_values) >= settings.vol_edge_min_iv_samples:
                values = vix_values
                source = "india_vix_history"

        current_iv = selected_iv if selected_iv is not None else vix
        if current_iv is None or len(values) < settings.vol_edge_min_iv_samples:
            return {
                "iv_rank": None,
                "iv_percentile": None,
                "sample_count": len(values),
                "source": source,
                "data_quality": self.UNKNOWN_DATA_MISSING,
            }
        low = min(values)
        high = max(values)
        rank = 50.0 if high == low else ((current_iv - low) / (high - low)) * 100
        percentile = (sum(1 for value in values if value <= current_iv) / len(values)) * 100
        return {
            "iv_rank": self._round(max(0.0, min(100.0, rank))),
            "iv_percentile": self._round(max(0.0, min(100.0, percentile))),
            "sample_count": len(values),
            "source": source,
            "data_quality": "ok",
        }

    def _recent_vix_values(self) -> list[float]:
        candles = self._recent_candles("INDIAVIX", settings.candle_confirmation_timeframe, limit=max(settings.vol_edge_min_iv_samples, 60))
        return [float(candle.close_price) for candle in candles if float(candle.close_price or 0) > 0]

    def _expansion_state(self, contract: OptionContract | None, premium_eval: dict[str, Any] | None) -> dict[str, Any]:
        iv_change_pct = self._recent_option_iv_change(contract)
        vix_change_pct = self._recent_vix_change()
        premium_range_expansion_ratio = self._premium_range_expansion(contract)
        premium_details = premium_eval.get("details", {}) if isinstance(premium_eval, dict) else {}
        breakout = bool(premium_details.get("breakout"))
        iv_expansion_supported = (
            (iv_change_pct is not None and iv_change_pct > 1.0)
            or (vix_change_pct is not None and vix_change_pct > 1.0)
            or ((premium_range_expansion_ratio or 0.0) >= 1.10 and breakout)
        )
        iv_contraction_risk = (iv_change_pct is not None and iv_change_pct < -1.0) or (vix_change_pct is not None and vix_change_pct < -1.0)
        return {
            "iv_change_pct": self._round(iv_change_pct),
            "vix_change_pct": self._round(vix_change_pct),
            "premium_range_expansion_ratio": self._round(premium_range_expansion_ratio),
            "iv_expansion_supported": iv_expansion_supported,
            "iv_contraction_risk": iv_contraction_risk,
        }

    def _recent_option_iv_change(self, contract: OptionContract | None) -> float | None:
        if contract is None:
            return None
        session = get_session()
        try:
            rows = (
                session.query(OptionQuoteSnapshot.implied_volatility)
                .filter(
                    OptionQuoteSnapshot.tradingsymbol == contract.tradingsymbol,
                    OptionQuoteSnapshot.implied_volatility.isnot(None),
                )
                .order_by(OptionQuoteSnapshot.timestamp.desc())
                .limit(10)
                .all()
            )
            values = [iv for raw in reversed(rows) if (iv := self._normalize_iv(raw[0])) is not None]
        finally:
            session.close()
        if len(values) < 2 or values[0] <= 0:
            return None
        return ((values[-1] - values[0]) / values[0]) * 100

    def _recent_vix_change(self) -> float | None:
        values = self._recent_vix_values()[-10:]
        if len(values) < 2 or values[0] <= 0:
            return None
        return ((values[-1] - values[0]) / values[0]) * 100

    def _premium_range_expansion(self, contract: OptionContract | None) -> float | None:
        if contract is None:
            return None
        candles = self._recent_candles(contract.tradingsymbol, settings.candle_confirmation_timeframe, limit=8)
        ranges = [max(float(candle.high_price) - float(candle.low_price), 0.0) for candle in candles]
        if len(ranges) < 6:
            return None
        previous = mean(ranges[-6:-3])
        current = mean(ranges[-3:])
        if previous <= 0:
            return None
        return current / previous

    def _expected_move_state(
        self,
        *,
        spot_price: float | None,
        selected_iv: float | None,
        realized: dict[str, Any],
        option_quality: dict[str, Any] | None,
        prices: dict[str, float] | None,
        expected_move_check: dict[str, Any] | None,
    ) -> dict[str, Any]:
        days_to_expiry = self._nested_float(option_quality, "details", "greeks", "days_to_expiry")
        delta = abs(self._nested_float(option_quality, "details", "greeks", "delta") or 0.0)
        entry = self._dict_float(prices, "entry_price")
        target = self._dict_float(prices, "target_1")
        expected_from_iv = None
        expected_from_atr = None
        expected_from_range = None
        if spot_price and selected_iv and days_to_expiry is not None:
            expected_from_iv = spot_price * (selected_iv / 100.0) * math.sqrt(max(days_to_expiry, 1.0) / 365.0)
        atr_pct = realized.get("banknifty_atr_pct")
        if spot_price and atr_pct:
            expected_from_atr = spot_price * (float(atr_pct) / 100.0)
        range_pct = realized.get("realized_range_pct")
        if spot_price and range_pct:
            expected_from_range = spot_price * (float(range_pct) / 100.0)

        required = None
        if entry and target and target > entry and delta > 0:
            required = (target - entry) / max(delta, 0.10)
        elif expected_move_check and expected_move_check.get("requiredMovePoints"):
            required = float(expected_move_check["requiredMovePoints"])

        coverage_iv = self._coverage(expected_from_iv, required)
        coverage_atr = self._coverage(expected_from_atr, required)
        coverage_range = self._coverage(expected_from_range, required)
        coverages = [value for value in [coverage_iv, coverage_atr, coverage_range] if value is not None]
        best = max(coverages) if coverages else None
        if best is None:
            label = "unknown"
        elif best >= 1.10:
            label = "strong"
        elif best >= settings.vol_edge_min_expected_move_coverage:
            label = "acceptable"
        else:
            label = "weak"
        return {
            "expected_move_from_iv": self._round(expected_from_iv),
            "expected_move_from_atr": self._round(expected_from_atr),
            "expected_move_from_recent_range": self._round(expected_from_range),
            "required_move_for_target": self._round(required),
            "expected_move_coverage_iv": self._round(coverage_iv),
            "expected_move_coverage_atr": self._round(coverage_atr),
            "expected_move_coverage_range": self._round(coverage_range),
            "best_expected_move_coverage": self._round(best),
            "label": label,
        }

    def _iv_vs_realized(self, selected_iv: float | None, vix: float | None, realized_volatility: float | None) -> dict[str, Any]:
        iv = selected_iv if selected_iv is not None else vix
        if iv is None or realized_volatility is None or realized_volatility <= 0:
            return {"iv_to_rv_ratio": None, "iv_minus_rv": None, "label": "UNKNOWN_DATA_MISSING"}
        ratio = iv / realized_volatility
        if ratio > settings.vol_edge_max_iv_to_rv_ratio_for_buy:
            label = "IV_TOO_EXPENSIVE"
        elif ratio < 0.85:
            label = "IV_CHEAP_RELATIVE_TO_RV"
        else:
            label = "IV_REASONABLE"
        return {"iv_to_rv_ratio": self._round(ratio), "iv_minus_rv": self._round(iv - realized_volatility), "label": label}

    def _score(
        self,
        iv_vs_rv: dict[str, Any],
        iv_history: dict[str, Any],
        expansion: dict[str, Any],
        expected_move: dict[str, Any],
    ) -> tuple[int, list[str]]:
        score = 50
        reasons: list[str] = []
        label = iv_vs_rv["label"]
        if label == "IV_CHEAP_RELATIVE_TO_RV":
            score += 15
            reasons.append("IV is cheap relative to realized volatility")
        elif label == "IV_REASONABLE":
            score += 8
        elif label == "IV_TOO_EXPENSIVE":
            score -= 20
            reasons.append("IV is expensive relative to realized volatility")

        if expansion["iv_expansion_supported"]:
            score += 12
            reasons.append("volatility expansion supports option buying")
        if expansion["iv_contraction_risk"]:
            score -= 10
            reasons.append("IV or VIX contraction risk is present")

        if self._iv_crush_risk(iv_vs_rv, iv_history, expansion):
            score -= 20
            reasons.append("IV crush risk is elevated")

        move_label = expected_move["label"]
        if move_label == "strong":
            score += 12
        elif move_label == "acceptable":
            score += 5
        elif move_label == "weak":
            score -= 15
            reasons.append("expected move coverage is weak for target 1")

        return max(0, min(100, int(round(score)))), reasons

    def _classification(
        self,
        iv_vs_rv: dict[str, Any],
        iv_history: dict[str, Any],
        expansion: dict[str, Any],
        expected_move: dict[str, Any],
        reasons: list[str],
        score: int,
    ) -> str:
        if self._has_unknown_only_block(reasons):
            return "unknown_data_missing"
        if self._iv_crush_risk(iv_vs_rv, iv_history, expansion):
            return "iv_crush_risk"
        if expected_move["label"] == "weak":
            return "insufficient_expected_move"
        if expansion["iv_expansion_supported"] and iv_vs_rv["label"] != "IV_TOO_EXPENSIVE":
            return "iv_expansion_supported"
        if iv_vs_rv["label"] == "IV_TOO_EXPENSIVE":
            return "expensive"
        if iv_vs_rv["label"] == "IV_CHEAP_RELATIVE_TO_RV":
            return "cheap_relative_to_realized"
        if score < settings.min_volatility_edge_score:
            return "not_suitable_for_option_buying"
        return "fair"

    def _edge_label(self, classification: str, score: int, expected_move: dict[str, Any]) -> tuple[str, str]:
        if classification == "unknown_data_missing":
            return "unknown", "data_missing"
        if classification in {"iv_crush_risk", "expensive"}:
            return "unfavorable", "iv_crush" if classification == "iv_crush_risk" else "overpriced_premium"
        if classification == "insufficient_expected_move":
            return "unfavorable", "insufficient_realized_move"
        if classification == "not_suitable_for_option_buying":
            return "unfavorable", "low_volatility"
        if score >= 65 and expected_move["label"] in {"acceptable", "strong"}:
            return "favorable", "none"
        return "neutral", "none"

    def _iv_crush_risk(self, iv_vs_rv: dict[str, Any], iv_history: dict[str, Any], expansion: dict[str, Any]) -> bool:
        iv_rank = iv_history.get("iv_rank")
        high_rank = iv_rank is not None and float(iv_rank) >= settings.vol_edge_iv_crush_warning_threshold
        expensive = iv_vs_rv.get("label") == "IV_TOO_EXPENSIVE"
        return bool((high_rank or expensive) and not expansion.get("iv_expansion_supported"))

    def _has_unknown_only_block(self, reasons: list[str]) -> bool:
        return any(str(reason).startswith(self.UNKNOWN_DATA_MISSING) for reason in reasons)

    def _selected_iv(self, option_quality: dict[str, Any] | None) -> float | None:
        return self._normalize_iv(self._nested_float(option_quality, "details", "greeks", "implied_volatility"))

    def _normalize_iv(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            iv = float(value)
        except (TypeError, ValueError):
            return None
        if iv <= 0:
            return None
        if iv <= 3.0:
            iv *= 100.0
        return iv

    def _snapshot_price(self, snapshot: dict[str, Any] | None) -> float | None:
        if not isinstance(snapshot, dict):
            return None
        return self._safe_float(snapshot.get("price") or snapshot.get("last_price"))

    def _coverage(self, expected_move: float | None, required_move: float | None) -> float | None:
        if expected_move is None or required_move is None or required_move <= 0:
            return None
        return expected_move / required_move

    def _nested_float(self, value: dict[str, Any] | None, *keys: str) -> float | None:
        current: Any = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return self._safe_float(current)

    def _dict_float(self, value: dict[str, float] | None, key: str) -> float | None:
        if not isinstance(value, dict):
            return None
        return self._safe_float(value.get(key))

    def _safe_float(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(result):
            return None
        return result

    def _round(self, value: float | None) -> float | None:
        if value is None:
            return None
        return round(float(value), 4)
