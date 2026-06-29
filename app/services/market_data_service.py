from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.database import Candle, get_session


class MarketDataService:
    """Persist and query OHLCV candles for analysis workflows."""

    def __init__(self, session_factory: Optional[Any] = None) -> None:
        self.session_factory = session_factory or get_session

    def save_candles(self, symbol: str, timeframe: str, candles: List[Dict[str, Any]]) -> int:
        session: Session = self.session_factory()
        try:
            for candle in candles:
                timestamp = candle.get("timestamp")
                if isinstance(timestamp, str):
                    parsed_timestamp = datetime.fromisoformat(timestamp)
                else:
                    parsed_timestamp = timestamp
                session.add(
                    Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=parsed_timestamp,
                        open_price=float(candle["open"]),
                        high_price=float(candle["high"]),
                        low_price=float(candle["low"]),
                        close_price=float(candle["close"]),
                        volume=float(candle.get("volume", 0.0)),
                    )
                )
            session.commit()
            return len(candles)
        finally:
            session.close()

    def get_market_summary(self, symbol: str) -> Dict[str, Any]:
        session: Session = self.session_factory()
        try:
            candles = (
                session.query(Candle)
                .filter(Candle.symbol == symbol)
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
