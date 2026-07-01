from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_repository import TradeRepository


KiteProviderFactory = Callable[[], KiteProvider]


class TradeExitService:
    """Square off open paper/live trades when target or stop is hit."""

    def __init__(
        self,
        *,
        trade_repository: TradeRepository,
        kite_provider_factory: KiteProviderFactory,
        paper_trading_service: PaperTradingService,
    ) -> None:
        self.trade_repository = trade_repository
        self.kite_provider_factory = kite_provider_factory
        self.paper_trading_service = paper_trading_service

    def evaluate_once(self, limit: int = 100) -> dict[str, Any]:
        if not settings.enable_auto_squareoff:
            return {"enabled": False, "evaluated": 0, "closed": 0, "results": []}

        provider = self.kite_provider_factory()
        trades = self.trade_repository.open_trades()[:limit]
        results: list[dict[str, Any]] = []
        for trade in trades:
            results.append(self._evaluate_trade(provider, trade))
        return {
            "enabled": True,
            "evaluated": len(results),
            "closed": len([item for item in results if item.get("closed")]),
            "results": results,
        }

    def _evaluate_trade(self, provider: KiteProvider, trade: Any) -> dict[str, Any]:
        current_price = self._current_option_price(provider, trade.exchange, trade.tradingsymbol)
        if current_price is None:
            return {"trade_id": trade.id, "tradingsymbol": trade.tradingsymbol, "closed": False, "reason": "quote_unavailable"}

        outcome = self._outcome_for_price(trade, current_price)
        if outcome is None:
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "current_price": current_price,
            }

        if trade.mode == "live" and not settings.live_auto_squareoff:
            return {
                "trade_id": trade.id,
                "tradingsymbol": trade.tradingsymbol,
                "closed": False,
                "current_price": current_price,
                "outcome_detected": outcome,
                "reason": "live auto square-off is disabled",
            }

        squareoff = self._squareoff(provider, trade, current_price)
        updated = self.trade_repository.close_trade(
            int(trade.id),
            outcome=outcome,
            exit_price=current_price,
            notes=f"Auto square-off: {outcome}; {squareoff}",
        )
        return {
            "trade_id": updated.id,
            "tradingsymbol": updated.tradingsymbol,
            "mode": updated.mode,
            "closed": True,
            "outcome": outcome,
            "exit_price": current_price,
            "pnl": updated.pnl,
            "squareoff": squareoff,
        }

    def _squareoff(self, provider: KiteProvider, trade: Any, exit_price: float) -> dict[str, Any]:
        if trade.mode == "paper":
            try:
                paper = self.paper_trading_service.close_trade(str(trade.tradingsymbol), exit_price=exit_price)
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

    def _outcome_for_price(self, trade: Any, price: float) -> str | None:
        timed_exit = self._time_exit_outcome(trade, price)
        if timed_exit:
            return timed_exit

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

    def _time_exit_outcome(self, trade: Any, price: float) -> str | None:
        if settings.exit_open_trades_before_close_minutes > 0 and self._near_market_close():
            return "time_exit"
        if settings.option_time_stop_minutes <= 0:
            return None
        created_at = trade.created_at
        if created_at is None:
            return None
        created_utc = created_at.replace(tzinfo=ZoneInfo("UTC")) if created_at.tzinfo is None else created_at
        if datetime.now(ZoneInfo("UTC")) < created_utc + timedelta(minutes=settings.option_time_stop_minutes):
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

    def _near_market_close(self) -> bool:
        try:
            hour, minute = (int(part) for part in settings.market_close_time.split(":", 1))
        except ValueError:
            return False
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        close_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return close_at - timedelta(minutes=settings.exit_open_trades_before_close_minutes) <= now <= close_at + timedelta(minutes=5)

    def _current_option_price(self, provider: KiteProvider, exchange: str, tradingsymbol: str) -> float | None:
        instrument = f"{exchange or settings.option_exchange}:{tradingsymbol}"
        try:
            quote = provider.quote([instrument])
        except Exception:
            return None
        data = quote.get(instrument) or quote.get(tradingsymbol) or {}
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
