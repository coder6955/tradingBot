from __future__ import annotations

from typing import Dict, List

from app.config import settings
from app.services.execution_realism_service import ExecutionRealismService
from app.services.realistic_pnl_service import RealisticPnlService


class PaperTradingService:
    """A lightweight paper-trading engine for virtual order execution."""

    def __init__(self) -> None:
        self.positions: List[Dict[str, object]] = []
        self.closed_trades: List[Dict[str, object]] = []
        self.equity = 100000.0
        self.pnl = 0.0
        self.pnl_service = RealisticPnlService()
        self.execution_realism_service = ExecutionRealismService()

    def execute_trade(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        action: str,
        metadata: Dict[str, object] | None = None,
    ) -> Dict[str, object]:
        fill = (
            self.execution_realism_service.entry_fill(
                intended_price=entry_price,
                side="BUY" if str(action).upper().startswith("BUY") else "SELL",
                bid=self._nested_float(metadata, "quote", "bid"),
                ask=self._nested_float(metadata, "quote", "ask"),
            )
            if settings.enable_execution_realism
            else None
        )
        filled_entry = fill.fill_price if fill and fill.filled else float(entry_price)
        trade = {
            "symbol": symbol,
            "entry_price": filled_entry,
            "intended_entry_price": entry_price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "action": action,
        }
        if fill:
            trade["execution_realism"] = {"entry_fill": fill.to_dict()}
        if metadata:
            trade["metadata"] = metadata
        self.positions.append(trade)
        self.equity = float(filled_entry)
        return trade

    def close_trade(
        self,
        symbol: str,
        exit_price: float,
        outcome: str = "exit",
        bid: float | None = None,
        ask: float | None = None,
    ) -> Dict[str, object]:
        position = next(
            (item for item in self.positions if item["symbol"] == symbol), None
        )
        if position is None:
            raise ValueError(f"no open position for {symbol}")
        self.positions.remove(position)
        fill = (
            self.execution_realism_service.exit_fill(
                intended_price=exit_price,
                side="BUY"
                if str(position.get("action", "")).upper().startswith("BUY")
                else "SELL",
                outcome=outcome,
                bid=bid,
                ask=ask,
            )
            if settings.enable_execution_realism
            else None
        )
        filled_exit = fill.fill_price if fill and fill.filled else float(exit_price)
        breakdown = self.pnl_service.calculate(
            entry_price=float(position["entry_price"]),
            exit_price=filled_exit,
            quantity=int(position["quantity"]),
            side="BUY"
            if str(position.get("action", "")).upper().startswith("BUY")
            else "SELL",
            include_slippage=not settings.enable_execution_realism,
            include_spread=not settings.enable_execution_realism,
        )
        pnl = breakdown.net_pnl
        self.pnl += pnl
        realism_payload = dict(position.get("execution_realism") or {})
        if fill:
            realism_payload["exit_fill"] = fill.to_dict()
        self.closed_trades.append(
            {
                "symbol": symbol,
                "entry_price": position["entry_price"],
                "intended_entry_price": position.get(
                    "intended_entry_price", position["entry_price"]
                ),
                "exit_price": filled_exit,
                "intended_exit_price": exit_price,
                "quantity": position["quantity"],
                "gross_pnl": breakdown.gross_pnl,
                "charges": breakdown.charges,
                "slippage_cost": breakdown.slippage_cost,
                "spread_cost": breakdown.spread_cost,
                "net_pnl": pnl,
                "pnl": pnl,
                "execution_realism": realism_payload,
            }
        )
        return self.closed_trades[-1]

    def rollback_unpersisted_trade(self, trade: Dict[str, object]) -> bool:
        """Remove the exact paper position when durable trade creation fails."""
        for index, position in enumerate(self.positions):
            if position is trade:
                self.positions.pop(index)
                return True
        return False

    def get_summary(self) -> Dict[str, object]:
        return {
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
            "equity": self.equity,
            "pnl": self.pnl,
        }

    def _nested_float(
        self, payload: Dict[str, object] | None, *path: str
    ) -> float | None:
        current: object = payload or {}
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        try:
            return float(current) if current is not None else None
        except (TypeError, ValueError):
            return None
