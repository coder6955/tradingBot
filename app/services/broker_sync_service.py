from __future__ import annotations

import logging
from typing import Any, Callable

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.notification_service import NotificationService
from app.services.episode_reservation_service import EpisodeReservationService
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
        latency_metrics: Any | None = None,
        episode_reservation_service: EpisodeReservationService | None = None,
    ) -> None:
        self.trade_repository = trade_repository
        self.kite_provider_factory = kite_provider_factory
        self.notification_service = notification_service or NotificationService()
        self.exit_confirmation_callback = exit_confirmation_callback
        self.latency_metrics = latency_metrics
        self.episode_reservation_service = episode_reservation_service or EpisodeReservationService()
        self.live_trading_blocked = False
        self.live_block_reason: str | None = None
        self._persistent_live_block_reasons: set[str] = set()
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
        previous_status = str(trade.status or "").lower()
        updated = self.trade_repository.update_broker_status(
            trade_id,
            status=status,
            broker_payload={"history": history, "latest": latest},
            filled_quantity=filled_quantity,
            average_price=average_price,
        )
        protective = self._maybe_place_pending_protective_sl(updated, provider, status=status, filled_quantity=filled_quantity)
        if self.latency_metrics is not None and status in {"complete", "filled"} and previous_status not in {"complete", "filled"}:
            self.latency_metrics.record_between(
                "broker_acknowledgement_to_fill",
                getattr(trade, "created_at", None),
                detail={"trade_id": trade_id, "broker_order_id": trade.broker_order_id},
            )
        return {
            "trade_id": updated.id,
            "broker_order_id": updated.broker_order_id,
            "status": updated.status,
            "filled_quantity": updated.filled_quantity,
            "average_price": updated.average_price,
            "broker_emergency_sl": protective,
        }

    def reconcile_startup_positions(self) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        local_trades = self.trade_repository.live_open_trades(limit=500)
        broker_positions = self._broker_open_positions(provider)
        local_by_symbol = {str(trade.tradingsymbol).upper(): trade for trade in local_trades}
        broker_by_symbol = {str(row.get("tradingsymbol") or "").upper(): row for row in broker_positions}
        mismatches: list[dict[str, Any]] = []
        pending_order_intents = self.episode_reservation_service.recoverable_order_intents()

        for intent in pending_order_intents:
            if str(intent.get("order_mode") or "").lower() != "live":
                continue
            mismatches.append(
                {
                    "type": (
                        "submitted_live_order_without_local_trade"
                        if intent.get("broker_order_id")
                        else "unresolved_live_order_intent"
                    ),
                    "episode_key": intent.get("episode_key"),
                    "broker_order_id": intent.get("broker_order_id"),
                    "submission_status": intent.get("submission_status"),
                    "order_intent": intent.get("order_intent"),
                }
            )

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

        mismatch_blocked = bool(mismatches) and settings.live_reconciliation_blocks_automation
        reasons = sorted(self._persistent_live_block_reasons)
        if mismatch_blocked:
            reasons.append("broker/local live position mismatch")
        self.live_trading_blocked = bool(reasons)
        self.live_block_reason = "; ".join(reasons) if reasons else None
        self.last_reconciliation = {
            "status": "blocked" if self.live_trading_blocked else "ok",
            "live_trading_blocked": self.live_trading_blocked,
            "reason": self.live_block_reason,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "local_open_live_trades": len(local_trades),
            "broker_open_positions": len(broker_positions),
            "recoverable_order_intents": len(pending_order_intents),
        }
        if self.live_trading_blocked:
            self._alert(f"CRITICAL: live trading blocked by startup reconciliation mismatch count={len(mismatches)}")
        return self.last_reconciliation

    def _maybe_place_pending_protective_sl(self, trade: Any, provider: KiteProvider, *, status: str, filled_quantity: int | None) -> dict[str, Any]:
        if not settings.enable_broker_emergency_sl:
            if settings.require_broker_protective_stop_for_live_entry:
                self._block_live("broker-side protective stop is required but disabled")
            return {
                "enabled": False,
                "reason": "broker-side protective stop is required but disabled"
                if settings.require_broker_protective_stop_for_live_entry
                else "broker-side protective stop is disabled",
            }
        if str(getattr(trade, "mode", "")).lower() != "live" or str(getattr(trade, "side", "")).upper() != "BUY":
            return {"enabled": True, "submitted": False, "reason": "not a live BUY trade"}
        if str(getattr(trade, "protective_order_status", "") or "").lower() not in {"pending_entry_confirmation", "failed", ""}:
            return {"enabled": True, "submitted": False, "reason": "protective order already handled", "status": trade.protective_order_status}
        if status not in {"complete", "filled"} and int(filled_quantity or 0) <= 0:
            return {"enabled": True, "submitted": False, "reason": "entry order is not complete", "entry_status": status}
        quantity = int(filled_quantity or trade.filled_quantity or trade.placed_quantity or trade.requested_quantity or 0)
        trigger_price = float(trade.stop_loss or 0.0)
        if quantity <= 0 or trigger_price <= 0:
            self.trade_repository.update_protective_order(
                int(trade.id),
                status="failed",
                broker_payload={"reason": "quantity or stop loss missing", "quantity": quantity, "trigger_price": trigger_price},
                error="quantity or stop loss missing",
            )
            self._block_live("protective stop quantity or trigger price is invalid")
            return {"enabled": True, "submitted": False, "reason": "quantity or stop loss missing"}
        if status not in {"complete", "filled"} and quantity > 0:
            try:
                provider.cancel_order(str(trade.broker_order_id), variety="regular")
            except Exception as exc:
                self._block_live(f"partial entry cancellation failed before protection: {exc}")
                return {"enabled": True, "submitted": False, "reason": f"partial entry cancellation failed: {exc}"}
        try:
            response = provider.place_order(
                tradingsymbol=str(trade.tradingsymbol),
                exchange=str(trade.exchange or settings.option_exchange),
                transaction_type="SELL",
                quantity=quantity,
                order_type="SL-M",
                product=settings.default_product,
                trigger_price=trigger_price,
            )
        except Exception as exc:
            self.trade_repository.update_protective_order(
                int(trade.id),
                status="failed",
                broker_payload={"reason": str(exc), "quantity": quantity, "trigger_price": trigger_price},
                trigger_price=trigger_price,
                error=str(exc),
            )
            self._alert(f"CRITICAL: broker emergency SL placement failed for {trade.tradingsymbol} trade_id={trade.id}: {exc}")
            self._block_live(f"broker emergency SL placement failed: {exc}")
            return {"enabled": True, "submitted": False, "reason": str(exc)}
        protective_order_id = str(response.get("order_id")) if isinstance(response, dict) and response.get("order_id") else None
        response_status = str(response.get("status") or "submitted").lower() if isinstance(response, dict) else "submitted"
        if not protective_order_id or response_status in {"rejected", "cancelled", "canceled", "failed"}:
            reason = "protective stop broker acknowledgement is missing an order id" if not protective_order_id else f"protective stop order {response_status}"
            self.trade_repository.update_protective_order(
                int(trade.id),
                status="failed",
                protective_order_id=protective_order_id,
                trigger_price=trigger_price,
                broker_payload={"protective_order": response, "source": "broker_sync"},
                error=reason,
            )
            self._block_live(reason)
            return {"enabled": True, "submitted": False, "reason": reason, "protective_order_id": protective_order_id}
        self.trade_repository.update_protective_order(
            int(trade.id),
            status=str(response.get("status") or "submitted") if isinstance(response, dict) else "submitted",
            protective_order_id=protective_order_id,
            trigger_price=trigger_price,
            broker_payload={"protective_order": response, "source": "broker_sync"},
        )
        return {"enabled": True, "submitted": True, "protective_order_id": protective_order_id, "trigger_price": trigger_price}

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
                if str(getattr(trade, "protective_order_id", "") or "") == order_id:
                    results.append(self._handle_protective_order_postback(trade, data, provider))
                else:
                    results.append(self.sync_trade(int(trade.id), provider=provider))
            except Exception as exc:
                logger.exception("Broker postback sync failed for order %s", order_id)
                results.append({"trade_id": trade.id, "status": "error", "message": str(exc)})
        return {"handled": bool(trades), "order_id": order_id, "status": status, "synced": results}

    def _handle_protective_order_postback(self, trade: Any, data: dict[str, Any], provider: KiteProvider) -> dict[str, Any]:
        status = str(data.get("status") or data.get("order_status") or "").lower()
        protective_order_id = str(getattr(trade, "protective_order_id", "") or "")
        self.trade_repository.update_protective_order(
            int(trade.id),
            status=status or "unknown",
            protective_order_id=protective_order_id,
            broker_payload={"postback": data},
        )
        if status not in {"complete", "filled"}:
            if status in {"rejected", "cancelled", "canceled", "failed"}:
                self._block_live(f"protective stop order {status}")
            return {"trade_id": trade.id, "protective_order_id": protective_order_id, "status": status or "unknown"}
        if self.latency_metrics is not None:
            self.latency_metrics.record_between(
                "exit_trigger_to_fill",
                getattr(trade, "exit_requested_at", None) or getattr(trade, "price_timestamp", None),
                detail={"trade_id": trade.id, "protective_order_id": protective_order_id, "source": "protective_stop_postback"},
            )
        exit_price = self._float(data.get("average_price")) or float(trade.stop_loss or 0.0)
        claimed = self.trade_repository.try_mark_closing(
            int(trade.id),
            outcome="stop_loss",
            exit_price=exit_price,
            notes="Broker protective SL postback complete; confirming broker position",
        )
        if claimed is None:
            return {"trade_id": trade.id, "protective_order_id": protective_order_id, "status": status, "reason": "trade_already_closing_or_closed"}
        self.trade_repository.update_exit_order_status(
            int(trade.id),
            status=status,
            exit_order_id=protective_order_id,
            broker_payload={"protective_postback": data},
        )
        if self.exit_confirmation_callback is not None:
            return self.exit_confirmation_callback(int(trade.id))
        history = provider.order_history(protective_order_id)
        return {"trade_id": trade.id, "protective_order_id": protective_order_id, "status": status, "history": history}

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

    def _block_live(self, reason: str) -> None:
        self._persistent_live_block_reasons.add(str(reason))
        self.live_trading_blocked = True
        self.live_block_reason = "; ".join(sorted(self._persistent_live_block_reasons))
        self.last_reconciliation = {
            **dict(self.last_reconciliation or {}),
            "status": "blocked",
            "live_trading_blocked": True,
            "reason": self.live_block_reason,
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
