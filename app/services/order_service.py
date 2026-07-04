from __future__ import annotations

from typing import Any, Callable, Dict

from app.config import settings
from app.models import Signal
from app.providers.kite_provider import KiteProvider
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.paper_trading_service import PaperTradingService
from app.services.risk_management_service import RiskManagementService
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
    ) -> None:
        self.kite_provider = kite_provider or KiteProvider()
        self.paper_trading_service = paper_trading_service or PaperTradingService()
        self.trade_setup_service = trade_setup_service or TradeSetupService()
        self.trade_repository = trade_repository or TradeRepository()
        self.risk_management_service = risk_management_service or RiskManagementService(self.trade_repository)
        self.active_price_feed = active_price_feed
        self.live_safety_checker = live_safety_checker
        self.market_data_coordinator = market_data_coordinator
        self._banknifty_underlying_token: int | None = None

    def place_signal_order(
        self,
        signal: Signal,
        confirm_live: bool = False,
        opportunity_id: int | None = None,
        order_mode: str | None = None,
    ) -> Dict[str, Any]:
        self._validate_signal(signal)
        quality = self._execution_quality(signal)
        if settings.enforce_execution_quality and not quality["passed"]:
            raise ValueError("execution quality blocked order: " + "; ".join(str(reason) for reason in quality["reasons"]))

        transaction_type = "BUY" if signal.side.upper() == "BUY" else "SELL"
        mode = (order_mode or settings.default_order_mode or "paper").lower()
        live_requested = mode == "live" and confirm_live
        if not live_requested:
            trade = self.paper_trading_service.execute_trade(
                symbol=signal.tradingsymbol or signal.symbol,
                entry_price=float(signal.entry_price or 0.0),
                quantity=signal.quantity,
                stop_loss=float(signal.stop_loss or 0.0),
                action=signal.action,
            )
            record = self.trade_repository.create_trade(
                signal,
                mode="paper",
                status="filled",
                requested_quantity=signal.quantity,
                placed_quantity=signal.quantity,
                order_response=trade,
                opportunity_id=opportunity_id,
            )
            self._subscribe_active_trade_tokens(signal)
            return {"status": "paper", "trade": trade, "trade_id": record.id, "execution_quality": quality}

        if not settings.live_trading_mode or settings.paper_trading_mode:
            raise ValueError("live order blocked: set LIVE_TRADING_MODE=true and PAPER_TRADING_MODE=false")
        safety = self.live_safety_checker() if self.live_safety_checker is not None else {"blocked": False}
        if safety.get("blocked"):
            raise ValueError(f"live order blocked by broker reconciliation: {safety.get('reason') or 'unknown'}")

        risk = self.risk_management_service.evaluate_signal(signal.symbol)
        if not risk["passed"]:
            raise ValueError("risk guard blocked order: " + "; ".join(str(reason) for reason in risk["reasons"]))

        quantity = self._live_affordable_quantity(signal)
        if quantity <= 0:
            raise ValueError("available Zerodha funds are insufficient for one option lot")

        result = self.kite_provider.place_order(
            tradingsymbol=str(signal.tradingsymbol),
            exchange=signal.exchange,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="MARKET",
            product=settings.default_product,
        )
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
        self._subscribe_active_trade_tokens(signal)
        return {
            "status": "live",
            "order": result,
            "requested_quantity": signal.quantity,
            "placed_quantity": quantity,
            "trade_id": record.id,
            "execution_quality": quality,
            "broker_emergency_sl": self._broker_emergency_protection(signal),
        }

    def _validate_signal(self, signal: Signal) -> None:
        if not signal.tradingsymbol:
            raise ValueError("signal does not include an option tradingsymbol")
        if signal.quantity <= 0:
            raise ValueError("signal quantity must be positive")
        if not signal.entry_price or signal.entry_price <= 0:
            raise ValueError("signal entry price must be positive")
        if not signal.stop_loss or signal.stop_loss <= 0:
            raise ValueError("signal stop loss must be positive")
        if signal.score < settings.min_signal_score:
            raise ValueError("signal score is below threshold")

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

    def _broker_emergency_protection(self, signal: Signal) -> dict[str, Any]:
        if not settings.enable_broker_emergency_sl:
            return {"enabled": False}
        return {
            "enabled": True,
            "submitted": False,
            "reason": "current KiteProvider wrapper does not support trigger_price/GTT protective orders",
            "software_stop_loss": signal.stop_loss,
            "fallback": "TradeExitService software square-off and startup broker reconciliation",
        }

    def _subscribe_active_trade_tokens(self, signal: Signal) -> None:
        if self.active_price_feed is None:
            return
        tokens: set[int] = set()
        if signal.instrument_token:
            tokens.add(int(signal.instrument_token))
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
