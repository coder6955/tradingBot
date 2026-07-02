from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.database import Candle, get_session
from app.services.time_utils import ist_now
from app.providers.kite_provider import KiteProvider
from app.services.active_price_feed import KitePollingPriceFeed
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

        outcome = self._outcome_for_price(provider, trade, current_price)
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

        partial = self._maybe_partial_squareoff(provider, trade, current_price, outcome)
        if partial is not None:
            return partial

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
            "gross_pnl": updated.gross_pnl,
            "net_pnl": updated.net_pnl if updated.net_pnl is not None else updated.pnl,
            "charges": updated.charges,
            "pnl": updated.net_pnl if updated.net_pnl is not None else updated.pnl,
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

    def _maybe_partial_squareoff(self, provider: KiteProvider, trade: Any, price: float, outcome: str) -> dict[str, Any] | None:
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
        try:
            quote = provider.quote(["NSE:NIFTY BANK"])
        except Exception:
            return False
        payload = quote.get("NSE:NIFTY BANK") or {}
        if not isinstance(payload, dict):
            return False
        price = float(payload.get("last_price") or 0.0)
        candles = self._recent_underlying_candles("BANKNIFTY", 24)
        if price <= 0 or len(candles) < 6:
            return False
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

    def _current_option_price(self, provider: KiteProvider, exchange: str, tradingsymbol: str) -> float | None:
        tick = KitePollingPriceFeed(provider).latest_price(exchange=exchange, tradingsymbol=tradingsymbol)
        return tick.price if tick else None
