from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Any, Callable

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.database import OpportunityRecord
from app.services.opportunity_repository import OpportunityRepository
from app.services.trade_exit_service import TradeExitService


KiteProviderFactory = Callable[[], KiteProvider]


class OpportunityOutcomeService:
    """Monitor open opportunities and classify target, stop, and failure outcomes."""

    def __init__(
        self,
        repository: OpportunityRepository,
        kite_provider_factory: KiteProviderFactory,
        trade_exit_service: TradeExitService | None = None,
    ) -> None:
        self.repository = repository
        self.kite_provider_factory = kite_provider_factory
        self.trade_exit_service = trade_exit_service
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.interval_seconds = 30
        self.last_run_at: str | None = None
        self.last_results: list[dict[str, Any]] = []
        self.last_trade_exit_result: dict[str, Any] | None = None
        self.errors: list[dict[str, Any]] = []

    def start(self, interval_seconds: int = 30) -> dict[str, Any]:
        if self.running:
            return self.status()
        self.interval_seconds = max(5, int(interval_seconds))
        self.running = True
        self.task = asyncio.create_task(self._run())
        return self.status()

    async def stop(self) -> dict[str, Any]:
        self.running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "last_run_at": self.last_run_at,
            "last_result_count": len(self.last_results),
            "last_trade_exit_result": self.last_trade_exit_result,
            "error_count": len(self.errors),
        }

    async def _run(self) -> None:
        while self.running:
            try:
                self.evaluate_once()
            except Exception as exc:
                self.errors.append({"time": datetime.utcnow().isoformat(), "error": str(exc)})
            await asyncio.sleep(float(self.interval_seconds))

    def evaluate_once(self, limit: int = 100) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        open_records = self.repository.list_opportunities(status="open", limit=limit)
        results: list[dict[str, Any]] = []
        for record in open_records:
            results.append(self._evaluate_record(provider, record))
        trade_exit_result = self.trade_exit_service.evaluate_once(limit=limit) if self.trade_exit_service else None
        self.last_run_at = datetime.utcnow().isoformat()
        self.last_results = results
        self.last_trade_exit_result = trade_exit_result
        return {
            "evaluated": len(results),
            "closed": len([item for item in results if item.get("closed")]),
            "results": results,
            "trade_exits": trade_exit_result,
        }

    def _evaluate_record(self, provider: KiteProvider, record: OpportunityRecord) -> dict[str, Any]:
        current_price = self._current_option_price(provider, record)
        if current_price is None:
            expired_result = self._expired_result(record)
            if expired_result is None:
                return {"id": record.id, "symbol": record.symbol, "closed": False, "reason": "quote_unavailable"}
            current_price = expired_result

        outcome = self._outcome_for_price(record, current_price)
        if outcome is None:
            return {
                "id": record.id,
                "symbol": record.symbol,
                "tradingsymbol": record.tradingsymbol,
                "closed": False,
                "current_price": current_price,
            }

        failure_tags = self._failure_tags(record, current_price, outcome)
        updated = self.repository.update_outcome(
            int(record.id),
            outcome=outcome,
            exit_price=current_price,
            review_notes=self._review_note(record, outcome, failure_tags),
            failure_tags=failure_tags,
        )
        return {
            "id": updated.id,
            "symbol": updated.symbol,
            "tradingsymbol": updated.tradingsymbol,
            "closed": True,
            "outcome": outcome,
            "exit_price": current_price,
            "failure_tags": failure_tags,
        }

    def _current_option_price(self, provider: KiteProvider, record: OpportunityRecord) -> float | None:
        if not record.tradingsymbol:
            return None
        instrument = f"{record.exchange or settings.option_exchange}:{record.tradingsymbol}"
        try:
            quote = provider.quote([instrument])
        except Exception:
            return None
        data = quote.get(instrument) or quote.get(record.tradingsymbol) or {}
        if not isinstance(data, dict):
            return None
        for key in ("last_price", "last_traded_price"):
            value = data.get(key)
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _expired_result(self, record: OpportunityRecord) -> float | None:
        expiry = self._parse_date(record.expiry)
        if expiry is None or expiry >= date.today():
            return None
        return 0.0 if record.side == "BUY" else record.entry_price

    def _outcome_for_price(self, record: OpportunityRecord, price: float) -> str | None:
        expiry = self._parse_date(record.expiry)
        if expiry is not None and expiry < date.today():
            return "expired"

        if record.side == "SELL":
            if record.stop_loss is not None and price >= record.stop_loss:
                return "stop_loss"
            if record.target_3 is not None and price <= record.target_3:
                return "target_3"
            if record.target_2 is not None and price <= record.target_2:
                return "target_2"
            if record.target_1 is not None and price <= record.target_1:
                return "target_1"
            return None

        if record.stop_loss is not None and price <= record.stop_loss:
            return "stop_loss"
        if record.target_3 is not None and price >= record.target_3:
            return "target_3"
        if record.target_2 is not None and price >= record.target_2:
            return "target_2"
        if record.target_1 is not None and price >= record.target_1:
            return "target_1"
        return None

    def _failure_tags(self, record: OpportunityRecord, exit_price: float, outcome: str) -> list[str]:
        if outcome not in {"stop_loss", "false_signal", "loser", "expired"}:
            return []

        tags: list[str] = []
        factors = self._json(record.factor_scores_json)
        contract = factors.get("contract", {}) if isinstance(factors.get("contract"), dict) else {}
        price_action = factors.get("price_action", {}) if isinstance(factors.get("price_action"), dict) else {}
        option_chain = factors.get("option_chain", {}) if isinstance(factors.get("option_chain"), dict) else {}
        prices = factors.get("prices", {}) if isinstance(factors.get("prices"), dict) else {}

        expiry = self._parse_date(record.expiry)
        if expiry is not None and expiry <= date.today():
            tags.append("expiry_day_or_expired_option")
        if record.side == "BUY" and record.entry_price is not None and record.entry_price < settings.min_option_buy_premium:
            tags.append("low_premium_option_noise")

        room_pct = self._nested_float(price_action, "details", "room_to_level_pct")
        if room_pct is not None and room_pct < settings.min_directional_room_pct:
            tags.append("insufficient_room_to_nearest_level")

        bid = self._float(contract.get("bid"))
        ask = self._float(contract.get("ask"))
        last_price = self._float(contract.get("last_price")) or record.entry_price
        if bid and ask and last_price:
            spread_pct = ((ask - bid) / last_price) * 100
            if spread_pct > 2:
                tags.append("spread_slippage_drag")

        chain_reasons = option_chain.get("reasons", [])
        if isinstance(chain_reasons, list):
            tags.extend(f"option_chain:{str(reason)}" for reason in chain_reasons[:3])

        pcr_volume = self._nested_float(option_chain, "details", "pcr_volume")
        if record.action == "BUY_PE" and pcr_volume is not None and pcr_volume < 0.7:
            tags.append("weak_put_volume_confirmation")
        if record.action == "BUY_CE" and pcr_volume is not None and pcr_volume > 1.4:
            tags.append("put_volume_too_dominant_for_call_buy")

        risk_reward = self._float(prices.get("risk_reward")) or record.risk_reward
        if risk_reward and risk_reward < settings.min_risk_reward:
            tags.append("weak_risk_reward")
        if exit_price <= 0:
            tags.append("option_expired_worthless")

        return list(dict.fromkeys(tags))

    def _review_note(self, record: OpportunityRecord, outcome: str, failure_tags: list[str]) -> str:
        if failure_tags:
            return f"Auto-classified {outcome}; tags={', '.join(failure_tags)}"
        return f"Auto-classified {outcome}"

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _nested_float(self, value: dict[str, Any], *keys: str) -> float | None:
        current: Any = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return self._float(current)

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
