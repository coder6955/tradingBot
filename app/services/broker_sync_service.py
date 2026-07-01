from __future__ import annotations

from typing import Any, Callable

from app.providers.kite_provider import KiteProvider
from app.services.trade_repository import TradeRepository


KiteProviderFactory = Callable[[], KiteProvider]


class BrokerSyncService:
    """Synchronize stored live trades with broker order status."""

    def __init__(
        self,
        trade_repository: TradeRepository,
        kite_provider_factory: KiteProviderFactory,
    ) -> None:
        self.trade_repository = trade_repository
        self.kite_provider_factory = kite_provider_factory

    def sync_open_trades(self, limit: int = 100) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        trades = [
            trade
            for trade in self.trade_repository.list_trades(limit=limit)
            if trade.mode == "live" and trade.status != "closed" and trade.broker_order_id
        ]
        results: list[dict[str, Any]] = []
        for trade in trades:
            try:
                results.append(self.sync_trade(int(trade.id), provider=provider))
            except Exception as exc:
                results.append({"trade_id": trade.id, "status": "error", "message": str(exc)})
        return {"count": len(results), "results": results}

    def sync_trade(self, trade_id: int, provider: KiteProvider | None = None) -> dict[str, Any]:
        provider = provider or self.kite_provider_factory()
        trade = self.trade_repository.get_trade(trade_id)
        if trade is None:
            raise ValueError(f"trade {trade_id} was not found")
        if not trade.broker_order_id:
            raise ValueError(f"trade {trade_id} does not have a broker order id")

        history = provider.order_history(str(trade.broker_order_id))
        latest = history[-1] if history else {}
        status = str(latest.get("status") or trade.status or "unknown").lower()
        filled_quantity = self._int(latest.get("filled_quantity"))
        average_price = self._float(latest.get("average_price"))
        updated = self.trade_repository.update_broker_status(
            trade_id,
            status=status,
            broker_payload={"history": history, "latest": latest},
            filled_quantity=filled_quantity,
            average_price=average_price,
        )
        return {
            "trade_id": updated.id,
            "broker_order_id": updated.broker_order_id,
            "status": updated.status,
            "filled_quantity": updated.filled_quantity,
            "average_price": updated.average_price,
        }

    def _int(self, value: Any) -> int | None:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            return None
        return None

    def _float(self, value: Any) -> float | None:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            return None
        return None
