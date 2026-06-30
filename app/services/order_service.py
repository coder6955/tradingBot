from __future__ import annotations

from typing import Any, Dict

from app.config import settings
from app.models import Signal
from app.providers.kite_provider import KiteProvider
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_setup_service import TradeSetupService


class OrderService:
    """Route approved signals to paper trading or Kite live orders."""

    def __init__(
        self,
        kite_provider: KiteProvider | None = None,
        paper_trading_service: PaperTradingService | None = None,
        trade_setup_service: TradeSetupService | None = None,
    ) -> None:
        self.kite_provider = kite_provider or KiteProvider()
        self.paper_trading_service = paper_trading_service or PaperTradingService()
        self.trade_setup_service = trade_setup_service or TradeSetupService()

    def place_signal_order(self, signal: Signal, confirm_live: bool = False) -> Dict[str, Any]:
        self._validate_signal(signal)

        transaction_type = "BUY" if signal.side.upper() == "BUY" else "SELL"
        if not settings.live_trading_mode or settings.paper_trading_mode or not confirm_live:
            trade = self.paper_trading_service.execute_trade(
                symbol=signal.tradingsymbol or signal.symbol,
                entry_price=float(signal.entry_price or 0.0),
                quantity=signal.quantity,
                stop_loss=float(signal.stop_loss or 0.0),
                action=signal.action,
            )
            return {"status": "paper", "trade": trade}

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
        return {
            "status": "live",
            "order": result,
            "requested_quantity": signal.quantity,
            "placed_quantity": quantity,
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
        return settings.account_equity
