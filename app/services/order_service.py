from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Callable, Dict

from app.config import settings
from app.models import Signal
from app.providers.kite_provider import KiteProvider
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.paper_trading_service import PaperTradingService
from app.services.risk_management_service import RiskManagementService
from app.services.time_utils import ist_today
from app.services.trade_repository import TradeRepository
from app.services.trade_setup_service import TradeSetupService
from app.services.market_data_coordinator import MarketDataCoordinator


class OrderService:
    """Route approved signals to paper trading or Kite live orders."""

    def __init__(
        self,
        kite_provider: KiteProvider | None = None,
        paper_trading_service: PaperTradingService | None = None,
        trade_setup_service: TradeSetupService | None = None,
        trade_repository: TradeRepository | None = None,
        risk_management_service: RiskManagementService | None = None,
        active_price_feed: ActiveTradePriceFeed | None = None,
        live_safety_checker: Callable[[], dict[str, Any]] | None = None,
        market_data_coordinator: MarketDataCoordinator | None = None,
        latency_metrics: Any | None = None,
    ) -> None:
        self.kite_provider = kite_provider or KiteProvider()
        self.paper_trading_service = paper_trading_service or PaperTradingService()
        self.trade_setup_service = trade_setup_service or TradeSetupService()
        self.trade_repository = trade_repository or TradeRepository()
        self.risk_management_service = risk_management_service or RiskManagementService(self.trade_repository)
        self.active_price_feed = active_price_feed
        self.live_safety_checker = live_safety_checker
        self.market_data_coordinator = market_data_coordinator
        self.latency_metrics = latency_metrics
        self._banknifty_underlying_token: int | None = None
        self._protective_failure_blocked = False
        self._protective_failure_reason: str | None = None

    def place_signal_order(
        self,
        signal: Signal,
        confirm_live: bool = False,
        opportunity_id: int | None = None,
        order_mode: str | None = None,
        metadata: dict[str, Any] | None = None,
        execution_quality_override: dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        order_started = time.perf_counter()
        self._validate_signal(signal)
        transaction_type = "BUY" if signal.side.upper() == "BUY" else "SELL"
        mode = (order_mode or settings.default_order_mode or "paper").lower()
        live_requested = mode == "live" and confirm_live
        quality = execution_quality_override if execution_quality_override and not live_requested else self._execution_quality(signal)
        if settings.enforce_execution_quality and not quality["passed"]:
            raise ValueError("execution quality blocked order: " + "; ".join(str(reason) for reason in quality["reasons"]))
        if not live_requested:
            trade = self.paper_trading_service.execute_trade(
                symbol=signal.tradingsymbol or signal.symbol,
                entry_price=float(signal.entry_price or 0.0),
                quantity=signal.quantity,
                stop_loss=float(signal.stop_loss or 0.0),
                action=signal.action,
                metadata=metadata,
            )
            record = self.trade_repository.create_trade(
                signal,
                mode="paper",
                status="filled",
                requested_quantity=signal.quantity,
                placed_quantity=signal.quantity,
                order_response=trade,
                opportunity_id=opportunity_id,
                notes=self._metadata_note(metadata),
            )
            self._subscribe_active_trade_tokens(signal)
            self._record_latency("order_service_start_to_ack", order_started, signal=signal, mode="paper")
            return {"status": "paper", "trade": trade, "trade_id": record.id, "execution_quality": quality}

        if not settings.live_trading_mode or settings.paper_trading_mode:
            raise ValueError("live order blocked: set LIVE_TRADING_MODE=true and PAPER_TRADING_MODE=false")
        if settings.require_broker_protective_stop_for_live_entry and not settings.enable_broker_emergency_sl:
            raise ValueError("live order blocked: broker-side protective stop is required but ENABLE_BROKER_EMERGENCY_SL is false")
        if self._protective_failure_blocked:
            raise ValueError(f"live order blocked: previous protective stop failure: {self._protective_failure_reason or 'unknown'}")
        safety = self.live_safety_checker() if self.live_safety_checker is not None else {"blocked": False}
        if safety.get("blocked"):
            raise ValueError(f"live order blocked by safety check: {safety.get('reason') or 'unknown'}")

        risk = self.risk_management_service.evaluate_signal(signal.symbol)
        if not risk["passed"]:
            raise ValueError("risk guard blocked order: " + "; ".join(str(reason) for reason in risk["reasons"]))

        quantity = self._live_affordable_quantity(signal)
        if quantity <= 0:
            raise ValueError("available Zerodha funds are insufficient for one option lot")

        submission_started = time.perf_counter()
        result = self.kite_provider.place_order(
            tradingsymbol=str(signal.tradingsymbol),
            exchange=signal.exchange,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="MARKET",
            product=settings.default_product,
        )
        self._record_latency("live_submission_to_broker_ack", submission_started, signal=signal, mode="live")
        self._record_latency("order_submission_to_broker_acknowledgement", submission_started, signal=signal, mode="live")
        order_id = result.get("order_id") if isinstance(result, dict) else None
        record = self.trade_repository.create_trade(
            signal,
            mode="live",
            status=str(result.get("status", "submitted")) if isinstance(result, dict) else "submitted",
            requested_quantity=signal.quantity,
            placed_quantity=quantity,
            order_response=result,
            broker_order_id=str(order_id) if order_id else None,
            opportunity_id=opportunity_id,
        )
        broker_emergency_sl = self._broker_emergency_protection(signal, record=record, quantity=quantity, entry_order_id=str(order_id) if order_id else None)
        if settings.require_broker_protective_stop_for_live_entry and broker_emergency_sl.get("enabled") and broker_emergency_sl.get("reason") not in {None, "entry order is not confirmed filled yet"} and not broker_emergency_sl.get("submitted"):
            self._protective_failure_blocked = True
            self._protective_failure_reason = str(broker_emergency_sl.get("reason") or "protective stop submission failed")
        self._subscribe_active_trade_tokens(signal)
        self._record_latency("order_service_start_to_ack", order_started, signal=signal, mode="live")
        return {
            "status": "live",
            "order": result,
            "requested_quantity": signal.quantity,
            "placed_quantity": quantity,
            "trade_id": record.id,
            "execution_quality": quality,
            "broker_emergency_sl": broker_emergency_sl,
        }

    def _record_latency(self, name: str, started: float, *, signal: Signal, mode: str) -> None:
        if self.latency_metrics is None:
            return
        self.latency_metrics.record(
            name,
            (time.perf_counter() - started) * 1000.0,
            detail={"symbol": signal.symbol, "tradingsymbol": signal.tradingsymbol, "mode": mode},
        )

    def _validate_signal(self, signal: Signal) -> None:
        if not signal.tradingsymbol:
            raise ValueError("signal does not include an option tradingsymbol")
        symbol = str(signal.symbol or "").upper()
        tradingsymbol = str(signal.tradingsymbol or "").upper()
        if symbol != "BANKNIFTY" or not tradingsymbol.startswith("BANKNIFTY"):
            raise ValueError("only BANKNIFTY option-buying signals can be ordered")
        if str(signal.exchange or "").upper() != settings.option_exchange.upper():
            raise ValueError(f"signal exchange must be {settings.option_exchange}")
        if signal.side.upper() != "BUY":
            raise ValueError("only option buying entries are supported")
        if signal.action.upper() not in {"BUY_CE", "BUY_PE"}:
            raise ValueError("only BUY_CE and BUY_PE actions are supported")
        if signal.quantity <= 0:
            raise ValueError("signal quantity must be positive")
        if not signal.entry_price or signal.entry_price <= 0:
            raise ValueError("signal entry price must be positive")
        if not signal.stop_loss or signal.stop_loss <= 0:
            raise ValueError("signal stop loss must be positive")
        if not signal.target_1 or signal.target_1 <= signal.entry_price:
            raise ValueError("signal target_1 must be above entry price")
        if signal.score < settings.min_signal_score:
            raise ValueError("signal score is below threshold")
        expiry = self._signal_expiry_date(signal)
        if expiry is None:
            raise ValueError("signal expiry is required for order placement")
        today = ist_today()
        if expiry < today:
            raise ValueError("signal option contract is expired")
        if settings.block_expiry_day_option_buying and expiry <= today:
            raise ValueError("expiry-day option buying is blocked")
        factors = signal.factor_scores if isinstance(signal.factor_scores, dict) else {}
        if not factors or "strategy_metadata" not in factors:
            raise ValueError("order signal must come from scanner diagnostics/opportunity with strategy metadata")

    def _signal_expiry_date(self, signal: Signal) -> date | None:
        raw_expiry: Any = signal.expiry
        if raw_expiry is None and isinstance(signal.factor_scores, dict):
            contract = signal.factor_scores.get("contract")
            if isinstance(contract, dict):
                raw_expiry = contract.get("expiry")
        if raw_expiry is None:
            return None
        if isinstance(raw_expiry, date) and not isinstance(raw_expiry, datetime):
            return raw_expiry
        try:
            return datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def _live_affordable_quantity(self, signal: Signal) -> int:
        if signal.side.upper() == "SELL":
            return signal.quantity

        margins = self.kite_provider.margins()
        available_funds = self._available_cash(margins)
        affordable_quantity = self.trade_setup_service.affordable_quantity(
            entry_price=float(signal.entry_price or 0.0),
            lot_size=signal.lot_size or signal.quantity,
            available_funds=available_funds,
            side=signal.side,
        )
        if affordable_quantity <= 0:
            return 0
        return min(signal.quantity, affordable_quantity)

    def _available_cash(self, margins: Dict[str, Any]) -> float:
        candidates = [
            margins.get("available", {}).get("cash") if isinstance(margins.get("available"), dict) else None,
            margins.get("equity", {}).get("available", {}).get("cash") if isinstance(margins.get("equity"), dict) else None,
            margins.get("equity", {}).get("net") if isinstance(margins.get("equity"), dict) else None,
        ]
        for value in candidates:
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _execution_quality(self, signal: Signal) -> dict[str, Any]:
        if not settings.enforce_execution_quality:
            return {"passed": True, "reasons": [], "skipped": "execution quality guard disabled"}
        instrument = f"{signal.exchange}:{signal.tradingsymbol}"
        try:
            if self.market_data_coordinator is not None:
                quote = self.market_data_coordinator.quote([instrument], provider=self.kite_provider)
            else:
                quote = self.kite_provider.quote([instrument])
        except Exception as exc:
            return {"passed": False, "reasons": [f"quote unavailable before execution: {exc}"]}
        payload = quote.get(instrument) or quote.get(str(signal.tradingsymbol)) or {}
        last_price = self._float(payload.get("last_price"))
        depth = payload.get("depth", {}) if isinstance(payload, dict) else {}
        buy_depth = depth.get("buy", []) if isinstance(depth, dict) else []
        sell_depth = depth.get("sell", []) if isinstance(depth, dict) else []
        bid = self._float(buy_depth[0].get("price")) if buy_depth else 0.0
        ask = self._float(sell_depth[0].get("price")) if sell_depth else 0.0
        reference = ask if signal.side.upper() == "BUY" and ask > 0 else last_price
        reasons: list[str] = []
        if last_price < settings.min_execution_quote_price:
            reasons.append("last traded price is too low or unavailable")
        if bid <= 0 or ask <= 0:
            reasons.append("bid/ask depth is unavailable")
        spread_pct = ((ask - bid) / max(last_price, 0.01)) * 100 if bid > 0 and ask > 0 else 100.0
        if spread_pct > settings.max_execution_spread_pct:
            reasons.append("execution spread is wider than allowed")
        entry_price = float(signal.entry_price or 0)
        deviation_pct = abs(reference - entry_price) / max(entry_price, 0.01) * 100
        if deviation_pct > settings.max_entry_price_deviation_pct:
            reasons.append("current quote moved too far from signal entry")
        return {
            "passed": not reasons,
            "reasons": reasons,
            "details": {
                "instrument": instrument,
                "last_price": last_price,
                "bid": bid,
                "ask": ask,
                "spread_pct": round(spread_pct, 2),
                "entry_price": entry_price,
                "deviation_pct": round(deviation_pct, 2),
            },
        }

    def _broker_emergency_protection(self, signal: Signal, *, record: Any, quantity: int, entry_order_id: str | None) -> dict[str, Any]:
        if not settings.enable_broker_emergency_sl:
            return {"enabled": False}
        if signal.side.upper() != "BUY":
            return {"enabled": True, "submitted": False, "reason": "broker emergency SL is only supported for option BUY trades"}
        trigger_price = float(signal.stop_loss or 0.0)
        if trigger_price <= 0:
            self.trade_repository.update_protective_order(
                int(record.id),
                status="failed",
                broker_payload={"reason": "stop loss is missing"},
                error="stop loss is missing",
            )
            return {"enabled": True, "submitted": False, "reason": "stop loss is missing"}
        entry_confirmation = self._confirm_entry_fill(entry_order_id)
        partial_filled_quantity = int(entry_confirmation.get("filled_quantity") or 0)
        if not entry_confirmation["complete"] and partial_filled_quantity <= 0:
            self.trade_repository.update_protective_order(
                int(record.id),
                status="pending_entry_confirmation",
                broker_payload={"entry_confirmation": entry_confirmation},
                trigger_price=trigger_price,
            )
            return {
                "enabled": True,
                "submitted": False,
                "reason": "entry order is not confirmed filled yet",
                "entry_confirmation": entry_confirmation,
                "trigger_price": trigger_price,
            }
        if not entry_confirmation["complete"] and partial_filled_quantity > 0:
            try:
                self.kite_provider.cancel_order(str(entry_order_id), variety="regular")
            except Exception as exc:
                self.trade_repository.update_protective_order(
                    int(record.id),
                    status="failed",
                    broker_payload={"reason": "partial entry cancellation failed", "entry_confirmation": entry_confirmation},
                    trigger_price=trigger_price,
                    error=str(exc),
                )
                return {"enabled": True, "submitted": False, "reason": f"partial entry cancellation failed: {exc}"}
        filled_quantity = partial_filled_quantity or int(quantity)
        average_price = self._float(entry_confirmation.get("average_price"))
        self.trade_repository.update_broker_status(
            int(record.id),
            status="filled",
            broker_payload={"entry_confirmation": entry_confirmation},
            filled_quantity=filled_quantity,
            average_price=average_price if average_price > 0 else None,
        )
        try:
            response = self.kite_provider.place_order(
                tradingsymbol=str(signal.tradingsymbol),
                exchange=signal.exchange,
                transaction_type="SELL",
                quantity=filled_quantity,
                order_type="SL-M",
                product=settings.default_product,
                trigger_price=trigger_price,
            )
        except Exception as exc:
            self.trade_repository.update_protective_order(
                int(record.id),
                status="failed",
                broker_payload={"reason": str(exc), "entry_confirmation": entry_confirmation},
                trigger_price=trigger_price,
                error=str(exc),
            )
            return {"enabled": True, "submitted": False, "reason": str(exc), "trigger_price": trigger_price}
        protective_order_id = str(response.get("order_id")) if isinstance(response, dict) and response.get("order_id") else None
        response_status = str(response.get("status") or "submitted").lower() if isinstance(response, dict) else "submitted"
        if not protective_order_id or response_status in {"rejected", "cancelled", "canceled", "failed"}:
            reason = "protective stop broker acknowledgement is missing an order id" if not protective_order_id else f"protective stop order {response_status}"
            self.trade_repository.update_protective_order(
                int(record.id),
                status="failed",
                protective_order_id=protective_order_id,
                trigger_price=trigger_price,
                broker_payload={"protective_order": response, "entry_confirmation": entry_confirmation},
                error=reason,
            )
            return {"enabled": True, "submitted": False, "reason": reason, "trigger_price": trigger_price}
        self.trade_repository.update_protective_order(
            int(record.id),
            status=str(response.get("status") or "submitted") if isinstance(response, dict) else "submitted",
            protective_order_id=protective_order_id,
            trigger_price=trigger_price,
            broker_payload={"protective_order": response, "entry_confirmation": entry_confirmation},
        )
        return {
            "enabled": True,
            "submitted": True,
            "order_type": "SL-M",
            "transaction_type": "SELL",
            "trigger_price": trigger_price,
            "quantity": filled_quantity,
            "protective_order_id": protective_order_id,
        }

    def _confirm_entry_fill(self, entry_order_id: str | None) -> dict[str, Any]:
        if not entry_order_id:
            return {"complete": False, "reason": "entry_order_id_missing"}
        try:
            history = self.kite_provider.order_history(entry_order_id)
        except Exception as exc:
            return {"complete": False, "reason": "entry_order_history_unavailable", "message": str(exc)}
        latest = history[-1] if history else {}
        status = str(latest.get("status") or "").lower()
        filled_quantity = self._int(latest.get("filled_quantity")) or self._int(latest.get("quantity")) or 0
        average_price = self._float(latest.get("average_price"))
        return {
            "complete": status in {"complete", "filled"} and filled_quantity > 0 and average_price > 0,
            "status": status or "unknown",
            "filled_quantity": filled_quantity,
            "average_price": average_price,
            "latest": latest,
        }

    def _subscribe_active_trade_tokens(self, signal: Signal) -> None:
        if self.active_price_feed is None:
            return
        tokens: set[int] = set()
        if signal.instrument_token:
            tokens.add(int(signal.instrument_token))
            websocket_feed = getattr(self.active_price_feed, "websocket_feed", None)
            register_symbol = getattr(websocket_feed, "register_token_symbol", None)
            if callable(register_symbol) and signal.tradingsymbol:
                register_symbol(int(signal.instrument_token), str(signal.tradingsymbol))
        underlying_token = self._resolve_banknifty_underlying_token()
        if underlying_token:
            tokens.add(underlying_token)
        if tokens:
            self.active_price_feed.subscribe(tokens)

    def _resolve_banknifty_underlying_token(self) -> int | None:
        if self._banknifty_underlying_token:
            return self._banknifty_underlying_token
        try:
            if self.market_data_coordinator is not None:
                instruments = self.market_data_coordinator.instruments("NSE", provider=self.kite_provider)
            else:
                instruments = self.kite_provider.instruments("NSE")
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

    def _metadata_note(self, metadata: dict[str, Any] | None) -> str | None:
        if not metadata:
            return None
        source = metadata.get("entry_source")
        setup_id = metadata.get("armed_setup_id")
        if source or setup_id:
            return f"entry_source={source or 'unknown'} armed_setup_id={setup_id or '-'}"
        return None

    def _int(self, value: Any) -> int | None:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            return None
        return None

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
