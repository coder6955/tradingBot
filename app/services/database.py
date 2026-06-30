from __future__ import annotations

from typing import Optional

from datetime import datetime

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


class OpportunityRecord(Base):
    __tablename__ = "opportunities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    action = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False, index=True)
    tradingsymbol = Column(String(100), nullable=True, index=True)
    exchange = Column(String(20), nullable=True)
    expiry = Column(String(20), nullable=True, index=True)
    strike = Column(Float, nullable=True)
    entry_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    target_1 = Column(Float, nullable=True)
    target_2 = Column(Float, nullable=True)
    target_3 = Column(Float, nullable=True)
    quantity = Column(Integer, nullable=False, default=0)
    lot_size = Column(Integer, nullable=False, default=0)
    score = Column(Integer, nullable=False)
    probability = Column(Float, nullable=False, default=0.0)
    risk_reward = Column(Float, nullable=False, default=0.0)
    status = Column(String(30), nullable=False, default="open", index=True)
    outcome = Column(String(30), nullable=True, index=True)
    exit_price = Column(Float, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    pnl = Column(Float, nullable=True)
    signal_json = Column(Text, nullable=False)
    factor_scores_json = Column(Text, nullable=True)
    review_notes = Column(Text, nullable=True)


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
