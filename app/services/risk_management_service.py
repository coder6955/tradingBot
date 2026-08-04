from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.account_funds_service import AccountFundsService
from app.services.account_equity_state_service import AccountEquityStateService
from app.services.trade_repository import TradeRepository
from app.services.time_utils import ist_now


class RiskManagementService:
    """Account-level kill-switch checks before automated entries."""

    def __init__(
        self,
        trade_repository: TradeRepository | None = None,
        account_funds_service: AccountFundsService | None = None,
        account_equity_state_service: AccountEquityStateService | None = None,
    ) -> None:
        self.trade_repository = trade_repository or TradeRepository()
        self.account_funds_service = account_funds_service or AccountFundsService()
        self.account_equity_state_service = (
            account_equity_state_service
            or AccountEquityStateService(
                trade_repository=self.trade_repository,
                account_funds_service=self.account_funds_service,
            )
        )

    def evaluate_entry(self, order_mode: str | None = None) -> dict[str, Any]:
        mode = str(order_mode or settings.default_order_mode or "paper").lower()
        paper_mode = mode == "paper"
        summary = self.trade_repository.daily_summary()
        reasons: list[str] = []
        # Risk decisions use the same authoritative calculation, but avoid a
        # non-safety append-only snapshot write on the synchronous order path.
        equity_snapshot = self.account_equity_state_service.snapshot(persist=False)

        if int(summary["trades"]) >= settings.max_trades_per_day:
            reasons.append("max trades per day reached")

        broker_available_cash = None if paper_mode else self._available_cash()
        available_cash = (
            max(0.0, float(settings.account_equity))
            if paper_mode
            else max(0.0, float(broker_available_cash or 0.0))
        )
        risk_equity = (
            max(float(settings.account_equity), 0.01)
            if paper_mode
            else max(float(equity_snapshot.current_audited_equity), 0.01)
        )
        start_of_day_risk_equity = (
            max(float(settings.account_equity), 0.01)
            if paper_mode
            else max(float(equity_snapshot.start_of_day_equity), 0.01)
        )
        max_daily_loss = start_of_day_risk_equity * (
            settings.max_realized_daily_loss_percent / 100
        )
        if float(summary["pnl"]) <= -abs(max_daily_loss):
            reasons.append("max daily loss reached")

        if int(summary["stop_losses"]) >= settings.max_stop_losses_per_day:
            reasons.append("max stop losses per day reached")

        consecutive_losses = equity_snapshot.consecutive_losses
        if consecutive_losses >= settings.max_consecutive_losses:
            reasons.append("max consecutive losses reached")

        cooldown_reason = self._cooldown_reason()
        if cooldown_reason:
            reasons.append(cooldown_reason)

        exposure = self.trade_repository.open_exposure_summary()
        if int(exposure["open_trades"]) >= settings.max_open_trades:
            reasons.append("max open trades reached")
        max_open_premium = available_cash * (
            settings.max_open_premium_exposure_pct / 100
        )
        if float(exposure["premium_exposure"]) >= max_open_premium:
            reasons.append("max open premium exposure reached")
        if equity_snapshot.missing_bid_trade_ids:
            reasons.append("open executable bid coverage is incomplete")

        risk_exposure = {
            "planned_risk_today": equity_snapshot.daily_planned_risk,
            "total_open_risk": equity_snapshot.open_stop_risk,
            "banknifty_open_risk": equity_snapshot.open_stop_risk,
            "planned_risk_today_pct": round(
                equity_snapshot.daily_planned_risk / risk_equity * 100.0, 4
            ),
            "total_open_risk_pct": round(
                equity_snapshot.open_stop_risk / risk_equity * 100.0, 4
            ),
        }

        return {
            "passed": not reasons,
            "reasons": reasons,
            "summary": summary,
            "open_exposure": exposure,
            "limits": {
                "max_daily_loss": round(max_daily_loss, 2),
                "available_cash": round(available_cash, 2),
                "current_audited_equity": round(risk_equity, 2),
                "start_of_day_equity": round(start_of_day_risk_equity, 2),
                "account_equity_source": (
                    "configured_paper_equity" if paper_mode else "kite_margins"
                ),
                "available_cash_source": (
                    "configured_paper_equity" if paper_mode else "kite_margins"
                ),
                "broker_audited_equity": equity_snapshot.current_audited_equity,
                "max_daily_loss_pct": settings.max_daily_loss_pct,
                "max_realized_daily_loss_percent": settings.max_realized_daily_loss_percent,
                "max_daily_planned_risk_percent": settings.max_daily_planned_risk_percent,
                "max_total_open_risk_percent": settings.max_total_open_risk_percent,
                "max_banknifty_open_risk_percent": settings.max_banknifty_open_risk_percent,
                "max_consecutive_losses": settings.max_consecutive_losses,
                "max_trades_per_day": settings.max_trades_per_day,
                "max_stop_losses_per_day": settings.max_stop_losses_per_day,
                "max_open_trades": settings.max_open_trades,
                "max_symbol_open_trades": settings.max_symbol_open_trades,
                "max_open_premium_exposure": round(max_open_premium, 2),
                "max_open_premium_exposure_pct": settings.max_open_premium_exposure_pct,
                "cooldown_after_stop_minutes": settings.cooldown_after_stop_minutes,
            },
            "risk_state": {
                "realized_daily_pnl": equity_snapshot.realized_pnl,
                "unrealized_daily_pnl": equity_snapshot.unrealized_pnl_executable_bid,
                "current_drawdown_pct": (
                    round(
                        max(
                            0.0,
                            -(
                                equity_snapshot.realized_pnl
                                + equity_snapshot.unrealized_pnl_executable_bid
                            )
                            / risk_equity
                            * 100.0,
                        ),
                        4,
                    )
                    if paper_mode
                    else equity_snapshot.peak_to_current_drawdown_percent
                ),
                "current_audited_equity": round(risk_equity, 2),
                "start_of_day_equity": round(start_of_day_risk_equity, 2),
                "peak_equity": (
                    round(risk_equity, 2) if paper_mode else equity_snapshot.peak_equity
                ),
                "daily_total_equity_change": (
                    round(
                        equity_snapshot.realized_pnl
                        + equity_snapshot.unrealized_pnl_executable_bid,
                        2,
                    )
                    if paper_mode
                    else equity_snapshot.daily_total_equity_change
                ),
                "account_equity_source": (
                    "configured_paper_equity" if paper_mode else "kite_margins"
                ),
                "broker_audited_equity": equity_snapshot.current_audited_equity,
                "premium_exposure": equity_snapshot.premium_exposure,
                "executable_bid_coverage_percent": equity_snapshot.executable_bid_coverage_percent,
                "consecutive_losses": equity_snapshot.consecutive_losses,
                **risk_exposure,
            },
        }

    def _available_cash(self) -> float:
        try:
            return self.account_funds_service.available_cash()
        except Exception:
            return 0.0

    def evaluate_signal(
        self, symbol: str, order_mode: str | None = None
    ) -> dict[str, Any]:
        result = self.evaluate_entry(order_mode=order_mode)
        exposure = result["open_exposure"]
        symbol_count = int(exposure.get("by_symbol", {}).get(symbol, 0))
        if symbol_count >= settings.max_symbol_open_trades:
            result = dict(result)
            result["reasons"] = list(result["reasons"]) + [
                "max open trades for symbol reached"
            ]
            result["passed"] = False
        return result

    def _cooldown_reason(self) -> str | None:
        if settings.cooldown_after_stop_minutes <= 0:
            return None
        trades = self.trade_repository.today_trades()
        stop_trades = [
            trade
            for trade in trades
            if trade.outcome == "stop_loss" and trade.updated_at
        ]
        if not stop_trades:
            return None
        latest = max(stop_trades, key=lambda trade: trade.updated_at)
        latest_ist = (
            latest.updated_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            if latest.updated_at.tzinfo is None
            else latest.updated_at.astimezone(ZoneInfo("Asia/Kolkata"))
        )
        cooldown_until = latest_ist + timedelta(
            minutes=settings.cooldown_after_stop_minutes
        )
        if ist_now() < cooldown_until:
            return "cooldown after stop loss is active"
        return None

    def _consecutive_losses(self) -> int:
        closed = [
            trade
            for trade in self.trade_repository.today_trades()
            if trade.status == "closed"
        ]
        closed.sort(
            key=lambda trade: trade.updated_at or trade.created_at, reverse=True
        )
        count = 0
        for trade in closed:
            pnl = float(
                trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0
            )
            if pnl >= 0:
                break
            count += 1
        return count

    def _risk_exposure(self, available_cash: float) -> dict[str, float]:
        planned_risk = 0.0
        total_open_risk = 0.0
        banknifty_open_risk = 0.0
        for trade in self.trade_repository.today_trades():
            approved = float(getattr(trade, "approved_risk_amount", 0.0) or 0.0)
            planned_risk += approved
        for trade in self.trade_repository.open_trades():
            quantity = int(
                trade.remaining_quantity
                or trade.filled_quantity
                or trade.placed_quantity
                or trade.requested_quantity
                or 0
            )
            estimated = float(getattr(trade, "estimated_loss_at_stop", 0.0) or 0.0)
            if estimated <= 0:
                entry = float(trade.average_price or trade.entry_price or 0.0)
                stop = float(trade.stop_loss or 0.0)
                estimated = max(0.0, entry - stop) * quantity
            total_open_risk += estimated
            if str(trade.symbol or "").upper() == "BANKNIFTY":
                banknifty_open_risk += estimated
        return {
            "planned_risk_today": round(planned_risk, 2),
            "total_open_risk": round(total_open_risk, 2),
            "banknifty_open_risk": round(banknifty_open_risk, 2),
            "planned_risk_today_pct": round(
                planned_risk / max(available_cash, 0.01) * 100.0, 4
            ),
            "total_open_risk_pct": round(
                total_open_risk / max(available_cash, 0.01) * 100.0, 4
            ),
        }
