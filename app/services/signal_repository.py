from __future__ import annotations

from typing import List, Optional

from app.services.database import SignalRecord, get_session


class SignalRepository:
    """Persist and retrieve generated trading signals."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url
        if database_url is not None:
            from app.services.database import init_db

            init_db(database_url)

    def save_signal(
        self,
        symbol: str,
        action: str,
        score: int,
        confidence: float,
        trend: str,
        explanation: str | None = None,
    ) -> SignalRecord:
        session = get_session()
        try:
            record = SignalRecord(
                symbol=symbol,
                action=action,
                score=score,
                confidence=confidence,
                trend=trend,
                explanation=explanation,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record
        finally:
            session.close()

    def list_signals(self) -> List[SignalRecord]:
        session = get_session()
        try:
            return session.query(SignalRecord).order_by(SignalRecord.id.desc()).all()
        finally:
            session.close()
