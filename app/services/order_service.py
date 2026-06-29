from __future__ import annotations

from typing import Any, Dict

from app.config import settings
from app.models import Signal
from app.providers.kite_provider import KiteProvider
from app.services.paper_trading_service import PaperTradingService


class OrderService:
    """Route approved signals to paper trading or Kite live orders."""

    def __init__(
        self,
        kite_provider: KiteProvider | None = None,
        paper_trading_service: PaperTradingService | None = None,
    ) -> None:
        self.kite_provider = kite_provider or KiteProvider()
        self.paper_trading_service = paper_trading_service or PaperTradingService()

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

        result = self.kite_provider.place_order(
            tradingsymbol=str(signal.tradingsymbol),
            exchange=signal.exchange,
            transaction_type=transaction_type,
            quantity=signal.quantity,
            order_type="MARKET",
            product=settings.default_product,
        )
        return {"status": "live", "order": result}

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
