from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.account_funds_service import AccountFundsService
from app.services.trade_repository import TradeRepository
from app.services.time_utils import ist_now


class RiskManagementService:
    """Account-level kill-switch checks before automated entries."""

    def __init__(self, trade_repository: TradeRepository | None = None, account_funds_service: AccountFundsService | None = None) -> None:
        self.trade_repository = trade_repository or TradeRepository()
        self.account_funds_service = account_funds_service or AccountFundsService()

    def evaluate_entry(self) -> dict[str, Any]:
        summary = self.trade_repository.daily_summary()
        reasons: list[str] = []

        if int(summary["trades"]) >= settings.max_trades_per_day:
            reasons.append("max trades per day reached")

        available_cash = self._available_cash()
        max_daily_loss = available_cash * (settings.max_daily_loss_pct / 100)
        if float(summary["pnl"]) <= -abs(max_daily_loss):
            reasons.append("max daily loss reached")

        if int(summary["stop_losses"]) >= settings.max_stop_losses_per_day:
            reasons.append("max stop losses per day reached")

        cooldown_reason = self._cooldown_reason()
        if cooldown_reason:
            reasons.append(cooldown_reason)

        exposure = self.trade_repository.open_exposure_summary()
        if int(exposure["open_trades"]) >= settings.max_open_trades:
            reasons.append("max open trades reached")
        max_open_premium = available_cash * (settings.max_open_premium_exposure_pct / 100)
        if float(exposure["premium_exposure"]) >= max_open_premium:
            reasons.append("max open premium exposure reached")

        return {
            "passed": not reasons,
            "reasons": reasons,
            "summary": summary,
            "open_exposure": exposure,
            "limits": {
                "max_daily_loss": round(max_daily_loss, 2),
                "available_cash": round(available_cash, 2),
                "available_cash_source": "kite_margins",
                "max_daily_loss_pct": settings.max_daily_loss_pct,
                "max_trades_per_day": settings.max_trades_per_day,
                "max_stop_losses_per_day": settings.max_stop_losses_per_day,
                "max_open_trades": settings.max_open_trades,
                "max_symbol_open_trades": settings.max_symbol_open_trades,
                "max_open_premium_exposure": round(max_open_premium, 2),
                "max_open_premium_exposure_pct": settings.max_open_premium_exposure_pct,
                "cooldown_after_stop_minutes": settings.cooldown_after_stop_minutes,
            },
        }

    def _available_cash(self) -> float:
        try:
            return self.account_funds_service.available_cash()
        except Exception:
            return 0.0

    def evaluate_signal(self, symbol: str) -> dict[str, Any]:
        result = self.evaluate_entry()
        exposure = result["open_exposure"]
        symbol_count = int(exposure.get("by_symbol", {}).get(symbol, 0))
        if symbol_count >= settings.max_symbol_open_trades:
            result = dict(result)
            result["reasons"] = list(result["reasons"]) + ["max open trades for symbol reached"]
            result["passed"] = False
        return result

    def _cooldown_reason(self) -> str | None:
        if settings.cooldown_after_stop_minutes <= 0:
            return None
        trades = self.trade_repository.today_trades()
        stop_trades = [trade for trade in trades if trade.outcome == "stop_loss" and trade.updated_at]
        if not stop_trades:
            return None
        latest = max(stop_trades, key=lambda trade: trade.updated_at)
        latest_ist = latest.updated_at.replace(tzinfo=ZoneInfo("Asia/Kolkata")) if latest.updated_at.tzinfo is None else latest.updated_at.astimezone(ZoneInfo("Asia/Kolkata"))
        cooldown_until = latest_ist + timedelta(minutes=settings.cooldown_after_stop_minutes)
        if ist_now() < cooldown_until:
            return "cooldown after stop loss is active"
        return None
