from __future__ import annotations

from datetime import datetime, time
from typing import Any

from app.config import settings
from app.services.database import Candle, get_session
from app.services.time_utils import ist_now_naive, ist_today
from app.services.trade_setup_service import OptionContract


class BankNiftyIntelligenceService:
    """Bank Nifty-specific quality filters around the generic scanner flow."""

    TOP_BANKS = [
        {"symbol": "HDFCBANK", "name": "HDFC Bank", "weight": 0.29, "group": "private"},
        {"symbol": "ICICIBANK", "name": "ICICI Bank", "weight": 0.24, "group": "private"},
        {"symbol": "SBIN", "name": "SBI", "weight": 0.10, "group": "psu"},
        {"symbol": "AXISBANK", "name": "Axis Bank", "weight": 0.09, "group": "private"},
        {"symbol": "KOTAKBANK", "name": "Kotak Bank", "weight": 0.08, "group": "private"},
        {"symbol": "INDUSINDBK", "name": "IndusInd Bank", "weight": 0.03, "group": "private"},
        {"symbol": "BANKBARODA", "name": "Bank of Baroda", "weight": 0.03, "group": "psu"},
        {"symbol": "PNB", "name": "PNB", "weight": 0.02, "group": "psu"},
        {"symbol": "CANBK", "name": "Canara Bank", "weight": 0.02, "group": "psu"},
    ]

    def evaluate(
        self,
        *,
        trend: str,
        snapshot: dict[str, Any],
        market_snapshots: dict[str, dict[str, Any]],
        contract: OptionContract,
        chain_contracts: list[OptionContract],
        prices: dict[str, float],
        premium_eval: dict[str, Any],
        day_type_eval: dict[str, Any],
    ) -> dict[str, Any]:
        if not settings.enable_banknifty_intelligence:
            return {"enabled": False, "score": 100, "passed": True, "hard_reasons": [], "soft_reasons": [], "details": {}}

        bullish = trend.lower() == "bullish"
        top_banks = self._top_bank_alignment(bullish, market_snapshots)
        private_psu = self._private_psu_strength(top_banks)
        relative = self._relative_strength(bullish, market_snapshots)
        opening = self._opening_range_status(bullish, float(snapshot.get("price") or 0.0))
        expected_move = self._expected_move_check(bullish=bullish, snapshot=snapshot, contract=contract, prices=prices)
        dte = self._dte_mode(contract.expiry)
        event = self._event_day_mode()
        zone = self._round_zone(float(snapshot.get("price") or 0.0), bullish)
        near_atm = self._near_atm_pressure(float(snapshot.get("price") or 0.0), bullish, chain_contracts, contract)
        day = self._day_quality(day_type_eval, snapshot, top_banks, premium_eval)

        hard_reasons: list[str] = []
        soft_reasons: list[str] = []
        if top_banks["available"] >= 3:
            if top_banks["alignment"] < settings.banknifty_top_bank_min_alignment:
                hard_reasons.append("top banks are mixed against Bank Nifty direction")
            if top_banks["direction_count"] < settings.banknifty_top_bank_min_direction_count:
                hard_reasons.append("not enough top banks support the trade direction")
            if top_banks["one_bank_pull"]:
                soft_reasons.append("Bank Nifty move appears concentrated in one heavyweight")
            if private_psu["hdfcIciciCombinedImpact"] < -0.05:
                hard_reasons.append("HDFC and ICICI are opposite to the trade direction")
        else:
            soft_reasons.append("top bank constituent live data is incomplete")

        if relative["extreme_against"]:
            hard_reasons.append(relative["reason"])
        elif not relative["supports"]:
            soft_reasons.append(relative["reason"])

        if opening["status"] in {"pre_opening_range_complete", "inside_opening_range"}:
            hard_reasons.append(opening["reason"])
        if opening["status"] in {"failed_breakout", "failed_breakdown"}:
            soft_reasons.append(opening["reason"])

        premium_details = premium_eval.get("details", {}) if isinstance(premium_eval.get("details"), dict) else {}
        if settings.enable_option_premium_confirmation and not premium_eval.get("passed", False):
            hard_reasons.extend(str(reason) for reason in premium_eval.get("reasons", ["option premium is not expanding"]))
        if premium_details.get("option_vwap") and premium_details.get("last_close") and float(premium_details["last_close"]) < float(premium_details["option_vwap"]):
            hard_reasons.append("option premium is below option VWAP")

        if not expected_move["passed"]:
            soft_reasons.append(expected_move["reason"])
        if dte["risk"] == "near_expiry" and not premium_eval.get("passed", False):
            hard_reasons.append("near-expiry option buying needs strong premium expansion")
        if dte["risk"] == "far_expiry" and expected_move["coverage"] < 1.25:
            soft_reasons.append("far-expiry option needs stronger move to justify premium")
        if event["is_event_day"] and not event["post_event_confirmation_window"]:
            soft_reasons.append("event day requires post-event confirmation")
        if zone["zoneRisk"] == "trapped":
            soft_reasons.append("Bank Nifty is near a major round-number zone; require breakout confirmation")
        elif zone["zoneRisk"] != "clear":
            soft_reasons.append(zone["reason"])
        if not near_atm["supports"]:
            soft_reasons.append(near_atm["reason"])
        if day["dayType"] in {"range", "choppy_no_trade"}:
            hard_reasons.append(day["dayTypeReason"])

        score = self._score(top_banks, private_psu, relative, opening, expected_move, dte, event, zone, near_atm, day, premium_eval)
        trade_quality = self._trade_quality(score, hard_reasons, soft_reasons)
        return {
            "enabled": True,
            "score": score,
            "passed": not hard_reasons,
            "hard_reasons": list(dict.fromkeys(hard_reasons)),
            "soft_reasons": list(dict.fromkeys(soft_reasons)),
            "details": {
                "bankNiftySpecificScore": score,
                "topBankAlignment": top_banks,
                "privateBankStrength": private_psu["privateBankStrength"],
                "psuBankStrength": private_psu["psuBankStrength"],
                "privateVsPsuAgreement": private_psu["privateVsPsuAgreement"],
                "sbiStandaloneImpact": private_psu["sbiStandaloneImpact"],
                "hdfcIciciCombinedImpact": private_psu["hdfcIciciCombinedImpact"],
                "relativeStrengthVsNifty": relative,
                "openingRangeStatus": opening,
                "optionPremiumConfirmation": premium_eval,
                "expectedMoveCheck": expected_move,
                "dteMode": dte,
                "eventDayMode": event,
                "nearestMajorZone": zone,
                "optionChainNearAtmSignal": near_atm,
                "dayType": day["dayType"],
                "dayTypeReason": day["dayTypeReason"],
                "noTradeReasons": list(dict.fromkeys(hard_reasons)),
                "tradeQuality": trade_quality,
                "confidenceReason": self._confidence_reason(trade_quality, hard_reasons, soft_reasons),
                "invalidationReason": "; ".join(hard_reasons[:3]) if hard_reasons else "",
            },
        }

    def _top_bank_alignment(self, bullish: bool, market_snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        support_weight = 0.0
        against_weight = 0.0
        available_weight = 0.0
        direction_count = 0
        opposite_count = 0
        for bank in self.TOP_BANKS:
            snapshot = market_snapshots.get(str(bank["symbol"]), {})
            move = self._pct_move(snapshot)
            if move is None:
                continue
            weight = float(bank["weight"])
            supports = move > 0.05 if bullish else move < -0.05
            opposes = move < -0.05 if bullish else move > 0.05
            contribution = move * weight
            rows.append({**bank, "move_pct": round(move, 3), "supports": supports, "contribution": round(contribution, 4)})
            available_weight += weight
            if supports:
                direction_count += 1
                support_weight += weight
            elif opposes:
                opposite_count += 1
                against_weight += weight
        alignment = support_weight / available_weight if available_weight else 0.0
        top_contribution = max((abs(float(row["contribution"])) for row in rows), default=0.0)
        total_contribution = sum(abs(float(row["contribution"])) for row in rows)
        return {
            "available": len(rows),
            "alignment": round(alignment, 3),
            "weightedDirectionalContribution": round(sum(float(row["contribution"]) for row in rows), 4),
            "direction_count": direction_count,
            "opposite_count": opposite_count,
            "broad_based": direction_count >= settings.banknifty_top_bank_min_direction_count and alignment >= settings.banknifty_top_bank_min_alignment,
            "one_bank_pull": total_contribution > 0 and top_contribution / total_contribution > 0.55,
            "banks": rows,
        }

    def _private_psu_strength(self, top_banks: dict[str, Any]) -> dict[str, Any]:
        rows = top_banks.get("banks", [])
        private = [row for row in rows if row.get("group") == "private"]
        psu = [row for row in rows if row.get("group") == "psu"]
        private_strength = self._weighted_strength(private)
        psu_strength = self._weighted_strength(psu)
        sbi = next((float(row.get("move_pct") or 0.0) for row in rows if row.get("symbol") == "SBIN"), 0.0)
        hdfc_icici = sum(float(row.get("move_pct") or 0.0) * float(row.get("weight") or 0.0) for row in rows if row.get("symbol") in {"HDFCBANK", "ICICIBANK"})
        return {
            "privateBankStrength": round(private_strength, 3),
            "psuBankStrength": round(psu_strength, 3),
            "privateVsPsuAgreement": (private_strength >= 0 and psu_strength >= 0) or (private_strength <= 0 and psu_strength <= 0),
            "sbiStandaloneImpact": round(sbi, 3),
            "hdfcIciciCombinedImpact": round(hdfc_icici, 4),
        }

    def _relative_strength(self, bullish: bool, market_snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
        bank = self._pct_move(market_snapshots.get("BANKNIFTY", {}))
        nifty = self._pct_move(market_snapshots.get("NIFTY", {}))
        if bank is None or nifty is None:
            return {"available": False, "value": 0.0, "supports": True, "extreme_against": False, "reason": "relative strength data unavailable"}
        value = bank - nifty
        supports = value >= 0 if bullish else value <= 0
        extreme_against = value < -settings.banknifty_extreme_divergence_pct if bullish else value > settings.banknifty_extreme_divergence_pct
        return {
            "available": True,
            "bankNiftyMovePct": round(bank, 3),
            "niftyMovePct": round(nifty, 3),
            "value": round(value, 3),
            "supports": supports,
            "extreme_against": extreme_against,
            "reason": "Bank Nifty relative strength supports trade" if supports else "Bank Nifty relative strength diverges against trade",
        }

    def _opening_range_status(self, bullish: bool, price: float) -> dict[str, Any]:
        candles = self._today_candles("BANKNIFTY")
        if not candles:
            return {"status": "unavailable", "passed": True, "reason": "opening range candles unavailable"}
        now = ist_now_naive().time()
        first_trade = self._parse_time(settings.banknifty_first_trade_time)
        if now < first_trade:
            return {"status": "pre_opening_range_complete", "passed": False, "reason": "opening range is not complete"}
        opening = self._candles_between(candles, settings.banknifty_opening_range_start, settings.banknifty_opening_range_end)
        opening = opening or candles[:3]
        high = max(float(candle.high_price) for candle in opening)
        low = min(float(candle.low_price) for candle in opening)
        first = opening[0]
        body = abs(float(first.close_price) - float(first.open_price))
        wick = (float(first.high_price) - float(first.low_price)) - body
        last_close = price or float(candles[-1].close_price)
        previous = float(candles[-2].close_price) if len(candles) > 1 else last_close
        if low <= last_close <= high:
            status = "inside_opening_range"
            reason = "Bank Nifty is inside opening range"
        elif bullish and last_close > high:
            status = "breakout" if previous >= high or last_close > previous else "failed_breakout"
            reason = "opening range breakout confirmed" if status == "breakout" else "opening breakout is not holding"
        elif (not bullish) and last_close < low:
            status = "breakdown" if previous <= low or last_close < previous else "failed_breakdown"
            reason = "opening range breakdown confirmed" if status == "breakdown" else "opening breakdown is not holding"
        else:
            status = "opposite_side"
            reason = "Bank Nifty is on the wrong side of opening range"
        return {
            "status": status,
            "passed": status in {"breakout", "breakdown", "unavailable"},
            "openingRangeHigh": round(high, 2),
            "openingRangeLow": round(low, 2),
            "first15Body": round(body, 2),
            "first15Wick": round(max(wick, 0.0), 2),
            "largeWick": wick > body * 1.25 if body > 0 else True,
            "reason": reason,
        }

    def _expected_move_check(self, *, bullish: bool, snapshot: dict[str, Any], contract: OptionContract, prices: dict[str, float]) -> dict[str, Any]:
        candles = self._recent_candles("BANKNIFTY", limit=30)
        atr = self._atr(candles)
        price = float(snapshot.get("price") or 0.0)
        if price <= 0 or atr <= 0:
            atr = max(price * 0.004, 100.0)
        zone = self._round_zone(price, bullish)
        distance_to_zone = float(zone.get("distanceToZone") or atr)
        realistic_move = min(max(atr * 0.9, price * 0.0025), distance_to_zone if distance_to_zone > 0 else atr)
        delta = self._approx_delta(contract)
        required_move = max((float(prices["target_1"]) - float(prices["entry_price"])) / max(delta, 0.10), 1.0)
        coverage = realistic_move / required_move if required_move > 0 else 0.0
        passed = coverage >= settings.banknifty_expected_move_min_coverage
        return {
            "passed": passed,
            "expectedMovePoints": round(realistic_move, 2),
            "requiredMovePoints": round(required_move, 2),
            "coverage": round(coverage, 2),
            "approxDelta": round(delta, 2),
            "reason": "expected Bank Nifty move can justify option target" if passed else "expected move is smaller than option premium target requirement",
        }

    def _dte_mode(self, expiry: str) -> dict[str, Any]:
        try:
            expiry_date = datetime.fromisoformat(str(expiry)).date()
            dte = max(0, (expiry_date - ist_today()).days)
        except ValueError:
            dte = 999
        if dte <= 1:
            mode = "gamma_scalp"
            risk = "near_expiry"
        elif dte <= 5:
            mode = "momentum"
            risk = "normal"
        else:
            mode = "trend_continuation"
            risk = "far_expiry"
        return {"daysToExpiry": dte, "mode": mode, "risk": risk}

    def _event_day_mode(self) -> dict[str, Any]:
        today = ist_today().isoformat()
        events = [item.strip() for item in settings.banknifty_event_dates.split(",") if item.strip()]
        is_event = today in events
        after = ist_now_naive().time() >= self._parse_time(settings.banknifty_event_preferred_after_time)
        return {
            "is_event_day": is_event,
            "mode": "event_day" if is_event else "normal",
            "post_event_confirmation_window": (not is_event) or after,
            "configured_dates": events,
        }

    def _round_zone(self, price: float, bullish: bool) -> dict[str, Any]:
        if price <= 0:
            return {"nearestMajorZone": 0, "distanceToZone": 0, "zoneRisk": "unknown", "reason": "price unavailable"}
        major = settings.banknifty_major_zone_points
        lower = int(price // major) * major
        upper = lower + major
        target_zone = upper if bullish else lower
        distance = abs(target_zone - price)
        very_major = target_zone % settings.banknifty_very_major_zone_points == 0
        if distance <= settings.banknifty_zone_risk_points:
            zone_risk = "trapped" if very_major else "near_major_zone"
        elif (upper - lower) > 0 and min(price - lower, upper - price) / (upper - lower) < 0.18:
            zone_risk = "near_zone"
        else:
            zone_risk = "clear"
        return {
            "nearestMajorZone": target_zone,
            "distanceToZone": round(distance, 2),
            "zoneRisk": zone_risk,
            "veryMajorZone": very_major,
            "reason": "near Bank Nifty round-number supply/demand zone" if zone_risk != "clear" else "clear of immediate major round zone",
        }

    def _near_atm_pressure(self, spot: float, bullish: bool, contracts: list[OptionContract], selected: OptionContract) -> dict[str, Any]:
        if not contracts or spot <= 0:
            return {"supports": True, "score": 50, "reason": "near-ATM option-chain data unavailable", "strikes": []}
        step = self._strike_step(contracts)
        atm = round(spot / step) * step
        strikes = {atm - 2 * step, atm - step, atm, atm + step, atm + 2 * step}
        near = [contract for contract in contracts if contract.strike in strikes]
        calls = [contract for contract in near if contract.option_type == "CE"]
        puts = [contract for contract in near if contract.option_type == "PE"]
        call_oi = sum(contract.open_interest for contract in calls)
        put_oi = sum(contract.open_interest for contract in puts)
        put_bias = put_oi >= call_oi * 0.85 if call_oi else False
        call_bias = call_oi >= put_oi * 0.85 if put_oi else False
        selected_near_atm = abs(selected.strike - atm) <= 2 * step
        supports = (put_bias and bullish) or (call_bias and not bullish)
        score = 70 if supports and selected_near_atm else 45
        return {
            "supports": supports and selected_near_atm,
            "score": score,
            "atm": atm,
            "selectedNearAtm": selected_near_atm,
            "nearAtmCallOi": round(call_oi, 2),
            "nearAtmPutOi": round(put_oi, 2),
            "strikes": sorted(strikes),
            "reason": "near-ATM option-chain pressure supports trade" if supports else "near-ATM option-chain pressure is not supportive",
        }

    def _day_quality(self, day_type_eval: dict[str, Any], snapshot: dict[str, Any], top_banks: dict[str, Any], premium_eval: dict[str, Any]) -> dict[str, str]:
        details = day_type_eval.get("details", {}) if isinstance(day_type_eval.get("details"), dict) else {}
        day_type = str(details.get("day_type") or "unknown")
        price = float(snapshot.get("price") or 0.0)
        vwap = float(snapshot.get("vwap") or 0.0)
        near_vwap = price > 0 and vwap > 0 and abs(price - vwap) / price < 0.0015
        if near_vwap and not top_banks.get("broad_based") and not premium_eval.get("passed", False):
            return {"dayType": "choppy_no_trade", "dayTypeReason": "near flat VWAP, mixed top banks, and flat option premium"}
        if day_type in {"rotation_range", "range"}:
            return {"dayType": "range", "dayTypeReason": "range day blocks option buying"}
        return {"dayType": day_type, "dayTypeReason": "; ".join(str(reason) for reason in day_type_eval.get("reasons", [])) or "day type acceptable"}

    def _score(self, top_banks: dict[str, Any], private_psu: dict[str, Any], relative: dict[str, Any], opening: dict[str, Any], expected_move: dict[str, Any], dte: dict[str, Any], event: dict[str, Any], zone: dict[str, Any], near_atm: dict[str, Any], day: dict[str, str], premium_eval: dict[str, Any]) -> int:
        score = 35
        score += int(top_banks.get("alignment", 0) * 20)
        if top_banks.get("broad_based"):
            score += 10
        if private_psu.get("privateVsPsuAgreement"):
            score += 6
        if relative.get("supports"):
            score += 8
        if opening.get("passed"):
            score += 8
        if premium_eval.get("passed"):
            score += 12
        if expected_move.get("passed"):
            score += 10
        if near_atm.get("supports"):
            score += 5
        if day.get("dayType") in {"trend_expansion", "directional_acceptance"}:
            score += 8
        if dte.get("risk") in {"near_expiry", "far_expiry"}:
            score -= 4
        event_day = bool(event.get("is_event_day"))
        if event_day:
            score -= 5
        if zone.get("zoneRisk") != "clear":
            score -= 8
        if top_banks.get("one_bank_pull"):
            score -= 8
        capped = max(0, min(100, score))
        return min(capped, 88) if event_day else capped

    def _trade_quality(self, score: int, hard_reasons: list[str], soft_reasons: list[str]) -> str:
        if hard_reasons:
            return "NO_TRADE"
        if score >= 92 and not soft_reasons:
            return "A_PLUS"
        if score >= 82:
            return "A"
        if score >= 70:
            return "B"
        return "C"

    def _confidence_reason(self, quality: str, hard_reasons: list[str], soft_reasons: list[str]) -> str:
        if hard_reasons:
            return "No-trade: " + "; ".join(hard_reasons[:3])
        if soft_reasons:
            return f"{quality} setup with cautions: " + "; ".join(soft_reasons[:3])
        return f"{quality} setup: Bank Nifty-specific checks support the trade"

    def _pct_move(self, snapshot: dict[str, Any]) -> float | None:
        price = float(snapshot.get("price") or 0.0)
        previous = float(snapshot.get("previous_day_close") or snapshot.get("day_open") or 0.0)
        if price <= 0 or previous <= 0:
            return None
        return ((price - previous) / previous) * 100

    def _weighted_strength(self, rows: list[dict[str, Any]]) -> float:
        total_weight = sum(float(row.get("weight") or 0.0) for row in rows)
        if total_weight <= 0:
            return 0.0
        return sum(float(row.get("move_pct") or 0.0) * float(row.get("weight") or 0.0) for row in rows) / total_weight

    def _recent_candles(self, symbol: str, timeframe: str = "5minute", limit: int = 30) -> list[Candle]:
        session = get_session()
        try:
            return list(
                reversed(
                    session.query(Candle)
                    .filter(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe)
                    .order_by(Candle.timestamp.desc())
                    .limit(limit)
                    .all()
                )
            )
        finally:
            session.close()

    def _today_candles(self, symbol: str, timeframe: str = "5minute") -> list[Candle]:
        session = get_session()
        try:
            start = datetime.combine(ist_today(), time.min)
            end = datetime.combine(ist_today(), time.max)
            return (
                session.query(Candle)
                .filter(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe, Candle.timestamp >= start, Candle.timestamp <= end)
                .order_by(Candle.timestamp.asc())
                .all()
            )
        finally:
            session.close()

    def _candles_between(self, candles: list[Candle], start_text: str, end_text: str) -> list[Candle]:
        start = self._parse_time(start_text)
        end = self._parse_time(end_text)
        return [candle for candle in candles if start <= candle.timestamp.time() <= end]

    def _atr(self, candles: list[Candle]) -> float:
        if len(candles) < 2:
            return 0.0
        values: list[float] = []
        previous = float(candles[0].close_price)
        for candle in candles[1:]:
            high = float(candle.high_price)
            low = float(candle.low_price)
            values.append(max(high - low, abs(high - previous), abs(low - previous)))
            previous = float(candle.close_price)
        return sum(values[-14:]) / max(len(values[-14:]), 1)

    def _approx_delta(self, contract: OptionContract) -> float:
        # Conservative fallback when the latest quality/Greeks object is not passed into this service.
        return 0.50 if contract.option_type in {"CE", "PE"} else 0.40

    def _strike_step(self, contracts: list[OptionContract]) -> int:
        strikes = sorted({int(contract.strike) for contract in contracts if contract.strike > 0})
        diffs = [strikes[idx] - strikes[idx - 1] for idx in range(1, len(strikes)) if strikes[idx] > strikes[idx - 1]]
        return min(diffs) if diffs else 100

    def _parse_time(self, value: str) -> time:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
