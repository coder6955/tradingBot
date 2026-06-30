from __future__ import annotations

import sys
from typing import Optional

from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.append(str(VENV_SITE_PACKAGES))

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine, inspect, text
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
    failure_tags_json = Column(Text, nullable=True)
    review_notes = Column(Text, nullable=True)


def init_db(database_url: Optional[str] = None) -> None:
    global engine, SessionLocal
    url = database_url or settings.database_url
    connect_args = {"connect_timeout": 5} if url.startswith("mysql") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    _ensure_opportunity_columns()


def _ensure_opportunity_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "opportunities" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("opportunities")}
    if "failure_tags_json" in columns:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE opportunities ADD COLUMN failure_tags_json TEXT"))


def get_session():
    if SessionLocal is None:
        init_db()
    else:
        _ensure_opportunity_columns()
    return SessionLocal()
