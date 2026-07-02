from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, time
from typing import Any

from app.models import Signal
from app.services.database import TradeRecord, get_session
from app.services.realistic_pnl_service import RealisticPnlService
from app.services.time_utils import ist_now_naive, ist_today


class TradeRepository:
    """Persist actual paper/live trade lifecycle events separately from opportunities."""

    def __init__(self, pnl_service: RealisticPnlService | None = None) -> None:
        self.pnl_service = pnl_service or RealisticPnlService()

    def create_trade(
        self,
        signal: Signal,
        *,
        mode: str,
        status: str,
        requested_quantity: int,
        placed_quantity: int,
        order_response: dict[str, Any] | None = None,
        broker_order_id: str | None = None,
        opportunity_id: int | None = None,
        notes: str | None = None,
    ) -> TradeRecord:
        session = get_session()
        try:
            record = TradeRecord(
                opportunity_id=opportunity_id,
                symbol=signal.symbol,
                tradingsymbol=str(signal.tradingsymbol or signal.symbol),
                exchange=signal.exchange,
                action=signal.action,
                side=signal.side,
                mode=mode,
                status=status,
                broker_order_id=broker_order_id,
                requested_quantity=requested_quantity,
                placed_quantity=placed_quantity,
                filled_quantity=placed_quantity if mode == "paper" else 0,
                remaining_quantity=placed_quantity,
                entry_price=signal.entry_price,
                average_price=signal.entry_price if mode == "paper" else None,
                stop_loss=signal.stop_loss,
                target_1=signal.target_1,
                target_2=signal.target_2,
                target_3=signal.target_3,
                order_response_json=json.dumps(order_response or asdict(signal), default=str),
                notes=notes,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def list_trades(self, status: str | None = None, limit: int = 100) -> list[TradeRecord]:
        session = get_session()
        try:
            query = session.query(TradeRecord).order_by(TradeRecord.id.desc())
            if status:
                query = query.filter(TradeRecord.status == status)
            return query.limit(limit).all()
        finally:
            session.close()

    def get_trade(self, trade_id: int) -> TradeRecord | None:
        session = get_session()
        try:
            return session.get(TradeRecord, trade_id)
        finally:
            session.close()

    def update_broker_status(self, trade_id: int, *, status: str, broker_payload: dict[str, Any], filled_quantity: int | None = None, average_price: float | None = None) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.status = status
            record.updated_at = ist_now_naive()
            record.broker_status_json = json.dumps(broker_payload, default=str)
            if filled_quantity is not None:
                record.filled_quantity = filled_quantity
            if average_price is not None:
                record.average_price = average_price
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def close_trade(self, trade_id: int, *, outcome: str, exit_price: float, notes: str | None = None) -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            record.status = "closed"
            record.outcome = outcome
            record.exit_price = exit_price
            record.updated_at = ist_now_naive()
            record.notes = notes or record.notes
            qty = record.remaining_quantity if record.remaining_quantity is not None else (record.filled_quantity or record.placed_quantity)
            if record.average_price is not None:
                pnl = self.pnl_service.calculate(
                    entry_price=float(record.average_price),
                    exit_price=float(exit_price),
                    quantity=int(qty or 0),
                    side=str(record.side),
                )
                record.gross_pnl = pnl.gross_pnl
                record.net_pnl = pnl.net_pnl
                record.charges = pnl.charges
                record.slippage_cost = pnl.slippage_cost
                record.spread_cost = pnl.spread_cost
                record.pnl = pnl.net_pnl
                record.remaining_quantity = 0
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def record_partial_exit(self, trade_id: int, *, quantity: int, exit_price: float, outcome: str = "partial_target_1") -> TradeRecord:
        session = get_session()
        try:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                raise ValueError(f"trade {trade_id} was not found")
            existing = json.loads(record.partial_exit_json or "[]")
            remaining = int(record.remaining_quantity or record.filled_quantity or record.placed_quantity or 0)
            close_qty = min(max(0, int(quantity)), remaining)
            if close_qty <= 0:
                return record
            pnl = self.pnl_service.calculate(
                entry_price=float(record.average_price or record.entry_price or 0.0),
                exit_price=float(exit_price),
                quantity=close_qty,
                side=str(record.side),
            )
            existing.append({"outcome": outcome, "quantity": close_qty, "exit_price": exit_price, **pnl.to_dict()})
            record.partial_exit_json = json.dumps(existing, default=str)
            record.remaining_quantity = remaining - close_qty
            record.gross_pnl = float(record.gross_pnl or 0.0) + pnl.gross_pnl
            record.net_pnl = float(record.net_pnl or 0.0) + pnl.net_pnl
            record.charges = float(record.charges or 0.0) + pnl.charges
            record.slippage_cost = float(record.slippage_cost or 0.0) + pnl.slippage_cost
            record.spread_cost = float(record.spread_cost or 0.0) + pnl.spread_cost
            record.pnl = record.net_pnl
            record.updated_at = ist_now_naive()
            if record.remaining_quantity <= 0:
                record.status = "closed"
                record.outcome = outcome
                record.exit_price = exit_price
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def today_trades(self) -> list[TradeRecord]:
        session = get_session()
        try:
            today = ist_today()
            start = datetime.combine(today, time.min)
            end = datetime.combine(today, time.max)
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.created_at >= start)
                .filter(TradeRecord.created_at <= end)
                .order_by(TradeRecord.id.desc())
                .all()
            )
        finally:
            session.close()

    def open_trades(self) -> list[TradeRecord]:
        session = get_session()
        try:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.status != "closed")
                .order_by(TradeRecord.id.desc())
                .all()
            )
        finally:
            session.close()

    def open_trade_for_opportunity(self, opportunity_id: int) -> TradeRecord | None:
        session = get_session()
        try:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.opportunity_id == opportunity_id)
                .filter(TradeRecord.status != "closed")
                .order_by(TradeRecord.id.desc())
                .first()
            )
        finally:
            session.close()

    def open_exposure_summary(self) -> dict[str, Any]:
        trades = self.open_trades()
        by_symbol: dict[str, int] = {}
        premium_exposure = 0.0
        for trade in trades:
            by_symbol[trade.symbol] = by_symbol.get(trade.symbol, 0) + 1
            price = float(trade.average_price or trade.entry_price or 0)
            quantity = int(trade.filled_quantity or trade.placed_quantity or trade.requested_quantity or 0)
            premium_exposure += price * quantity
        return {
            "open_trades": len(trades),
            "by_symbol": by_symbol,
            "premium_exposure": round(premium_exposure, 2),
        }

    def daily_summary(self) -> dict[str, Any]:
        trades = self.today_trades()
        closed = [trade for trade in trades if trade.status == "closed"]
        stop_losses = [trade for trade in closed if trade.outcome == "stop_loss"]
        pnl = sum(float(trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0) for trade in closed)
        return {
            "date": ist_today().isoformat(),
            "trades": len(trades),
            "open": len([trade for trade in trades if trade.status != "closed"]),
            "closed": len(closed),
            "stop_losses": len(stop_losses),
            "pnl": round(pnl, 2),
        }
