from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Callable

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.database import RejectedOpportunityRecord
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.time_utils import ist_today


KiteProviderFactory = Callable[[], KiteProvider]


class RejectedOpportunityOutcomeService:
    """Evaluate whether rejected setups would later have hit target or stop."""

    def __init__(
        self,
        repository: RejectedOpportunityRepository,
        kite_provider_factory: KiteProviderFactory,
        market_data_coordinator: MarketDataCoordinator | None = None,
    ) -> None:
        self.repository = repository
        self.kite_provider_factory = kite_provider_factory
        self.market_data_coordinator = market_data_coordinator
        self.last_result: dict[str, Any] | None = None

    def evaluate_once(self, *, symbol: str | None = "BANKNIFTY", limit: int = 100) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        rows = self.repository.list_pending_later_outcomes(symbol=symbol, limit=limit)
        results = [self._evaluate_record(provider, row) for row in rows]
        self.last_result = {
            "evaluated": len(results),
            "updated": len([item for item in results if item.get("updated")]),
            "results": results,
        }
        return self.last_result

    def _evaluate_record(self, provider: KiteProvider, record: RejectedOpportunityRecord) -> dict[str, Any]:
        prices = self._planned_prices(record)
        if not prices:
            return {"id": record.id, "updated": False, "reason": "planned_prices_unavailable"}

        current_price = self._current_option_price(provider, record)
        if current_price is None:
            expired_price = self._expired_price(record)
            if expired_price is None:
                return {"id": record.id, "updated": False, "reason": "quote_unavailable"}
            current_price = expired_price

        outcome = self._outcome_for_price(record, current_price, prices)
        if outcome is None:
            return {
                "id": record.id,
                "updated": False,
                "tradingsymbol": record.tradingsymbol,
                "current_price": current_price,
                "reason": "no_later_outcome_yet",
            }

        updated = self.repository.mark_later_outcome(
            int(record.id),
            outcome=outcome,
            exit_price=current_price,
            notes=self._notes(record, outcome, current_price, prices),
        )
        return {
            "id": updated.id,
            "updated": True,
            "tradingsymbol": updated.tradingsymbol,
            "later_outcome": updated.later_outcome,
            "later_exit_price": updated.later_exit_price,
        }

    def _planned_prices(self, record: RejectedOpportunityRecord) -> dict[str, float]:
        factors = self._json(record.factor_scores_json)
        prices = factors.get("prices", {}) if isinstance(factors.get("prices"), dict) else {}
        parsed: dict[str, float] = {}
        for key in ("entry_price", "stop_loss", "target_1", "target_2", "target_3"):
            value = self._float(prices.get(key))
            if value is not None:
                parsed[key] = value
        return parsed

    def _current_option_price(self, provider: KiteProvider, record: RejectedOpportunityRecord) -> float | None:
        if not record.tradingsymbol:
            return None
        instrument = f"{record.exchange or settings.option_exchange}:{record.tradingsymbol}"
        try:
            if self.market_data_coordinator is not None:
                quote = self.market_data_coordinator.quote([instrument], provider=provider)
            else:
                quote = provider.quote([instrument])
        except Exception:
            return None
        data = quote.get(instrument) or quote.get(record.tradingsymbol) or {}
        if not isinstance(data, dict):
            return None
        return self._float(data.get("last_price") or data.get("last_traded_price"))

    def _expired_price(self, record: RejectedOpportunityRecord) -> float | None:
        expiry = self._parse_date(record.expiry)
        if expiry is None or expiry >= ist_today():
            return None
        return 0.0 if str(record.side).upper() == "BUY" else None

    def _outcome_for_price(self, record: RejectedOpportunityRecord, price: float, prices: dict[str, float]) -> str | None:
        expiry = self._parse_date(record.expiry)
        if expiry is not None and expiry < ist_today():
            return "would_have_expired"

        side = str(record.side or "BUY").upper()
        if side == "SELL":
            if prices.get("stop_loss") is not None and price >= prices["stop_loss"]:
                return "would_have_hit_stop_loss"
            for key in ("target_3", "target_2", "target_1"):
                if prices.get(key) is not None and price <= prices[key]:
                    return f"would_have_hit_{key}"
            return None

        if prices.get("stop_loss") is not None and price <= prices["stop_loss"]:
            return "would_have_hit_stop_loss"
        for key in ("target_3", "target_2", "target_1"):
            if prices.get(key) is not None and price >= prices[key]:
                return f"would_have_hit_{key}"
        return None

    def _notes(self, record: RejectedOpportunityRecord, outcome: str, exit_price: float, prices: dict[str, float]) -> str:
        primary_gate = record.primary_gate or "unknown"
        return (
            f"Auto-evaluated rejected setup; outcome={outcome}; "
            f"primary_gate={primary_gate}; exit_price={exit_price}; planned_prices={prices}"
        )

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _float(self, value: Any) -> float | None:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            return None
        return None

    def _parse_date(self, value: Any) -> date | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            return None
