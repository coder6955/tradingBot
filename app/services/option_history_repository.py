from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.database import OptionQuoteSnapshot, get_session
from app.services.time_utils import format_ist_space, ist_now_naive, to_ist_naive


class OptionHistoryRepository:
    """Store and query historical option-chain quote snapshots for exact backtesting."""

    def import_snapshots(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        session = get_session()
        inserted = 0
        try:
            for row in rows:
                snapshot = OptionQuoteSnapshot(
                    underlying=str(row.get("underlying") or row.get("symbol") or "").upper(),
                    tradingsymbol=str(row.get("tradingsymbol") or ""),
                    exchange=str(row.get("exchange") or "NFO"),
                    timestamp=self._parse_datetime(row.get("timestamp")),
                    expiry=str(row.get("expiry") or ""),
                    strike=float(row.get("strike") or 0),
                    option_type=str(row.get("option_type") or row.get("instrument_type") or "").upper(),
                    last_price=float(row.get("last_price") or row.get("ltp") or 0),
                    bid=float(row.get("bid") or 0),
                    ask=float(row.get("ask") or 0),
                    implied_volatility=self._optional_float(row.get("implied_volatility") or row.get("iv")),
                    delta=self._optional_float(row.get("delta")),
                    gamma=self._optional_float(row.get("gamma")),
                    theta=self._optional_float(row.get("theta")),
                    vega=self._optional_float(row.get("vega")),
                    open_interest=float(row.get("open_interest") or row.get("oi") or 0),
                    volume=float(row.get("volume") or 0),
                )
                if not snapshot.underlying or not snapshot.tradingsymbol or not snapshot.expiry or not snapshot.option_type:
                    continue
                session.add(snapshot)
                inserted += 1
            session.commit()
            return {"status": "ok", "inserted": inserted}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def count_snapshots(self, underlying: str | None = None) -> int:
        session = get_session()
        try:
            query = session.query(OptionQuoteSnapshot)
            if underlying:
                query = query.filter(OptionQuoteSnapshot.underlying == underlying.upper())
            return int(query.count())
        finally:
            session.close()

    def latest_snapshots(self, underlying: str | None = None, limit: int = 20) -> list[OptionQuoteSnapshot]:
        session = get_session()
        try:
            query = session.query(OptionQuoteSnapshot)
            if underlying:
                query = query.filter(OptionQuoteSnapshot.underlying == underlying.upper())
            return (
                query.order_by(OptionQuoteSnapshot.timestamp.desc(), OptionQuoteSnapshot.id.desc())
                .limit(limit)
                .all()
            )
        finally:
            session.close()

    def coverage_summary(self, underlying: str | None = None) -> dict[str, Any]:
        session = get_session()
        try:
            query = session.query(OptionQuoteSnapshot)
            if underlying:
                query = query.filter(OptionQuoteSnapshot.underlying == underlying.upper())
            rows = query.all()
            if not rows:
                return {
                    "underlying": underlying.upper() if underlying else "ALL",
                    "snapshots": 0,
                    "contracts": 0,
                    "first_timestamp": None,
                    "latest_timestamp": None,
                    "trading_days": 0,
                    "ce_snapshots": 0,
                    "pe_snapshots": 0,
                    "with_bid_ask_pct": 0.0,
                    "with_greeks_pct": 0.0,
                }
            timestamps = [row.timestamp for row in rows if row.timestamp]
            contracts = {row.tradingsymbol for row in rows if row.tradingsymbol}
            trading_days = {row.timestamp.date().isoformat() for row in rows if row.timestamp}
            with_bid_ask = [row for row in rows if float(row.bid or 0) > 0 and float(row.ask or 0) > 0]
            with_greeks = [row for row in rows if row.delta is not None and row.theta is not None and row.implied_volatility is not None]
            return {
                "underlying": underlying.upper() if underlying else "ALL",
                "snapshots": len(rows),
                "contracts": len(contracts),
                "first_timestamp": format_ist_space(min(timestamps)) if timestamps else None,
                "latest_timestamp": format_ist_space(max(timestamps)) if timestamps else None,
                "trading_days": len(trading_days),
                "ce_snapshots": len([row for row in rows if row.option_type == "CE"]),
                "pe_snapshots": len([row for row in rows if row.option_type == "PE"]),
                "with_bid_ask_pct": round((len(with_bid_ask) / len(rows)) * 100, 2),
                "with_greeks_pct": round((len(with_greeks) / len(rows)) * 100, 2),
            }
        finally:
            session.close()

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return to_ist_naive(value)
        if value is None:
            return ist_now_naive()
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return to_ist_naive(parsed)

    def _optional_float(self, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return float(value)
