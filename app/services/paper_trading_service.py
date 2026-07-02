from __future__ import annotations

from typing import Dict, List

from app.services.realistic_pnl_service import RealisticPnlService


class PaperTradingService:
    """A lightweight paper-trading engine for virtual order execution."""

    def __init__(self) -> None:
        self.positions: List[Dict[str, object]] = []
        self.closed_trades: List[Dict[str, object]] = []
        self.equity = 100000.0
        self.pnl = 0.0
        self.pnl_service = RealisticPnlService()

    def execute_trade(self, symbol: str, entry_price: float, quantity: int, stop_loss: float, action: str) -> Dict[str, object]:
        trade = {
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "action": action,
        }
        self.positions.append(trade)
        self.equity = float(entry_price)
        return trade

    def close_trade(self, symbol: str, exit_price: float) -> Dict[str, object]:
        position = next((item for item in self.positions if item["symbol"] == symbol), None)
        if position is None:
            raise ValueError(f"no open position for {symbol}")
        self.positions.remove(position)
        breakdown = self.pnl_service.calculate(
            entry_price=float(position["entry_price"]),
            exit_price=exit_price,
            quantity=int(position["quantity"]),
            side="BUY" if str(position.get("action", "")).upper().startswith("BUY") else "SELL",
        )
        pnl = breakdown.net_pnl
        self.pnl += pnl
        self.closed_trades.append({
            "symbol": symbol,
            "entry_price": position["entry_price"],
            "exit_price": exit_price,
            "quantity": position["quantity"],
            "gross_pnl": breakdown.gross_pnl,
            "charges": breakdown.charges,
            "slippage_cost": breakdown.slippage_cost,
            "spread_cost": breakdown.spread_cost,
            "net_pnl": pnl,
            "pnl": pnl,
        })
        return self.closed_trades[-1]

    def get_summary(self) -> Dict[str, object]:
        return {
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
            "equity": self.equity,
            "pnl": self.pnl,
        }
