from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import settings
from app.services.database import Candle, OptionQuoteSnapshot, get_session
from app.services.time_utils import ist_now_naive, ist_today, to_ist_naive
from app.services.trade_setup_service import OptionContract


class OptionPremiumConfirmationService:
    """Confirm that the selected option premium itself is participating in the move."""

    def __init__(self, websocket_price_feed: Any | None = None) -> None:
        self.websocket_price_feed = websocket_price_feed

    def evaluate(self, *, contract: OptionContract, side: str = "BUY", timeframe: str = "5minute") -> dict[str, Any]:
        if not settings.enable_option_premium_confirmation or side.upper() != "BUY":
            return {"enabled": False, "score": 100, "passed": True, "reasons": [], "details": {}}

        minimum = self._minimum_required()
        websocket_state = self._websocket_candle_state(contract)
        websocket_candles = self._recent_websocket_candles(contract, settings.option_premium_lookback_candles + 1)
        websocket_freshness = self._candle_freshness(websocket_candles, source="websocket_builder", symbol=contract.tradingsymbol)
        if len(websocket_candles) >= minimum and websocket_freshness["premium_candle_freshness_passed"]:
            return self._evaluate_candles(
                contract=contract,
                candles=websocket_candles,
                source="websocket_builder",
                freshness={**websocket_freshness, **websocket_state},
            )

        candles = self._recent_candles(contract.tradingsymbol, timeframe, settings.option_premium_lookback_candles + 1)
        stored_freshness = self._candle_freshness(candles, source="stored_candles", symbol=contract.tradingsymbol)
        if len(candles) >= minimum and stored_freshness["premium_candle_freshness_passed"]:
            return self._evaluate_candles(
                contract=contract,
                candles=candles,
                source="stored_candles",
                freshness={**stored_freshness, **websocket_state, "websocket_fallback": websocket_freshness},
            )

        snapshot_eval = self._evaluate_snapshots(contract, candle_count=len(candles), fallback_freshness=websocket_freshness)
        block_reason = self._premium_block_reason(contract, websocket_candles, websocket_state, websocket_freshness, stored_freshness, minimum)
        return self._stale_result(
            contract=contract,
            score=0,
            reasons=[block_reason, "premium_candles_stale_or_missing"],
            details={
                "source": "unavailable",
                "premium_candle_source": "unavailable",
                "tradingsymbol": contract.tradingsymbol,
                "candles": len(candles),
                "minimum_required_premium_candles": minimum,
                "current_session_candle_count": websocket_state.get("current_session_candle_count", 0),
                **stored_freshness,
                **websocket_state,
                "premium_confirmation_ready": False,
                "premium_confirmation_block_reason": block_reason,
                "websocket_fallback": websocket_freshness,
                "snapshot_diagnostics": snapshot_eval,
            },
        )

    def _evaluate_candles(self, *, contract: OptionContract, candles: list[Any], source: str, freshness: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        closes = [float(candle.close_price) for candle in candles]
        volumes = [float(candle.volume) for candle in candles]
        highs = [float(candle.high_price) for candle in candles]
        lows = [float(candle.low_price) for candle in candles]
        last_close = closes[-1]
        prev_close = closes[-2]
        recent_high = max(float(candle.high_price) for candle in candles[:-1])
        avg_volume = sum(volumes[:-1]) / max(len(volumes[:-1]), 1)
        total_volume = sum(volumes)
        if total_volume > 0:
            option_vwap = sum(((highs[idx] + lows[idx] + closes[idx]) / 3) * volumes[idx] for idx in range(len(closes))) / total_volume
        else:
            option_vwap = sum((highs[idx] + lows[idx] + closes[idx]) / 3 for idx in range(len(closes))) / max(len(closes), 1)
        premium_change_pct = ((last_close - closes[0]) / max(closes[0], 0.01)) * 100
        last_change_pct = ((last_close - prev_close) / max(prev_close, 0.01)) * 100
        breakout = last_close >= recent_high
        volume_expansion = volumes[-1] >= avg_volume * 1.10 if avg_volume > 0 else False
        spread_pct = self._spread_pct(contract)

        score = 20
        if premium_change_pct > 4:
            score += 25
        else:
            reasons.append("option premium momentum is weak")
        if last_change_pct > 0:
            score += 15
        else:
            reasons.append("latest option candle is not positive")
        if breakout:
            score += 20
        else:
            reasons.append("option premium has not broken recent high")
        if last_close >= option_vwap:
            score += 10
        else:
            reasons.append("option premium is below option VWAP")
        if volume_expansion:
            score += 10
        else:
            reasons.append("option premium volume expansion is weak")
        if spread_pct <= settings.max_bid_ask_spread_pct:
            score += 5
        else:
            reasons.append("selected option spread is not suitable for premium confirmation")

        score = min(100, score)
        participation_confirmed = premium_change_pct > 2 and last_change_pct > 0 and (breakout or volume_expansion)
        passed = score >= settings.min_option_premium_confirmation_score and participation_confirmed
        if not participation_confirmed:
            reasons.append("selected option premium has not confirmed real participation")
        if not passed:
            reasons.append("option premium confirmation score is below threshold")

        return {
            "enabled": True,
            "score": score,
            "passed": passed,
            "reasons": list(dict.fromkeys(reasons)),
            "details": {
                "source": source,
                "premium_candle_source": source,
                "tradingsymbol": contract.tradingsymbol,
                "candles": len(candles),
                "first_timestamp": freshness["premium_first_timestamp"],
                "last_timestamp": freshness["premium_last_timestamp"],
                **freshness,
                "premium_confirmation_ready": passed,
                "premium_confirmation_block_reason": None if passed else "option_premium_confirmation_score_below_threshold",
                "first_close": round(closes[0], 2),
                "last_close": round(last_close, 2),
                "premium_change_pct": round(premium_change_pct, 2),
                "last_change_pct": round(last_change_pct, 2),
                "recent_high": round(recent_high, 2),
                "option_vwap": round(option_vwap, 2),
                "breakout": breakout,
                "volume_expansion": volume_expansion,
                "participation_confirmed": participation_confirmed,
                "spread_pct": round(spread_pct, 2),
            },
        }

    def _recent_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
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

    def _spread_pct(self, contract: OptionContract) -> float:
        if not contract.bid or not contract.ask or not contract.last_price:
            return 100.0
        return ((contract.ask - contract.bid) / max(contract.last_price, 0.01)) * 100

    def _evaluate_snapshots(self, contract: OptionContract, *, candle_count: int, fallback_freshness: dict[str, Any] | None = None) -> dict[str, Any]:
        snapshots = self._recent_snapshots(contract.tradingsymbol, settings.option_premium_lookback_candles + 1)
        if len(snapshots) < 3:
            return {
                "enabled": True,
                "score": 45,
                "passed": False,
                "reasons": ["premium_candles_stale_or_missing", "not enough option premium candles/snapshots for confirmation"],
                "details": {
                    "source": "snapshots",
                    "premium_candle_source": "snapshots",
                    "tradingsymbol": contract.tradingsymbol,
                    "candles": candle_count,
                    "snapshots": len(snapshots),
                    **self._missing_freshness("snapshots", "not_enough_snapshots"),
                    "websocket_fallback": fallback_freshness or {},
                },
            }
        freshness = self._snapshot_freshness(snapshots)
        if not freshness["premium_candle_freshness_passed"]:
            return self._stale_result(
                contract=contract,
                score=0,
                reasons=["premium_candles_stale_or_missing"],
                details={
                    "source": "snapshots",
                    "premium_candle_source": "snapshots",
                    "tradingsymbol": contract.tradingsymbol,
                    "candles": candle_count,
                    "snapshots": len(snapshots),
                    **freshness,
                    "websocket_fallback": fallback_freshness or {},
                },
            )
        prices = [float(item.last_price or 0) for item in snapshots if float(item.last_price or 0) > 0]
        if len(prices) < 3:
            return {
                "enabled": True,
                "score": 45,
                "passed": False,
                "reasons": ["premium_candles_stale_or_missing", "option premium snapshots have insufficient price data"],
                "details": {"tradingsymbol": contract.tradingsymbol, "snapshots": len(snapshots), **freshness},
            }
        first = prices[0]
        last = prices[-1]
        prev = prices[-2]
        premium_change_pct = ((last - first) / max(first, 0.01)) * 100
        last_change_pct = ((last - prev) / max(prev, 0.01)) * 100
        rising = premium_change_pct > 2 and last_change_pct > 0
        spread_pct = self._spread_pct(contract)
        score = 35
        reasons: list[str] = []
        if rising:
            score += 35
        else:
            reasons.append("option premium snapshots do not show rising momentum")
        if spread_pct <= settings.max_bid_ask_spread_pct:
            score += 15
        else:
            reasons.append("selected option spread is not suitable for premium confirmation")
        score = min(100, score)
        passed = False
        reasons.append("option premium snapshots are diagnostic only for premium confirmation")
        if score < settings.min_option_premium_confirmation_score or not rising:
            reasons.append("option premium confirmation score is below threshold")
        return {
            "enabled": True,
            "score": score,
            "passed": passed,
            "reasons": list(dict.fromkeys(reasons)),
            "details": {
                "tradingsymbol": contract.tradingsymbol,
                "source": "snapshots",
                "premium_candle_source": "snapshots",
                "candles": candle_count,
                "snapshots": len(snapshots),
                **freshness,
                "first_price": round(first, 2),
                "last_price": round(last, 2),
                "premium_change_pct": round(premium_change_pct, 2),
                "last_change_pct": round(last_change_pct, 2),
                "participation_confirmed": rising,
                "spread_pct": round(spread_pct, 2),
            },
        }

    def _recent_snapshots(self, symbol: str, limit: int) -> list[OptionQuoteSnapshot]:
        session = get_session()
        try:
            rows = (
                session.query(OptionQuoteSnapshot)
                .filter(OptionQuoteSnapshot.tradingsymbol == symbol)
                .order_by(OptionQuoteSnapshot.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()

    def _stale_result(self, *, contract: OptionContract, score: int, reasons: list[str], details: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": True,
            "score": score,
            "passed": False,
            "reasons": list(dict.fromkeys(reasons + ["option premium confirmation score is below threshold"])),
            "details": details,
        }

    def _recent_websocket_candles(self, contract: OptionContract, limit: int) -> list[Any]:
        if self.websocket_price_feed is None or not contract.instrument_token:
            return []
        getter = getattr(self.websocket_price_feed, "get_current_session_premium_candles", None)
        if not callable(getter):
            getter = getattr(self.websocket_price_feed, "get_recent_premium_candles", None)
        if not callable(getter):
            return []
        try:
            return list(getter(int(contract.instrument_token), limit=limit))
        except Exception:
            return []

    def _candle_freshness(self, candles: list[Any], source: str = "stored_candles", symbol: str | None = None) -> dict[str, Any]:
        if not candles:
            return self._missing_freshness(source, "missing_candles")
        first = self._as_ist_naive(candles[0].timestamp)
        last = self._as_ist_naive(candles[-1].timestamp)
        return self._freshness_payload(source=source, first=first, last=last, symbol=symbol or getattr(candles[-1], "symbol", None))

    def _snapshot_freshness(self, snapshots: list[OptionQuoteSnapshot]) -> dict[str, Any]:
        if not snapshots:
            return self._missing_freshness("snapshots", "missing_snapshots")
        first = self._as_ist_naive(snapshots[0].timestamp)
        last = self._as_ist_naive(snapshots[-1].timestamp)
        return self._freshness_payload(source="snapshots", first=first, last=last, symbol=None)

    def _freshness_payload(self, *, source: str, first: datetime | None, last: datetime | None, symbol: str | None = None) -> dict[str, Any]:
        now = ist_now_naive()
        session_date = ist_today()
        age = max(0.0, (now - last).total_seconds()) if last else None
        candle_date = last.date().isoformat() if last else None
        current_date = session_date.isoformat()
        max_age = self._max_age_seconds(source)
        passed = bool(last and last.date() == session_date and age is not None and age <= max_age)
        reason = None
        if last is None:
            reason = "premium_candles_stale_or_missing"
        elif last.date() != session_date:
            reason = "premium_candles_stale_or_missing"
        elif age is not None and age > max_age:
            reason = "premium_candles_stale_or_missing"
        payload = {
            "option_candle_ingestion_enabled": settings.automation_intraday_candle_sync,
            "premium_candle_source": source,
            "premium_first_timestamp": first.isoformat(sep=" ") if first else None,
            "premium_last_timestamp": last.isoformat(sep=" ") if last else None,
            "premium_candle_age_seconds": round(age, 3) if age is not None else None,
            "premium_candle_session_date": candle_date,
            "current_market_session_date": current_date,
            "premium_candle_freshness_passed": passed,
            "premium_candle_rejection_reason": reason,
            "premium_candle_max_age_seconds": max_age,
            "selected_option_candle_source": source,
            "selected_option_last_candle_age_seconds": round(age, 3) if age is not None else None,
        }
        if symbol:
            payload.update(self._current_session_candle_status(symbol))
        else:
            payload.update(
                {
                    "latest_current_session_option_candle": last.isoformat(sep=" ") if last and last.date() == session_date else None,
                    "selected_option_candle_count_today": None,
                }
            )
        return payload

    def _missing_freshness(self, source: str, reason: str) -> dict[str, Any]:
        return {
            "option_candle_ingestion_enabled": settings.automation_intraday_candle_sync,
            "premium_candle_source": source,
            "premium_first_timestamp": None,
            "premium_last_timestamp": None,
            "premium_candle_age_seconds": None,
            "premium_candle_session_date": None,
            "current_market_session_date": ist_today().isoformat(),
            "premium_candle_freshness_passed": False,
            "premium_candle_rejection_reason": "premium_candles_stale_or_missing",
            "premium_candle_missing_reason": reason,
            "premium_candle_max_age_seconds": self._max_age_seconds(source),
            "latest_current_session_option_candle": None,
            "selected_option_candle_count_today": 0,
            "selected_option_last_candle_age_seconds": None,
            "selected_option_candle_source": "unavailable" if source in {"stored_candles", "snapshots"} else source,
        }

    def _as_ist_naive(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return to_ist_naive(value)

    def _current_session_candle_status(self, symbol: str) -> dict[str, Any]:
        session = get_session()
        try:
            today = ist_today()
            start = datetime.combine(today, datetime.min.time())
            end = datetime.combine(today, datetime.max.time())
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.timestamp >= start, Candle.timestamp <= end)
                .order_by(Candle.timestamp.desc())
                .all()
            )
            latest = self._as_ist_naive(rows[0].timestamp) if rows else None
            age = max(0.0, (ist_now_naive() - latest).total_seconds()) if latest else None
            return {
                "latest_current_session_option_candle": latest.isoformat(sep=" ") if latest else None,
                "selected_option_candle_count_today": len(rows),
                "selected_option_last_candle_age_seconds": round(age, 3) if age is not None else None,
            }
        finally:
            session.close()

    def _minimum_required(self) -> int:
        return max(3, int(settings.min_websocket_premium_candles), settings.option_premium_lookback_candles // 2)

    def _max_age_seconds(self, source: str) -> int:
        if source == "websocket_builder":
            return settings.max_websocket_premium_candle_age_seconds
        if source == "stored_candles":
            return settings.max_stored_premium_candle_age_seconds
        return settings.max_premium_confirmation_candle_age_seconds

    def _websocket_candle_state(self, contract: OptionContract) -> dict[str, Any]:
        token = int(contract.instrument_token or 0)
        state = {
            "selected_option_subscribed_for_candles": False,
            "selected_option_subscription_status": "token_missing" if not token else "websocket_unavailable",
            "websocket_ticks_seen_for_selected_option": 0,
            "websocket_candles_built_for_selected_option": 0,
            "current_session_candle_count": 0,
            "minimum_required_premium_candles": self._minimum_required(),
            "current_building_candle": None,
            "last_completed_candle": None,
            "premium_confirmation_ready": False,
            "premium_confirmation_block_reason": None,
            "candle_builder_warmup_seconds": None,
        }
        if self.websocket_price_feed is None or not token:
            return state
        status_getter = getattr(self.websocket_price_feed, "premium_candle_status", None)
        if callable(status_getter):
            try:
                status = dict(status_getter(token))
                count = int(status.get("current_session_candle_count") or 0)
                subscribed = bool(status.get("subscribed"))
                ready = subscribed and count >= self._minimum_required()
                state.update(
                    {
                        "selected_option_subscribed_for_candles": subscribed,
                        "selected_option_subscription_status": "subscribed" if subscribed else "selected_option_not_subscribed_for_candles",
                        "websocket_ticks_seen_for_selected_option": int(status.get("ticks_seen") or 0),
                        "websocket_candles_built_for_selected_option": count,
                        "current_session_candle_count": count,
                        "current_building_candle": status.get("current_building_candle"),
                        "last_completed_candle": status.get("last_completed_candle"),
                        "selected_option_last_candle_age_seconds": status.get("last_candle_age_seconds"),
                        "premium_confirmation_ready": ready,
                        "candle_builder_warmup_seconds": self._warmup_seconds(count),
                    }
                )
            except Exception:
                state["selected_option_subscription_status"] = "websocket_status_unavailable"
        return state

    def _premium_block_reason(
        self,
        contract: OptionContract,
        websocket_candles: list[Any],
        websocket_state: dict[str, Any],
        websocket_freshness: dict[str, Any],
        stored_freshness: dict[str, Any],
        minimum: int,
    ) -> str:
        if not contract.instrument_token:
            return "selected_option_not_subscribed_for_candles"
        if not websocket_state.get("selected_option_subscribed_for_candles"):
            return "selected_option_not_subscribed_for_candles"
        ticks_seen = int(websocket_state.get("websocket_ticks_seen_for_selected_option") or 0)
        built = int(websocket_state.get("websocket_candles_built_for_selected_option") or 0)
        if ticks_seen > 0 and built == 0:
            return "websocket_ticks_available_but_no_candles_built"
        if 0 < built < minimum:
            return "premium_candle_builder_warming_up"
        if len(websocket_candles) < minimum:
            return "insufficient_current_session_premium_candles"
        if not websocket_freshness.get("premium_candle_freshness_passed") or not stored_freshness.get("premium_candle_freshness_passed"):
            return "premium_candles_stale_or_missing"
        return "premium_candles_stale_or_missing"

    def _warmup_seconds(self, current_count: int) -> int:
        missing = max(0, self._minimum_required() - int(current_count or 0))
        return missing * 60
