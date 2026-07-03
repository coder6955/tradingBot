from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import settings
from app.services.time_utils import ist_now_naive
from app.services.trade_setup_service import TradeSetupService


class BankNiftyOptionPrewarmService:
    """Subscribe a tiny near-ATM Bank Nifty option set for premium candle warm-up."""

    def __init__(self, websocket_price_feed: Any | None = None, trade_setup_service: TradeSetupService | None = None) -> None:
        self.websocket_price_feed = websocket_price_feed
        self.trade_setup_service = trade_setup_service or TradeSetupService()
        self.last_refresh_at: datetime | None = None
        self.last_atm_strike: float | None = None
        self.last_tokens: set[int] = set()
        self.last_tradingsymbols: list[str] = []
        self.last_reason: str | None = None
        self.last_subscription: dict[str, Any] = {}

    def prewarm(self, *, spot_price: float, option_instruments: list[dict[str, Any]]) -> dict[str, Any]:
        if not settings.enable_banknifty_option_prewarm:
            self.last_reason = "prewarm_disabled"
            return self.status(extra={"refreshed": False})
        if self.websocket_price_feed is None or not settings.enable_kite_websocket:
            self.last_reason = "websocket_unavailable"
            return self.status(extra={"refreshed": False})
        if spot_price <= 0:
            self.last_reason = "spot_price_unavailable"
            return self.status(extra={"refreshed": False})

        candidates = self._candidates(spot_price=spot_price, option_instruments=option_instruments)
        tokens = {int(item["instrument_token"]) for item in candidates if self._safe_int(item.get("instrument_token"))}
        atm = self._atm_strike(candidates, spot_price)
        if not tokens:
            self.last_reason = "near_atm_candidates_unavailable"
            return self.status(extra={"refreshed": False})

        if not self._refresh_due(atm):
            self.last_reason = "prewarm_already_current"
            return self.status(extra={"refreshed": False})

        subscribe = getattr(self.websocket_price_feed, "subscribe", None)
        if not callable(subscribe):
            self.last_reason = "websocket_subscribe_unavailable"
            return self.status(extra={"refreshed": False})
        self.last_subscription = dict(subscribe(tokens))
        self.last_refresh_at = ist_now_naive()
        self.last_atm_strike = atm
        self.last_tokens = set(tokens)
        self.last_tradingsymbols = [str(item.get("tradingsymbol")) for item in candidates if item.get("tradingsymbol")]
        self.last_reason = str(self.last_subscription.get("reason") or "prewarm_subscribed")
        return self.status(extra={"refreshed": True})

    def status(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "prewarm_enabled": settings.enable_banknifty_option_prewarm,
            "prewarm_tokens": sorted(self.last_tokens),
            "prewarm_tradingsymbols": list(self.last_tradingsymbols),
            "prewarm_last_refresh_at": self.last_refresh_at.isoformat(sep=" ") if self.last_refresh_at else None,
            "prewarm_reason": self.last_reason,
            "prewarm_atm_strike": self.last_atm_strike,
            "prewarm_subscription": self.last_subscription,
        }
        if extra:
            payload.update(extra)
        return payload

    def _candidates(self, *, spot_price: float, option_instruments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        expiry = self.trade_setup_service.nearest_expiry(option_instruments, "BANKNIFTY")
        if expiry is None:
            return []
        contracts = [
            item
            for item in option_instruments
            if self._banknifty_option(item)
            and str(item.get("expiry")) == expiry
            and self._safe_float(item.get("strike")) > 0
            and self._safe_int(item.get("instrument_token")) is not None
        ]
        if not contracts:
            return []
        strikes = sorted({float(item.get("strike") or 0.0) for item in contracts})
        atm = min(strikes, key=lambda strike: abs(strike - spot_price))
        idx = strikes.index(atm)
        depth = max(0, int(settings.banknifty_prewarm_strike_depth))
        wanted_strikes = set(strikes[max(0, idx - depth): min(len(strikes), idx + depth + 1)])
        wanted: list[dict[str, Any]] = []
        seen: set[tuple[float, str]] = set()
        for item in contracts:
            strike = float(item.get("strike") or 0.0)
            option_type = str(item.get("instrument_type") or "").upper()
            key = (strike, option_type)
            if strike in wanted_strikes and option_type in {"CE", "PE"} and key not in seen:
                wanted.append(item)
                seen.add(key)
        return sorted(wanted, key=lambda item: (float(item.get("strike") or 0.0), str(item.get("instrument_type") or "")))

    def _refresh_due(self, atm_strike: float | None) -> bool:
        if self.last_refresh_at is None or not self.last_tokens:
            return True
        if atm_strike is not None and self.last_atm_strike is not None and atm_strike != self.last_atm_strike:
            return True
        elapsed = (ist_now_naive() - self.last_refresh_at).total_seconds()
        return elapsed >= max(1, settings.banknifty_prewarm_refresh_seconds)

    def _atm_strike(self, candidates: list[dict[str, Any]], spot_price: float) -> float | None:
        strikes = sorted({float(item.get("strike") or 0.0) for item in candidates if self._safe_float(item.get("strike")) > 0})
        if not strikes:
            return None
        return min(strikes, key=lambda strike: abs(strike - spot_price))

    def _banknifty_option(self, item: dict[str, Any]) -> bool:
        name = str(item.get("name") or "").upper().replace(" ", "")
        tradingsymbol = str(item.get("tradingsymbol") or "").upper()
        return (name == "BANKNIFTY" or tradingsymbol.startswith("BANKNIFTY")) and str(item.get("instrument_type")) in {"CE", "PE"}

    def _safe_int(self, value: Any) -> int | None:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            return None
        return None

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
