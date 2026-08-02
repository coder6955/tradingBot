from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable

from app.config import settings
from app.services.database import AccountEquitySnapshotRecord, get_session
from app.services.time_utils import ist_now_naive


@dataclass(frozen=True)
class AccountEquitySnapshot:
    snapshot_id: str
    created_at: datetime
    trading_date: str
    start_of_day_equity: float
    current_audited_equity: float
    peak_equity: float
    peak_to_current_drawdown_percent: float
    realized_pnl: float
    unrealized_pnl_executable_bid: float
    reserved_planned_risk: float
    open_stop_risk: float
    daily_planned_risk: float
    daily_realized_loss: float
    daily_total_equity_change: float
    premium_exposure: float
    consecutive_losses: int
    recovery_after_losses: bool
    executable_bid_coverage_percent: float
    missing_bid_trade_ids: tuple[int, ...]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AccountEquityStateService:
    """Reproducible account-equity state using executable bid for open options."""

    def __init__(
        self,
        *,
        trade_repository: Any,
        account_funds_service: Any,
        executable_bid_provider: Callable[[Any], float | None] | None = None,
    ) -> None:
        self.trade_repository = trade_repository
        self.account_funds_service = account_funds_service
        self.executable_bid_provider = executable_bid_provider

    def snapshot(self, *, persist: bool = True) -> AccountEquitySnapshot:
        now = ist_now_naive()
        trading_date = now.date().isoformat()
        summary = self.trade_repository.daily_summary()
        exposure = self.trade_repository.open_exposure_summary()
        open_trades = self.trade_repository.open_trades()
        today_trades = self.trade_repository.today_trades()
        available_cash = self._available_cash()
        unrealized = 0.0
        open_market_value = 0.0
        open_stop_risk = 0.0
        missing: list[int] = []
        covered = 0
        for trade in open_trades:
            quantity = int(
                getattr(trade, "remaining_quantity", None)
                or getattr(trade, "filled_quantity", None)
                or getattr(trade, "placed_quantity", None)
                or getattr(trade, "requested_quantity", None)
                or 0
            )
            entry = float(
                getattr(trade, "average_price", None)
                or getattr(trade, "entry_price", None)
                or 0.0
            )
            bid = self._bid(trade)
            if bid is None:
                # A missing executable exit is valued at zero for risk, never at LTP.
                bid = 0.0
                missing.append(int(getattr(trade, "id", 0) or 0))
            else:
                covered += 1
            exit_cost = bid * float(settings.risk_allocated_exit_cost_pct) / 100.0
            executable_exit = max(0.0, bid - exit_cost)
            open_market_value += executable_exit * quantity
            unrealized += (executable_exit - entry) * quantity
            stored_risk = float(getattr(trade, "estimated_loss_at_stop", 0.0) or 0.0)
            stop_loss = float(getattr(trade, "stop_loss", 0.0) or 0.0)
            open_stop_risk += (
                stored_risk
                if stored_risk > 0
                else max(0.0, entry - stop_loss) * quantity
            )
        realized = float(summary.get("pnl") or 0.0)
        current_equity = max(0.0, available_cash + open_market_value)
        prior = self._latest_for_date(trading_date)
        start_equity = (
            float(prior.start_of_day_equity)
            if prior is not None
            else max(0.01, current_equity - realized - unrealized)
        )
        peak_equity = max(
            start_equity,
            current_equity,
            float(prior.peak_equity) if prior is not None else 0.0,
        )
        drawdown = max(
            0.0, (peak_equity - current_equity) / max(peak_equity, 0.01) * 100.0
        )
        planned = sum(
            float(getattr(trade, "approved_risk_amount", 0.0) or 0.0)
            for trade in today_trades
        )
        consecutive_losses = self._consecutive_losses(today_trades)
        snapshot = AccountEquitySnapshot(
            snapshot_id=uuid.uuid4().hex,
            created_at=now,
            trading_date=trading_date,
            start_of_day_equity=round(start_equity, 2),
            current_audited_equity=round(current_equity, 2),
            peak_equity=round(peak_equity, 2),
            peak_to_current_drawdown_percent=round(drawdown, 4),
            realized_pnl=round(realized, 2),
            unrealized_pnl_executable_bid=round(unrealized, 2),
            reserved_planned_risk=round(planned, 2),
            open_stop_risk=round(open_stop_risk, 2),
            daily_planned_risk=round(planned, 2),
            daily_realized_loss=round(max(0.0, -realized), 2),
            daily_total_equity_change=round(current_equity - start_equity, 2),
            premium_exposure=round(float(exposure.get("premium_exposure") or 0.0), 2),
            consecutive_losses=consecutive_losses,
            recovery_after_losses=bool(
                consecutive_losses == 0 and current_equity >= peak_equity
            ),
            executable_bid_coverage_percent=round(
                covered / max(len(open_trades), 1) * 100.0, 2
            )
            if open_trades
            else 100.0,
            missing_bid_trade_ids=tuple(missing),
            source="broker_cash_plus_executable_option_bid",
        )
        if persist:
            self._persist(snapshot)
        return snapshot

    def _persist(self, snapshot: AccountEquitySnapshot) -> None:
        session = get_session()
        try:
            session.add(
                AccountEquitySnapshotRecord(
                    snapshot_id=snapshot.snapshot_id,
                    created_at=snapshot.created_at,
                    trading_date=snapshot.trading_date,
                    start_of_day_equity=snapshot.start_of_day_equity,
                    current_equity=snapshot.current_audited_equity,
                    peak_equity=snapshot.peak_equity,
                    drawdown_percent=snapshot.peak_to_current_drawdown_percent,
                    realized_pnl=snapshot.realized_pnl,
                    unrealized_pnl=snapshot.unrealized_pnl_executable_bid,
                    planned_risk=snapshot.daily_planned_risk,
                    open_stop_risk=snapshot.open_stop_risk,
                    premium_exposure=snapshot.premium_exposure,
                    snapshot_json=json.dumps(
                        snapshot.to_dict(), default=str, sort_keys=True
                    ),
                )
            )
            session.commit()
        finally:
            session.close()

    def _latest_for_date(self, trading_date: str) -> AccountEquitySnapshotRecord | None:
        session = get_session()
        try:
            return (
                session.query(AccountEquitySnapshotRecord)
                .filter(AccountEquitySnapshotRecord.trading_date == trading_date)
                .order_by(
                    AccountEquitySnapshotRecord.created_at.desc(),
                    AccountEquitySnapshotRecord.id.desc(),
                )
                .first()
            )
        finally:
            session.close()

    def _available_cash(self) -> float:
        try:
            return max(0.0, float(self.account_funds_service.available_cash()))
        except Exception:
            return 0.0

    def _bid(self, trade: Any) -> float | None:
        if self.executable_bid_provider is None:
            return None
        try:
            value = self.executable_bid_provider(trade)
            parsed = float(value) if value is not None else 0.0
            return parsed if parsed > 0 else None
        except Exception:
            return None

    def _consecutive_losses(self, trades: list[Any]) -> int:
        closed = [
            trade
            for trade in trades
            if str(getattr(trade, "status", "")).lower() == "closed"
        ]
        closed.sort(
            key=lambda trade: (
                getattr(trade, "updated_at", None)
                or getattr(trade, "created_at", None)
                or datetime.min
            ),
            reverse=True,
        )
        count = 0
        for trade in closed:
            net_pnl = getattr(trade, "net_pnl", None)
            pnl = float(
                net_pnl if net_pnl is not None else getattr(trade, "pnl", 0.0) or 0.0
            )
            if pnl >= 0:
                break
            count += 1
        return count
