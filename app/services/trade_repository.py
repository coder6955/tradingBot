from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.models import Signal
from app.services.database import TradeRecord, get_session


class TradeRepository:
    """Persist actual paper/live trade lifecycle events separately from opportunities."""

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
            record.updated_at = datetime.utcnow()
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
            record.updated_at = datetime.utcnow()
            record.notes = notes or record.notes
            qty = record.filled_quantity or record.placed_quantity
            if record.average_price is not None:
                multiplier = 1 if record.side == "BUY" else -1
                record.pnl = (exit_price - record.average_price) * qty * multiplier
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def today_trades(self) -> list[TradeRecord]:
        session = get_session()
        try:
            today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
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
        pnl = sum(float(trade.pnl or 0.0) for trade in closed)
        return {
            "date": date.today().isoformat(),
            "trades": len(trades),
            "open": len([trade for trade in trades if trade.status != "closed"]),
            "closed": len(closed),
            "stop_losses": len(stop_losses),
            "pnl": round(pnl, 2),
        }
