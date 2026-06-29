from __future__ import annotations

from typing import Optional

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

Base = declarative_base()
engine = None
SessionLocal = None


class Candle(Base):
    __tablename__ = "candles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    open_price = Column(Float, nullable=False)
    high_price = Column(Float, nullable=False)
    low_price = Column(Float, nullable=False)
    close_price = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)


class SignalRecord(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    action = Column(String(20), nullable=False)
    score = Column(Integer, nullable=False)
    confidence = Column(Float, nullable=False)
    trend = Column(String(20), nullable=False)
    explanation = Column(Text, nullable=True)


def init_db(database_url: Optional[str] = None) -> None:
    global engine, SessionLocal
    url = database_url or settings.database_url
    engine = create_engine(url, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)


def get_session():
    if SessionLocal is None:
        init_db()
    return SessionLocal()
