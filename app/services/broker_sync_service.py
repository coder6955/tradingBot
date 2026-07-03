from __future__ import annotations

import logging
from typing import Any, Callable

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.notification_service import NotificationService
from app.services.trade_repository import TradeRepository


KiteProviderFactory = Callable[[], KiteProvider]
ExitConfirmationCallback = Callable[[int], dict[str, Any]]
logger = logging.getLogger(__name__)


class BrokerSyncService:
    """Synchronize stored live trades with broker order status."""

    def __init__(
        self,
        trade_repository: TradeRepository,
        kite_provider_factory: KiteProviderFactory,
        notification_service: NotificationService | None = None,
        exit_confirmation_callback: ExitConfirmationCallback | None = None,
    ) -> None:
        self.trade_repository = trade_repository
        self.kite_provider_factory = kite_provider_factory
        self.notification_service = notification_service or NotificationService()
        self.exit_confirmation_callback = exit_confirmation_callback
        self.live_trading_blocked = False
        self.live_block_reason: str | None = None
        self.last_reconciliation: dict[str, Any] = {}
        self._seen_postbacks: set[str] = set()

    def set_exit_confirmation_callback(self, callback: ExitConfirmationCallback) -> None:
        self.exit_confirmation_callback = callback

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
        if str(trade.status).lower() == "closing" and getattr(trade, "exit_order_id", None):
            if self.exit_confirmation_callback is not None:
                return self.exit_confirmation_callback(trade_id)
            history = provider.order_history(str(trade.exit_order_id))
            latest = history[-1] if history else {}
            status = str(latest.get("status") or trade.exit_order_status or "unknown").lower()
            updated = self.trade_repository.update_exit_order_status(
                trade_id,
                status=status,
                exit_order_id=str(trade.exit_order_id),
                broker_payload={"history": history, "latest": latest},
            )
            return {
                "trade_id": updated.id,
                "broker_order_id": updated.broker_order_id,
                "exit_order_id": updated.exit_order_id,
                "status": updated.status,
                "exit_order_status": updated.exit_order_status,
            }
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

    def reconcile_startup_positions(self) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        local_trades = self.trade_repository.live_open_trades(limit=500)
        broker_positions = self._broker_open_positions(provider)
        local_by_symbol = {str(trade.tradingsymbol).upper(): trade for trade in local_trades}
        broker_by_symbol = {str(row.get("tradingsymbol") or "").upper(): row for row in broker_positions}
        mismatches: list[dict[str, Any]] = []

        for symbol, row in broker_by_symbol.items():
            if symbol and symbol not in local_by_symbol:
                mismatches.append({"type": "broker_position_without_local_trade", "tradingsymbol": symbol, "broker_position": row})

        for trade in local_trades:
            symbol = str(trade.tradingsymbol).upper()
            row = broker_by_symbol.get(symbol)
            if row is None:
                updated = self.trade_repository.mark_reconciliation_mismatch(
                    int(trade.id),
                    reason="local live trade open but broker position is flat/missing",
                    broker_payload={"broker_positions": broker_positions},
                )
                mismatches.append({"type": "local_trade_without_broker_position", "trade_id": updated.id, "tradingsymbol": symbol})
                continue
            quantity = int(self._float(row.get("quantity")) or 0)
            expected = int(trade.remaining_quantity or trade.filled_quantity or trade.placed_quantity or trade.requested_quantity or 0)
            if expected > 0 and abs(quantity) != expected:
                mismatches.append(
                    {
                        "type": "quantity_mismatch",
                        "trade_id": trade.id,
                        "tradingsymbol": symbol,
                        "local_quantity": expected,
                        "broker_quantity": quantity,
                    }
                )

        self.live_trading_blocked = bool(mismatches) and settings.live_reconciliation_blocks_automation
        self.live_block_reason = "broker/local live position mismatch" if self.live_trading_blocked else None
        self.last_reconciliation = {
            "status": "blocked" if self.live_trading_blocked else "ok",
            "live_trading_blocked": self.live_trading_blocked,
            "reason": self.live_block_reason,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "local_open_live_trades": len(local_trades),
            "broker_open_positions": len(broker_positions),
        }
        if self.live_trading_blocked:
            self._alert(f"CRITICAL: live trading blocked by startup reconciliation mismatch count={len(mismatches)}")
        return self.last_reconciliation

    def live_block_status(self) -> dict[str, Any]:
        return {
            "blocked": self.live_trading_blocked,
            "reason": self.live_block_reason,
            "last_reconciliation": self.last_reconciliation,
        }

    def handle_order_postback(self, data: dict[str, Any]) -> dict[str, Any]:
        order_id = str(data.get("order_id") or data.get("order_id_value") or "").strip()
        if not order_id:
            return {"handled": False, "reason": "order_id_missing"}
        status = str(data.get("status") or data.get("order_status") or "").lower()
        fingerprint = "|".join(
            [
                order_id,
                status,
                str(data.get("filled_quantity") or ""),
                str(data.get("average_price") or ""),
            ]
        )
        if fingerprint in self._seen_postbacks:
            return {"handled": True, "duplicate": True, "order_id": order_id}
        self._seen_postbacks.add(fingerprint)

        trades = self.trade_repository.find_live_by_order_id(order_id)
        results: list[dict[str, Any]] = []
        provider = self.kite_provider_factory()
        for trade in trades:
            try:
                results.append(self.sync_trade(int(trade.id), provider=provider))
            except Exception as exc:
                logger.exception("Broker postback sync failed for order %s", order_id)
                results.append({"trade_id": trade.id, "status": "error", "message": str(exc)})
        return {"handled": bool(trades), "order_id": order_id, "status": status, "synced": results}

    def _broker_open_positions(self, provider: KiteProvider) -> list[dict[str, Any]]:
        positions = provider.positions()
        rows: list[dict[str, Any]] = []
        if isinstance(positions, dict):
            for key in ("net", "day"):
                payload = positions.get(key)
                if isinstance(payload, list):
                    rows.extend(item for item in payload if isinstance(item, dict))
        open_rows = []
        for row in rows:
            if str(row.get("exchange") or "").upper() != settings.option_exchange.upper():
                continue
            quantity = int(self._float(row.get("quantity")) or 0)
            if quantity != 0:
                open_rows.append(row)
        return open_rows

    def _alert(self, message: str) -> None:
        logger.critical(message)
        try:
            self.notification_service.send(message)
        except Exception:
            logger.exception("Failed to send broker sync alert")

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
