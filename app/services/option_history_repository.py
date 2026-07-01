from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.database import OptionQuoteSnapshot, get_session


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

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if value is None:
            return datetime.utcnow()
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return parsed.replace(tzinfo=None)

    def _optional_float(self, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return float(value)
