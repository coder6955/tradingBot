from __future__ import annotations

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class PnlBreakdown:
    gross_pnl: float
    net_pnl: float
    charges: float
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    gst: float
    stamp: float
    slippage_cost: float
    spread_cost: float

    def to_dict(self) -> dict[str, float]:
        return {
            "gross_pnl": round(self.gross_pnl, 2),
            "net_pnl": round(self.net_pnl, 2),
            "charges": round(self.charges, 2),
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_txn": round(self.exchange_txn, 2),
            "sebi": round(self.sebi, 4),
            "gst": round(self.gst, 2),
            "stamp": round(self.stamp, 2),
            "slippage_cost": round(self.slippage_cost, 2),
            "spread_cost": round(self.spread_cost, 2),
        }


class RealisticPnlService:
    """Estimate net options P&L after Indian market costs and execution friction."""

    def calculate(
        self,
        *,
        entry_price: float,
        exit_price: float,
        quantity: int,
        side: str = "BUY",
        include_slippage: bool = True,
        include_spread: bool = True,
    ) -> PnlBreakdown:
        qty = max(0, int(quantity or 0))
        entry_value = max(0.0, float(entry_price or 0.0)) * qty
        exit_value = max(0.0, float(exit_price or 0.0)) * qty
        multiplier = 1 if str(side).upper() == "BUY" else -1
        gross = (exit_value - entry_value) * multiplier

        brokerage = settings.estimated_brokerage_per_order * 2 if qty else 0.0
        sell_turnover = exit_value if str(side).upper() == "BUY" else entry_value
        buy_turnover = entry_value if str(side).upper() == "BUY" else exit_value
        total_turnover = entry_value + exit_value
        stt = sell_turnover * (settings.estimated_stt_sell_pct / 100)
        exchange_txn = total_turnover * (settings.estimated_exchange_txn_pct / 100)
        sebi = total_turnover * (settings.estimated_sebi_pct / 100)
        gst = (brokerage + exchange_txn + sebi) * (settings.estimated_gst_pct / 100)
        stamp = buy_turnover * (settings.estimated_stamp_buy_pct / 100)
        slippage_cost = (
            total_turnover * (settings.paper_slippage_pct_per_side / 100)
            if include_slippage
            else 0.0
        )
        spread_cost = (
            total_turnover * (settings.paper_spread_impact_pct_per_side / 100)
            if include_spread
            else 0.0
        )
        charges = (
            brokerage
            + stt
            + exchange_txn
            + sebi
            + gst
            + stamp
            + slippage_cost
            + spread_cost
        )
        net = gross - charges
        return PnlBreakdown(
            gross_pnl=gross,
            net_pnl=net,
            charges=charges,
            brokerage=brokerage,
            stt=stt,
            exchange_txn=exchange_txn,
            sebi=sebi,
            gst=gst,
            stamp=stamp,
            slippage_cost=slippage_cost,
            spread_cost=spread_cost,
        )

    def net_exit_price_for_backtest(
        self, *, raw_exit_price: float, side: str = "BUY"
    ) -> float:
        friction = (
            settings.backtest_slippage_pct + settings.paper_spread_impact_pct_per_side
        )
        if str(side).upper() == "BUY":
            return raw_exit_price * (1 - friction / 100)
        return raw_exit_price * (1 + friction / 100)
