from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.database import Candle, get_session
from app.services.time_utils import to_ist_naive


class MarketDataService:
    """Persist and query OHLCV candles for analysis workflows."""

    def __init__(self, session_factory: Optional[Any] = None) -> None:
        self.session_factory = session_factory or get_session

    def save_candles(self, symbol: str, timeframe: str, candles: List[Dict[str, Any]]) -> int:
        session: Session = self.session_factory()
        try:
            parsed_rows: list[dict[str, Any]] = []
            for candle in candles:
                parsed_timestamp = self._parse_timestamp(candle.get("timestamp") or candle.get("date"))
                if parsed_timestamp is None:
                    continue
                parsed_rows.append({**candle, "timestamp": parsed_timestamp})

            existing_timestamps = set()
            if parsed_rows:
                timestamps = [row["timestamp"] for row in parsed_rows]
                existing_timestamps = {
                    item[0]
                    for item in session.query(Candle.timestamp)
                    .filter(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe, Candle.timestamp.in_(timestamps))
                    .all()
                }

            inserted = 0
            for candle in parsed_rows:
                if candle["timestamp"] in existing_timestamps:
                    continue
                session.add(
                    Candle(
                        symbol=symbol.upper(),
                        timeframe=timeframe,
                        timestamp=candle["timestamp"],
                        open_price=float(candle["open"]),
                        high_price=float(candle["high"]),
                        low_price=float(candle["low"]),
                        close_price=float(candle["close"]),
                        volume=float(candle.get("volume", 0.0)),
                    )
                )
                inserted += 1
            session.commit()
            return inserted
        finally:
            session.close()

    def get_market_summary(self, symbol: str) -> Dict[str, Any]:
        session: Session = self.session_factory()
        try:
            candles = (
                session.query(Candle)
                .filter(Candle.symbol == symbol.upper())
                .order_by(Candle.timestamp.asc())
                .all()
            )
            if not candles:
                return {"symbol": symbol, "count": 0, "latest_close": None}
            latest = candles[-1]
            return {
                "symbol": symbol,
                "count": len(candles),
                "latest_close": latest.close_price,
                "latest_volume": latest.volume,
            }
        finally:
            session.close()

    def latest_candle_timestamp(self, symbol: str, timeframe: str) -> datetime | None:
        session: Session = self.session_factory()
        try:
            row = (
                session.query(Candle.timestamp)
                .filter(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe)
                .order_by(Candle.timestamp.desc())
                .first()
            )
            return row[0] if row else None
        finally:
            session.close()

    def count_candles(self, symbol: str | None = None, timeframe: str | None = None) -> int:
        session: Session = self.session_factory()
        try:
            query = session.query(Candle)
            if symbol:
                query = query.filter(Candle.symbol == symbol.upper())
            if timeframe:
                query = query.filter(Candle.timeframe == timeframe)
            return int(query.count())
        finally:
            session.close()

    def _parse_timestamp(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return to_ist_naive(value)
        text = str(value).replace("Z", "+00:00")
        try:
            return to_ist_naive(datetime.fromisoformat(text))
        except ValueError:
            return None
