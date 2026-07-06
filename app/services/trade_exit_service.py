from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.database import Candle, get_session
from app.services.time_utils import ist_now, ist_now_naive
from app.providers.kite_provider import KiteProvider
from app.services.active_price_feed import ActiveTradePriceFeed, KitePollingPriceFeed, PriceTick
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_repository import TradeRepository


KiteProviderFactory = Callable[[], KiteProvider]
logger = logging.getLogger(__name__)


class TradeExitService:
    """Square off open paper/live trades when target or stop is hit."""

    def __init__(
        self,
        *,
        trade_repository: TradeRepository,
        kite_provider_factory: KiteProviderFactory,
        paper_trading_service: PaperTradingService,
        active_price_feed: ActiveTradePriceFeed | None = None,
        notification_service: Any | None = None,
        market_data_coordinator: MarketDataCoordinator | None = None,
    ) -> None:
        self.trade_repository = trade_repository
        self.kite_provider_factory = kite_provider_factory
        self.paper_trading_service = paper_trading_service
        self.active_price_feed = active_price_feed
        self.notification_service = notification_service
        self.market_data_coordinator = market_data_coordinator
        self._banknifty_underlying_token: int | None = None
        self._instrument_validation_cache: dict[int, dict[str, Any]] = {}

    def evaluate_once(self, limit: int = 100) -> dict[str, Any]:
        if not settings.enable_auto_squareoff:
            return {"enabled": False, "evaluated": 0, "closed": 0, "results": []}

        provider = self.kite_provider_factory()
        trades = self.trade_repository.open_trades()[:limit]
        subscription = self._sync_active_trade_subscriptions(provider, trades)
        results: list[dict[str, Any]] = []
        for trade in trades:
            results.append(self._evaluate_trade(provider, trade))
        return {
            "enabled": True,
            "evaluated": len(results),
            "closed": len([item for item in results if item.get("closed")]),
            "price_feed": self.active_price_feed.status() if self.active_price_feed else {"websocket_enabled": False},
            "subscription": subscription,
            "results": results,
        }

    def _evaluate_trade(self, provider: KiteProvider, trade: Any) -> dict[str, Any]:
        if str(trade.status).lower() == "closing":
            return self._evaluate_closing_trade(provider, trade)
        if str(trade.status).lower() == "exit_failed":
            return {"trade_id": trade.id, "tradingsymbol": trade.tradingsymbol, "closed": False, "reason": "previous_exit_failed"}
        if str(trade.status).lower() == "reconciliation_mismatch":
            return {"trade_id": trade.id, "tradingsymbol": trade.tradingsymbol, "closed": False, "reason": "broker_reconciliation_mismatch"}

        current_tick = self._current_option_tick(provider, trade)
        if current_tick is None:
            reason = self.active_price_feed.last_reason if self.active_price_feed and self.active_price_feed.last_reason else "quote_unavailable"
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "reason": reason,
                "price_source": None,
                "fallback_used": False,
                "price_rejection_reason": reason,
            }
        current_price = current_tick.price
        self._record_price_excursion(trade, current_tick)

        outcome = self._outcome_for_price(provider, trade, current_price)
        if outcome is None:
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "current_price": current_price,
                **self._price_metadata(current_tick),
            }

        if trade.mode == "live" and not settings.live_auto_squareoff:
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "current_price": current_price,
                "outcome_detected": outcome,
                **self._price_metadata(current_tick),
                "reason": "live auto square-off is disabled",
            }

        partial = self._maybe_partial_squareoff(provider, trade, current_tick, outcome)
        if partial is not None:
            return partial

        if str(trade.mode).lower() == "live":
            return self._submit_live_exit(provider, trade, current_tick, outcome)

        squareoff = self._squareoff(provider, trade, current_price, outcome=outcome, tick=current_tick)
        close_price = current_price
        if str(trade.mode).lower() == "paper":
            paper_payload = squareoff.get("paper") if isinstance(squareoff, dict) else None
            if isinstance(paper_payload, dict) and paper_payload.get("exit_price") is not None:
                close_price = float(paper_payload.get("exit_price") or current_price)
        updated = self.trade_repository.close_trade(
            int(trade.id),
            outcome=outcome,
            exit_price=close_price,
            notes=f"Auto square-off: {outcome}; {squareoff}",
            **self._price_metadata(current_tick, include_in_db=True),
        )
        self._unsubscribe_closed_option_token(updated)
        return {
            "trade_id": updated.id,
            "tradingsymbol": updated.tradingsymbol,
            "mode": updated.mode,
            "closed": True,
            "outcome": outcome,
            "exit_price": close_price,
            "intended_exit_price": current_price,
            **self._price_metadata(current_tick),
            "gross_pnl": updated.gross_pnl,
            "net_pnl": updated.net_pnl if updated.net_pnl is not None else updated.pnl,
            "charges": updated.charges,
            "pnl": updated.net_pnl if updated.net_pnl is not None else updated.pnl,
            "squareoff": squareoff,
        }

    def _record_price_excursion(self, trade: Any, tick: PriceTick) -> None:
        try:
            self.trade_repository.update_mfe_mae(
                int(trade.id),
                price=float(tick.price),
                price_timestamp=tick.timestamp,
            )
        except Exception:
            logger.exception("Failed to update MFE/MAE for trade_id=%s tradingsymbol=%s", getattr(trade, "id", None), getattr(trade, "tradingsymbol", None))

    def _squareoff(self, provider: KiteProvider, trade: Any, exit_price: float, outcome: str = "exit", tick: PriceTick | None = None) -> dict[str, Any]:
        if trade.mode == "paper":
            try:
                paper = self.paper_trading_service.close_trade(
                    str(trade.tradingsymbol),
                    exit_price=exit_price,
                    outcome=outcome,
                    bid=tick.bid if tick else None,
                    ask=tick.ask if tick else None,
                )
                return {"status": "paper_closed", "paper": paper}
            except Exception as exc:
                return {"status": "paper_close_record_only", "message": str(exc)}

        transaction_type = "SELL" if str(trade.side).upper() == "BUY" else "BUY"
        quantity = int(trade.filled_quantity or trade.placed_quantity or trade.requested_quantity or 0)
        if quantity <= 0:
            return {"status": "live_squareoff_skipped", "message": "filled quantity is zero"}
        response = provider.place_order(
            tradingsymbol=str(trade.tradingsymbol),
            exchange=str(trade.exchange or settings.option_exchange),
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="MARKET",
            product=settings.default_product,
        )
        return {"status": "live_squareoff_submitted", "response": response}

    def _squareoff_quantity(self, provider: KiteProvider, trade: Any, exit_price: float, quantity: int) -> dict[str, Any]:
        if trade.mode == "paper":
            return {"status": "paper_partial_closed", "exit_price": exit_price, "quantity": quantity}
        transaction_type = "SELL" if str(trade.side).upper() == "BUY" else "BUY"
        response = provider.place_order(
            tradingsymbol=str(trade.tradingsymbol),
            exchange=str(trade.exchange or settings.option_exchange),
            transaction_type=transaction_type,
            quantity=int(quantity),
            order_type="MARKET",
            product=settings.default_product,
        )
        return {"status": "live_partial_squareoff_submitted", "response": response}

    def _submit_live_exit(self, provider: KiteProvider, trade: Any, tick: PriceTick, outcome: str) -> dict[str, Any]:
        protective_order_id = str(getattr(trade, "protective_order_id", "") or "")
        if protective_order_id and outcome == "stop_loss":
            protective_result = self._handle_protective_stop_exit(provider, trade, protective_order_id, tick, outcome)
            if protective_result is not None:
                return protective_result
        if protective_order_id:
            cancel_result = self._cancel_protective_order_before_exit(provider, trade, protective_order_id)
            if not cancel_result.get("cancelled"):
                reason = f"protective SL cancel failed before live exit: {cancel_result.get('reason') or cancel_result}"
                failed = self.trade_repository.mark_exit_failed(int(trade.id), reason=reason, broker_payload={"protective_cancel": cancel_result})
                self._alert(f"CRITICAL: {reason} for {trade.tradingsymbol} trade_id={trade.id}")
                return {
                    "trade_id": failed.id,
                    "tradingsymbol": failed.tradingsymbol,
                    "closed": False,
                    "reason": "protective_order_cancel_failed",
                    "protective_cancel": cancel_result,
                    **self._price_metadata(tick),
                }
        closing = self.trade_repository.try_mark_closing(
            int(trade.id),
            outcome=outcome,
            exit_price=tick.price,
            notes=f"Exit detected: {outcome}; awaiting live square-off submission",
            **self._price_metadata(tick, include_in_db=True),
        )
        if closing is None:
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "reason": "exit_already_pending_or_closed",
                **self._price_metadata(tick),
            }
        try:
            squareoff = self._squareoff(provider, trade, tick.price)
        except Exception as exc:
            reason = f"live square-off submission failed: {exc}"
            failed = self.trade_repository.mark_exit_failed(int(trade.id), reason=reason)
            self._alert(f"CRITICAL: {reason} for {trade.tradingsymbol} trade_id={trade.id}")
            return {
                "trade_id": failed.id,
                "tradingsymbol": failed.tradingsymbol,
                "closed": False,
                "reason": "live_squareoff_submission_failed",
                "message": str(exc),
                **self._price_metadata(tick),
            }

        response = squareoff.get("response") if isinstance(squareoff, dict) else {}
        exit_order_id = str(response.get("order_id")) if isinstance(response, dict) and response.get("order_id") else None
        self.trade_repository.update_exit_order_status(
            int(trade.id),
            status=str(response.get("status") or "submitted") if isinstance(response, dict) else "submitted",
            exit_order_id=exit_order_id,
            broker_payload={"squareoff": squareoff},
            notes=f"Live square-off submitted for {outcome}",
        )
        confirmed = self._confirm_live_exit(provider, closing, exit_order_id=exit_order_id, outcome=outcome, exit_price=tick.price, price_tick=tick)
        if confirmed.get("closed"):
            return confirmed
        return {
            "trade_id": trade.id,
            "tradingsymbol": trade.tradingsymbol,
            "mode": trade.mode,
            "closed": False,
            "status": "closing",
            "outcome_detected": outcome,
            "exit_price": tick.price,
            "exit_order_id": exit_order_id,
            "squareoff": squareoff,
            "confirmation": confirmed,
            **self._price_metadata(tick),
        }

    def _handle_protective_stop_exit(self, provider: KiteProvider, trade: Any, protective_order_id: str, tick: PriceTick, outcome: str) -> dict[str, Any] | None:
        try:
            history = provider.order_history(protective_order_id)
        except Exception as exc:
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "reason": "protective_order_history_unavailable",
                "message": str(exc),
                **self._price_metadata(tick),
            }
        latest = history[-1] if history else {}
        order_status = str(latest.get("status") or "").lower()
        self.trade_repository.update_protective_order(
            int(trade.id),
            status=order_status or "unknown",
            protective_order_id=protective_order_id,
            broker_payload={"history": history, "latest": latest},
        )
        if order_status in {"complete", "filled"}:
            return self._confirm_live_exit(provider, trade, exit_order_id=protective_order_id, outcome=outcome, exit_price=tick.price, price_tick=tick)
        if order_status in {"rejected", "cancelled", "canceled", "failed"}:
            reason = f"protective SL order {order_status}"
            failed = self.trade_repository.mark_exit_failed(
                int(trade.id),
                reason=reason,
                broker_payload={"protective_history": history, "latest": latest},
            )
            self._alert(f"CRITICAL: {reason} for {trade.tradingsymbol} trade_id={trade.id} order_id={protective_order_id}")
            return {
                "trade_id": failed.id,
                "tradingsymbol": failed.tradingsymbol,
                "closed": False,
                "reason": "protective_order_failed",
                "protective_order_status": order_status,
                **self._price_metadata(tick),
            }
        return {
            "trade_id": trade.id,
            "tradingsymbol": trade.tradingsymbol,
            "closed": False,
            "reason": "protective_stop_order_pending",
            "protective_order_id": protective_order_id,
            "protective_order_status": order_status or "unknown",
            **self._price_metadata(tick),
        }

    def _cancel_protective_order_before_exit(self, provider: KiteProvider, trade: Any, protective_order_id: str) -> dict[str, Any]:
        try:
            history = provider.order_history(protective_order_id)
        except Exception as exc:
            return {"cancelled": False, "reason": "protective_order_history_unavailable", "message": str(exc)}
        latest = history[-1] if history else {}
        status = str(latest.get("status") or "").lower()
        if status in {"complete", "filled"}:
            return {"cancelled": False, "reason": "protective_order_already_filled", "latest": latest}
        if status in {"cancelled", "canceled"}:
            self.trade_repository.update_protective_order(
                int(trade.id),
                status=status,
                protective_order_id=protective_order_id,
                broker_payload={"history": history, "latest": latest},
                cancelled=True,
            )
            return {"cancelled": True, "already_cancelled": True, "latest": latest}
        try:
            response = provider.cancel_order(protective_order_id)
        except Exception as exc:
            self.trade_repository.update_protective_order(
                int(trade.id),
                status="cancel_failed",
                protective_order_id=protective_order_id,
                broker_payload={"history": history, "latest": latest, "cancel_error": str(exc)},
                error=str(exc),
            )
            return {"cancelled": False, "reason": "cancel_order_failed", "message": str(exc)}
        self.trade_repository.update_protective_order(
            int(trade.id),
            status=str(response.get("status") or "cancelled") if isinstance(response, dict) else "cancelled",
            protective_order_id=protective_order_id,
            broker_payload={"history": history, "latest": latest, "cancel_response": response},
            cancelled=True,
        )
        return {"cancelled": True, "response": response}

    def _evaluate_closing_trade(self, provider: KiteProvider, trade: Any) -> dict[str, Any]:
        if str(trade.mode).lower() != "live":
            return {"trade_id": trade.id, "tradingsymbol": trade.tradingsymbol, "closed": False, "reason": "exit_already_pending"}
        return self._confirm_live_exit(
            provider,
            trade,
            exit_order_id=str(trade.exit_order_id) if getattr(trade, "exit_order_id", None) else None,
            outcome=str(trade.outcome or "exit_confirmed"),
            exit_price=float(trade.exit_price or 0.0),
            price_tick=None,
        )

    def confirm_live_exit_for_trade(self, trade_id: int) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        trade = self.trade_repository.get_trade(trade_id)
        if trade is None:
            raise ValueError(f"trade {trade_id} was not found")
        if str(trade.mode).lower() != "live" or str(trade.status).lower() != "closing":
            return {"trade_id": trade.id, "closed": False, "reason": "trade_not_waiting_for_live_exit_confirmation", "status": trade.status}
        return self._evaluate_closing_trade(provider, trade)

    def _confirm_live_exit(
        self,
        provider: KiteProvider,
        trade: Any,
        *,
        exit_order_id: str | None,
        outcome: str,
        exit_price: float,
        price_tick: PriceTick | None,
    ) -> dict[str, Any]:
        if not exit_order_id:
            return {"trade_id": trade.id, "closed": False, "reason": "exit_order_id_missing"}
        try:
            history = provider.order_history(exit_order_id)
        except Exception as exc:
            return {"trade_id": trade.id, "closed": False, "reason": "exit_order_history_unavailable", "message": str(exc)}
        latest = history[-1] if history else {}
        order_status = str(latest.get("status") or "").lower()
        self.trade_repository.update_exit_order_status(
            int(trade.id),
            status=order_status or "unknown",
            exit_order_id=exit_order_id,
            broker_payload={"history": history, "latest": latest},
        )
        if order_status in {"rejected", "cancelled", "canceled", "failed"}:
            reason = f"live exit order {order_status}"
            failed = self.trade_repository.mark_exit_failed(
                int(trade.id),
                reason=reason,
                broker_payload={"history": history, "latest": latest},
            )
            self._alert(f"CRITICAL: {reason} for {trade.tradingsymbol} trade_id={trade.id} order_id={exit_order_id}")
            return {
                "trade_id": failed.id,
                "tradingsymbol": failed.tradingsymbol,
                "closed": False,
                "status": failed.status,
                "exit_order_id": exit_order_id,
                "exit_order_status": order_status,
                "reason": "exit_order_failed",
            }
        if order_status not in {"complete", "filled"}:
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "status": "closing",
                "exit_order_id": exit_order_id,
                "exit_order_status": order_status or "unknown",
                "reason": "exit_order_not_complete",
            }
        expected_quantity = int(trade.remaining_quantity or trade.filled_quantity or trade.placed_quantity or trade.requested_quantity or 0)
        filled_quantity = self._int(latest.get("filled_quantity")) or self._int(latest.get("quantity"))
        if expected_quantity > 0 and (filled_quantity is None or filled_quantity < expected_quantity):
            self._alert(f"CRITICAL: live exit fill quantity incomplete for {trade.tradingsymbol} trade_id={trade.id}")
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "status": "closing",
                "exit_order_id": exit_order_id,
                "exit_order_status": order_status,
                "reason": "exit_filled_quantity_incomplete",
                "expected_quantity": expected_quantity,
                "filled_quantity": filled_quantity,
            }
        avg_price = self._float(latest.get("average_price"))
        if avg_price is None or avg_price <= 0:
            self._alert(f"CRITICAL: live exit average price unavailable for {trade.tradingsymbol} trade_id={trade.id}")
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "status": "closing",
                "exit_order_id": exit_order_id,
                "exit_order_status": order_status,
                "reason": "average_exit_price_unavailable",
            }
        position_check = self._broker_position_zero(provider, trade)
        if not position_check["position_zero"]:
            self._alert(f"CRITICAL: broker position not flat after exit for {trade.tradingsymbol} trade_id={trade.id}")
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "status": "closing",
                "exit_order_id": exit_order_id,
                "exit_order_status": order_status,
                "reason": "broker_position_not_zero",
                "position_check": position_check,
            }
        updated = self.trade_repository.close_trade(
            int(trade.id),
            outcome=outcome,
            exit_price=avg_price,
            notes=f"Live square-off confirmed by broker order {exit_order_id}; position zero",
            exit_order_id=exit_order_id,
            exit_order_status=order_status,
            exit_order_response={"history": history, "latest": latest, "position_check": position_check},
            **(self._price_metadata(price_tick, include_in_db=True) if price_tick else {}),
        )
        self._unsubscribe_closed_option_token(updated)
        return {
            "trade_id": updated.id,
            "tradingsymbol": updated.tradingsymbol,
            "mode": updated.mode,
            "closed": True,
            "outcome": updated.outcome,
            "exit_price": updated.exit_price,
            "exit_order_id": exit_order_id,
            "exit_order_status": order_status,
            "position_check": position_check,
            "gross_pnl": updated.gross_pnl,
            "net_pnl": updated.net_pnl if updated.net_pnl is not None else updated.pnl,
            "charges": updated.charges,
            "pnl": updated.net_pnl if updated.net_pnl is not None else updated.pnl,
        }

    def _alert(self, message: str) -> None:
        logger.critical(message)
        if self.notification_service is None:
            return
        try:
            self.notification_service.send(message)
        except Exception:
            logger.exception("Failed to send trade exit alert")

    def _broker_position_zero(self, provider: KiteProvider, trade: Any) -> dict[str, Any]:
        try:
            positions = provider.positions()
        except Exception as exc:
            return {"position_zero": False, "reason": "positions_unavailable", "message": str(exc)}
        rows: list[dict[str, Any]] = []
        if isinstance(positions, dict):
            for key in ("net", "day"):
                payload = positions.get(key)
                if isinstance(payload, list):
                    rows.extend(item for item in payload if isinstance(item, dict))
        target = str(trade.tradingsymbol).upper()
        exchange = str(trade.exchange or settings.option_exchange).upper()
        matches = [
            row for row in rows
            if str(row.get("tradingsymbol") or "").upper() == target and str(row.get("exchange") or exchange).upper() == exchange
        ]
        if not matches:
            return {"position_zero": True, "reason": "no_matching_position"}
        quantity = sum(int(self._float(row.get("quantity")) or 0) for row in matches)
        return {"position_zero": quantity == 0, "quantity": quantity, "matches": matches}

    def _outcome_for_price(self, provider: KiteProvider, trade: Any, price: float) -> str | None:
        timed_exit = self._time_exit_outcome(trade, price)
        if timed_exit:
            return timed_exit

        invalidation = self._invalidation_exit_outcome(provider, trade, price)
        if invalidation:
            return invalidation

        trailing_exit = self._trailing_exit_outcome(trade, price)
        if trailing_exit:
            return trailing_exit

        if str(trade.side).upper() == "SELL":
            if trade.stop_loss is not None and price >= float(trade.stop_loss):
                return "stop_loss"
            if trade.target_3 is not None and price <= float(trade.target_3):
                return "target_3"
            if trade.target_2 is not None and price <= float(trade.target_2):
                return "target_2"
            if trade.target_1 is not None and price <= float(trade.target_1):
                return "target_1"
            return None

        if trade.stop_loss is not None and price <= float(trade.stop_loss):
            return "stop_loss"
        if trade.target_3 is not None and price >= float(trade.target_3):
            return "target_3"
        if trade.target_2 is not None and price >= float(trade.target_2):
            return "target_2"
        if trade.target_1 is not None and price >= float(trade.target_1):
            return "target_1"
        return None

    def _maybe_partial_squareoff(self, provider: KiteProvider, trade: Any, tick: PriceTick, outcome: str) -> dict[str, Any] | None:
        price = tick.price
        if outcome != "target_1" or not settings.enable_partial_booking:
            return None
        quantity = int(trade.remaining_quantity or trade.filled_quantity or trade.placed_quantity or trade.requested_quantity or 0)
        if quantity <= 1:
            return None
        partial_qty = int(quantity * (settings.partial_target1_pct / 100))
        if partial_qty <= 0 or partial_qty >= quantity:
            return None
        if trade.mode == "live" and not settings.live_auto_squareoff:
            return None
        squareoff = self._squareoff_quantity(provider, trade, price, partial_qty)
        updated = self.trade_repository.record_partial_exit(int(trade.id), quantity=partial_qty, exit_price=price)
        return {
            "trade_id": updated.id,
            "tradingsymbol": updated.tradingsymbol,
            "mode": updated.mode,
            "closed": updated.status == "closed",
            "outcome": "partial_target_1",
            "exit_price": price,
            **self._price_metadata(tick),
            "partial_quantity": partial_qty,
            "remaining_quantity": updated.remaining_quantity,
            "gross_pnl": updated.gross_pnl,
            "net_pnl": updated.net_pnl,
            "pnl": updated.net_pnl if updated.net_pnl is not None else updated.pnl,
            "squareoff": squareoff,
        }

    def _time_exit_outcome(self, trade: Any, price: float) -> str | None:
        if settings.exit_open_trades_before_close_minutes > 0 and self._near_market_close():
            return "time_exit"
        if settings.option_time_stop_minutes <= 0:
            return None
        created_at = trade.created_at
        if created_at is None:
            return None
        created_ist = created_at.replace(tzinfo=ZoneInfo("Asia/Kolkata")) if created_at.tzinfo is None else created_at.astimezone(ZoneInfo("Asia/Kolkata"))
        if ist_now() < created_ist + timedelta(minutes=settings.option_time_stop_minutes):
            return None
        entry = float(trade.average_price or trade.entry_price or 0.0)
        if entry <= 0:
            return None
        move_pct = ((price - entry) / entry) * 100
        if str(trade.side).upper() == "SELL":
            move_pct *= -1
        if move_pct < settings.option_time_stop_min_move_pct:
            return "time_exit"
        return None

    def _trailing_exit_outcome(self, trade: Any, price: float) -> str | None:
        if str(trade.side).upper() != "BUY":
            return None
        entry = float(trade.average_price or trade.entry_price or 0.0)
        target_1 = float(trade.target_1 or 0.0)
        if entry <= 0 or target_1 <= 0 or price <= 0:
            return None
        high_since_entry = self._high_since_entry(str(trade.tradingsymbol), trade.created_at)
        if high_since_entry < target_1:
            return None
        locked_stop = entry * (1 + settings.option_trailing_stop_lock_pct / 100)
        if price <= locked_stop:
            return "trailing_stop"
        return None

    def _invalidation_exit_outcome(self, provider: KiteProvider, trade: Any, price: float) -> str | None:
        if str(trade.side).upper() != "BUY":
            return None
        if settings.enable_underlying_invalidation_exit and self._underlying_failed(provider, trade):
            return "underlying_invalidation"
        if settings.enable_premium_invalidation_exit and self._premium_failed(trade, price):
            return "premium_invalidation"
        return None

    def _underlying_failed(self, provider: KiteProvider, trade: Any) -> bool:
        price = self._current_underlying_price(provider, trade)
        if price <= 0:
            return False
        candles = self._recent_underlying_candles("BANKNIFTY", 24)
        if len(candles) < 6:
            return False
        return self._underlying_structure_failed(trade, price, candles)

    def _current_underlying_price(self, provider: KiteProvider, trade: Any) -> float:
        token = self._resolve_banknifty_underlying_token(provider)
        if self.active_price_feed and token:
            tick = self.active_price_feed.latest_price(
                provider=provider,
                exchange="NSE",
                tradingsymbol="NIFTY BANK",
                instrument_token=token,
                mode=str(trade.mode or "paper"),
            )
            if tick:
                return tick.price
        try:
            if self.market_data_coordinator is not None:
                quote = self.market_data_coordinator.quote(["NSE:NIFTY BANK"], provider=provider)
            else:
                quote = provider.quote(["NSE:NIFTY BANK"])
        except Exception:
            return 0.0
        payload = quote.get("NSE:NIFTY BANK") or {}
        if not isinstance(payload, dict):
            return 0.0
        try:
            return float(payload.get("last_price") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _underlying_structure_failed(self, trade: Any, price: float, candles: list[Candle]) -> bool:
        closes = [float(candle.close_price) for candle in candles]
        highs = [float(candle.high_price) for candle in candles]
        lows = [float(candle.low_price) for candle in candles]
        volumes = [float(candle.volume or 0.0) for candle in candles]
        recent_count = min(12, len(candles))
        volume_sum = sum(volumes[-recent_count:])
        if volume_sum <= 0:
            return False
        start = len(candles) - recent_count
        vwap = sum(((highs[idx] + lows[idx] + closes[idx]) / 3) * volumes[idx] for idx in range(start, len(candles))) / volume_sum
        action = str(trade.action or "").upper()
        symbol = str(trade.tradingsymbol or "").upper()
        if action.endswith("CE") or symbol.endswith("CE"):
            return price < vwap and closes[-1] < closes[-3]
        if action.endswith("PE") or symbol.endswith("PE"):
            return price > vwap and closes[-1] > closes[-3]
        return False

    def _premium_failed(self, trade: Any, price: float) -> bool:
        candles = self._recent_option_candles(str(trade.tradingsymbol), trade.created_at, 12)
        if len(candles) < 4:
            return False
        closes = [float(candle.close_price) for candle in candles]
        highs = [float(candle.high_price) for candle in candles]
        lows = [float(candle.low_price) for candle in candles]
        volumes = [float(candle.volume or 0.0) for candle in candles]
        volume_sum = sum(volumes)
        option_vwap = (
            sum(((highs[idx] + lows[idx] + closes[idx]) / 3) * volumes[idx] for idx in range(len(closes))) / volume_sum
            if volume_sum > 0
            else sum((highs[idx] + lows[idx] + closes[idx]) / 3 for idx in range(len(closes))) / len(closes)
        )
        recent_low = min(lows[-4:])
        entry = float(trade.average_price or trade.entry_price or 0.0)
        return price < option_vwap and price <= recent_low and price < entry

    def _high_since_entry(self, tradingsymbol: str, created_at: datetime | None) -> float:
        if created_at is None:
            return 0.0
        start = created_at.replace(tzinfo=None)
        session = get_session()
        try:
            row = (
                session.query(Candle)
                .filter(Candle.symbol == tradingsymbol, Candle.timestamp >= start)
                .order_by(Candle.high_price.desc())
                .first()
            )
            return float(row.high_price) if row else 0.0
        finally:
            session.close()

    def _recent_underlying_candles(self, symbol: str, limit: int) -> list[Candle]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.timeframe == "5minute")
                .order_by(Candle.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()

    def _recent_option_candles(self, tradingsymbol: str, created_at: datetime | None, limit: int) -> list[Candle]:
        session = get_session()
        try:
            query = session.query(Candle).filter(Candle.symbol == tradingsymbol, Candle.timeframe == "5minute")
            if created_at is not None:
                query = query.filter(Candle.timestamp >= created_at.replace(tzinfo=None))
            rows = query.order_by(Candle.timestamp.desc()).limit(limit).all()
            return list(reversed(rows))
        finally:
            session.close()

    def _near_market_close(self) -> bool:
        try:
            hour, minute = (int(part) for part in settings.market_close_time.split(":", 1))
        except ValueError:
            return False
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        close_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return close_at - timedelta(minutes=settings.exit_open_trades_before_close_minutes) <= now <= close_at + timedelta(minutes=5)

    def _current_option_tick(self, provider: KiteProvider, trade: Any) -> PriceTick | None:
        exchange = str(trade.exchange or settings.option_exchange)
        tradingsymbol = str(trade.tradingsymbol)
        token = self._int(getattr(trade, "instrument_token", None))
        if self.active_price_feed:
            tick = self.active_price_feed.latest_price(
                provider=provider,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                instrument_token=token,
                mode=str(trade.mode or "paper"),
            )
            return tick
        return KitePollingPriceFeed(provider, self.market_data_coordinator).latest_price(exchange=exchange, tradingsymbol=tradingsymbol, instrument_token=token, mode=str(trade.mode or "paper"))

    def _current_option_price(self, provider: KiteProvider, trade: Any) -> float | None:
        tick = self._current_option_tick(provider, trade)
        return tick.price if tick else None

    def _sync_active_trade_subscriptions(self, provider: KiteProvider, trades: list[Any]) -> dict[str, Any]:
        if not self.active_price_feed:
            return {"enabled": False}
        tokens: set[int] = set()
        missing: list[str] = []
        invalid: list[dict[str, Any]] = []
        underlying_token = self._resolve_banknifty_underlying_token(provider)
        if underlying_token:
            tokens.add(underlying_token)
        elif trades:
            missing.append("banknifty_underlying_token")
        for trade in trades:
            token = self._int(getattr(trade, "instrument_token", None))
            if token:
                validation = self._validate_trade_instrument_token(provider, trade, token)
                if validation["valid"]:
                    tokens.add(token)
                else:
                    invalid.append(validation)
            else:
                missing.append(str(getattr(trade, "tradingsymbol", "unknown")))
        subscribed = self.active_price_feed.subscribe(tokens) if tokens else {"subscribed": [], "reason": "token_missing"}
        return {"requested_tokens": sorted(tokens), "missing_tokens": missing, "invalid_tokens": invalid, "result": subscribed}

    def _unsubscribe_closed_option_token(self, trade: Any) -> None:
        if not self.active_price_feed:
            return
        token = self._int(getattr(trade, "instrument_token", None))
        if not token:
            return
        still_open = any(self._int(getattr(item, "instrument_token", None)) == token for item in self.trade_repository.open_trades())
        if not still_open:
            self.active_price_feed.unsubscribe({token})

    def _resolve_banknifty_underlying_token(self, provider: KiteProvider) -> int | None:
        if self._banknifty_underlying_token:
            return self._banknifty_underlying_token
        try:
            if self.market_data_coordinator is not None:
                instruments = self.market_data_coordinator.instruments("NSE", provider=provider)
            else:
                instruments = provider.instruments("NSE")
        except Exception:
            return None
        for item in instruments or []:
            name = str(item.get("name") or item.get("tradingsymbol") or "").upper()
            tradingsymbol = str(item.get("tradingsymbol") or "").upper()
            if name == "NIFTY BANK" or tradingsymbol == "NIFTY BANK":
                token = self._int(item.get("instrument_token"))
                if token:
                    self._banknifty_underlying_token = token
                    return token
        return None

    def _validate_trade_instrument_token(self, provider: KiteProvider, trade: Any, token: int) -> dict[str, Any]:
        cached = self._instrument_validation_cache.get(token)
        if cached is None:
            try:
                if self.market_data_coordinator is not None:
                    instruments = self.market_data_coordinator.instruments(str(trade.exchange or settings.option_exchange), provider=provider)
                else:
                    instruments = provider.instruments(str(trade.exchange or settings.option_exchange))
            except Exception as exc:
                return {"valid": False, "token": token, "tradingsymbol": str(trade.tradingsymbol), "reason": "instrument_validation_unavailable", "message": str(exc)}
            for item in instruments or []:
                if self._int(item.get("instrument_token")) == token:
                    cached = dict(item)
                    self._instrument_validation_cache[token] = cached
                    break
        if cached is None:
            return {"valid": False, "token": token, "tradingsymbol": str(trade.tradingsymbol), "reason": "token_not_found"}
        expected_symbol = str(trade.tradingsymbol or "").upper()
        actual_symbol = str(cached.get("tradingsymbol") or "").upper()
        expected_exchange = str(trade.exchange or settings.option_exchange).upper()
        actual_exchange = str(cached.get("exchange") or expected_exchange).upper()
        valid = expected_symbol == actual_symbol and expected_exchange == actual_exchange
        return {
            "valid": valid,
            "token": token,
            "tradingsymbol": str(trade.tradingsymbol),
            "actual_tradingsymbol": cached.get("tradingsymbol"),
            "exchange": expected_exchange,
            "actual_exchange": actual_exchange,
            "expiry": str(cached.get("expiry")) if cached.get("expiry") is not None else None,
            "reason": None if valid else "token_symbol_exchange_mismatch",
        }

    def _price_metadata(self, tick: PriceTick | None, *, include_in_db: bool = False) -> dict[str, Any]:
        if tick is None:
            return {}
        age = tick.age_seconds
        if age is None:
            age = max(0.0, (ist_now_naive() - tick.timestamp.replace(tzinfo=None)).total_seconds())
        payload = {
            "price_source": tick.source,
            "price_timestamp": tick.timestamp,
            "price_age_seconds": round(float(age), 3),
        }
        if include_in_db:
            return payload
        return {
            **payload,
            "instrument_token": tick.instrument_token,
            "bid": tick.bid,
            "ask": tick.ask,
            "fallback_used": tick.source == "kite_polling",
            "price_rejection_reason": None,
            "price_timestamp_source": tick.timestamp_source,
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
