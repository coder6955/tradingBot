from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.time_utils import to_ist_naive


class DataFreshnessService:
    """Fail closed when live trading would depend on stale or fallback data."""

    def validate_scan_inputs(
        self,
        *,
        order_mode: str,
        snapshot: dict[str, Any],
        chain_quotes: dict[str, Any],
        contract: Any | None,
    ) -> dict[str, Any]:
        live = str(order_mode).lower() == "live"
        reasons: list[str] = []
        checks: dict[str, Any] = {}

        source = str(snapshot.get("source") or "")
        is_real = bool(snapshot.get("is_real_data"))
        if live and (not is_real or source in {"mock", "fallback"}):
            reasons.append(f"live mode requires fresh Kite quote; source={source or 'unknown'}, is_real_data={is_real}")
        if live and not bool(snapshot.get("analysis_ready")):
            quality_reasons = snapshot.get("data_quality_reasons") or ["analysis snapshot unavailable"]
            reasons.append(f"live mode requires canonical completed-candle analysis: {quality_reasons}")

        quote_source = str(snapshot.get("quote_timestamp_source") or "unavailable")
        checks["banknifty_quote_timestamp_source"] = quote_source
        if live and quote_source not in {"exchange_timestamp", "last_trade_time", "timestamp"}:
            reasons.append(f"Bank Nifty quote lacks broker/exchange timestamp provenance: {quote_source}")

        quote_age = self._age_seconds(snapshot.get("quote_timestamp") or snapshot.get("timestamp"))
        checks["banknifty_quote_age_seconds"] = quote_age
        if live and quote_age is None:
            reasons.append("Bank Nifty quote timestamp is missing")
        elif live and quote_age is not None and quote_age > settings.max_live_quote_age_seconds:
            reasons.append(f"Bank Nifty quote is stale: {quote_age:.1f}s old")

        candle_age = self._latest_candle_age(snapshot)
        checks["banknifty_candle_age_seconds"] = candle_age
        max_candle_age = settings.max_live_candle_age_seconds if live else settings.max_paper_candle_age_seconds
        if live and candle_age is None:
            reasons.append("Bank Nifty candle timestamp is missing")
        elif live and candle_age is not None and candle_age > max_candle_age:
            reasons.append(f"Bank Nifty candle data is stale: {candle_age:.1f}s old")

        chain_age = self._quote_map_age(chain_quotes)
        checks["option_chain_age_seconds"] = chain_age
        if live and not chain_quotes:
            reasons.append("option-chain quote data is missing")
        elif live and chain_age is None:
            reasons.append("option-chain quote timestamps are missing")
        elif live and chain_age is not None and chain_age > settings.max_live_chain_age_seconds:
            reasons.append(f"option-chain quotes are stale: {chain_age:.1f}s old")

        option_age = self._selected_option_age(chain_quotes, contract)
        checks["selected_option_quote_age_seconds"] = option_age
        if live and contract is not None and option_age is None:
            reasons.append("selected option quote timestamp is missing")
        elif live and option_age is not None and option_age > settings.max_live_option_quote_age_seconds:
            reasons.append(f"selected option quote is stale: {option_age:.1f}s old")

        return {"passed": not reasons, "reasons": reasons, "checks": checks}

    def _latest_candle_age(self, snapshot: dict[str, Any]) -> float | None:
        candles = snapshot.get("candles")
        if isinstance(candles, list) and candles:
            last = candles[-1]
            if isinstance(last, dict):
                return self._age_seconds(last.get("date") or last.get("timestamp"))
        return self._age_seconds(snapshot.get("latest_candle_at") or snapshot.get("candle_timestamp"))

    def _selected_option_age(self, chain_quotes: dict[str, Any], contract: Any | None) -> float | None:
        if contract is None:
            return None
        key = f"{getattr(contract, 'exchange', settings.option_exchange)}:{getattr(contract, 'tradingsymbol', '')}"
        payload = chain_quotes.get(key) or chain_quotes.get(getattr(contract, "tradingsymbol", "")) or {}
        return self._quote_age(payload)

    def _quote_map_age(self, chain_quotes: dict[str, Any]) -> float | None:
        ages = [self._quote_age(value) for value in chain_quotes.values() if isinstance(value, dict)]
        ages = [age for age in ages if age is not None]
        return max(ages) if ages else None

    def _quote_age(self, payload: dict[str, Any]) -> float | None:
        source = str(payload.get("quote_timestamp_source") or "")
        if source and source not in {"exchange_timestamp", "last_trade_time", "timestamp"}:
            return None
        return self._age_seconds(
            payload.get("quote_timestamp")
            or payload.get("timestamp")
            or payload.get("last_trade_time")
            or payload.get("last_update_time")
        )

    def _age_seconds(self, value: Any) -> float | None:
        parsed = self._parse_dt(value)
        if parsed is None:
            return None
        now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        return max(0.0, (now - parsed).total_seconds())

    def _parse_dt(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return to_ist_naive(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return to_ist_naive(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
