from __future__ import annotations

import sys
from typing import Optional

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.append(str(VENV_SITE_PACKAGES))

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings
from app.services.time_utils import ist_now_naive

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


class OptionQuoteSnapshot(Base):
    __tablename__ = "option_quote_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    underlying = Column(String(50), nullable=False, index=True)
    tradingsymbol = Column(String(100), nullable=False, index=True)
    exchange = Column(String(20), nullable=False, default="NFO")
    timestamp = Column(DateTime, nullable=False, index=True)
    expiry = Column(String(20), nullable=False, index=True)
    strike = Column(Float, nullable=False, index=True)
    option_type = Column(String(5), nullable=False, index=True)
    last_price = Column(Float, nullable=False, default=0.0)
    bid = Column(Float, nullable=False, default=0.0)
    ask = Column(Float, nullable=False, default=0.0)
    implied_volatility = Column(Float, nullable=True)
    delta = Column(Float, nullable=True)
    gamma = Column(Float, nullable=True)
    theta = Column(Float, nullable=True)
    vega = Column(Float, nullable=True)
    open_interest = Column(Float, nullable=False, default=0.0)
    volume = Column(Float, nullable=False, default=0.0)


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
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
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


class RejectedOpportunityRecord(Base):
    __tablename__ = "rejected_opportunities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    action = Column(String(20), nullable=True, index=True)
    side = Column(String(10), nullable=False, index=True)
    tradingsymbol = Column(String(100), nullable=True, index=True)
    exchange = Column(String(20), nullable=True)
    expiry = Column(String(20), nullable=True, index=True)
    strike = Column(Float, nullable=True)
    option_type = Column(String(5), nullable=True, index=True)
    score = Column(Integer, nullable=False, default=0, index=True)
    primary_gate = Column(String(100), nullable=True, index=True)
    reasons_json = Column(Text, nullable=False)
    market_state_json = Column(Text, nullable=True)
    option_quality_json = Column(Text, nullable=True)
    premium_state_json = Column(Text, nullable=True)
    score_breakdown_json = Column(Text, nullable=True)
    factor_scores_json = Column(Text, nullable=True)
    later_outcome = Column(String(30), nullable=True, index=True)
    later_exit_price = Column(Float, nullable=True)
    later_evaluated_at = Column(DateTime, nullable=True)
    later_notes = Column(Text, nullable=True)


class TradeRecord(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    updated_at = Column(DateTime, nullable=False, default=ist_now_naive)
    opportunity_id = Column(Integer, nullable=True, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    tradingsymbol = Column(String(100), nullable=False, index=True)
    exchange = Column(String(20), nullable=False, default="NFO")
    instrument_token = Column(Integer, nullable=True, index=True)
    action = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False, index=True)
    mode = Column(String(20), nullable=False, index=True)
    status = Column(String(30), nullable=False, default="created", index=True)
    broker_order_id = Column(String(100), nullable=True, index=True)
    requested_quantity = Column(Integer, nullable=False, default=0)
    placed_quantity = Column(Integer, nullable=False, default=0)
    filled_quantity = Column(Integer, nullable=False, default=0)
    entry_price = Column(Float, nullable=True)
    average_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    target_1 = Column(Float, nullable=True)
    target_2 = Column(Float, nullable=True)
    target_3 = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_order_id = Column(String(100), nullable=True, index=True)
    exit_order_status = Column(String(50), nullable=True, index=True)
    exit_order_response_json = Column(Text, nullable=True)
    exit_attempt_count = Column(Integer, nullable=False, default=0)
    exit_last_error = Column(Text, nullable=True)
    exit_requested_at = Column(DateTime, nullable=True)
    exit_confirmed_at = Column(DateTime, nullable=True)
    price_source = Column(String(50), nullable=True)
    price_timestamp = Column(DateTime, nullable=True)
    price_age_seconds = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    gross_pnl = Column(Float, nullable=True)
    net_pnl = Column(Float, nullable=True)
    charges = Column(Float, nullable=True)
    slippage_cost = Column(Float, nullable=True)
    spread_cost = Column(Float, nullable=True)
    remaining_quantity = Column(Integer, nullable=True)
    partial_exit_json = Column(Text, nullable=True)
    outcome = Column(String(30), nullable=True, index=True)
    order_response_json = Column(Text, nullable=True)
    broker_status_json = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)


class StrategyValidationRecord(Base):
    __tablename__ = "strategy_validations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    strategy_name = Column(String(100), nullable=False, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(20), nullable=False, index=True)
    direction = Column(String(20), nullable=False, index=True)
    mode = Column(String(50), nullable=False, index=True)
    trades = Column(Integer, nullable=False, default=0)
    win_rate = Column(Float, nullable=False, default=0.0)
    expectancy_pct = Column(Float, nullable=False, default=0.0)
    profit_factor = Column(Float, nullable=True)
    max_drawdown_pct = Column(Float, nullable=False, default=0.0)
    passed = Column(Integer, nullable=False, default=0, index=True)
    result_json = Column(Text, nullable=False)


def init_db(database_url: Optional[str] = None) -> None:
    global engine, SessionLocal
    url = database_url or settings.database_url
    connect_args = {"connect_timeout": 5} if url.startswith("mysql") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    _ensure_opportunity_columns()
    _ensure_trade_columns()
    _ensure_strategy_validation_columns()


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


def _ensure_trade_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("trades")}
    required = {
        "opportunity_id": "INTEGER",
        "broker_status_json": "TEXT",
        "notes": "TEXT",
        "outcome": "VARCHAR(30)",
        "pnl": "FLOAT",
        "gross_pnl": "FLOAT",
        "net_pnl": "FLOAT",
        "charges": "FLOAT",
        "slippage_cost": "FLOAT",
        "spread_cost": "FLOAT",
        "remaining_quantity": "INTEGER",
        "partial_exit_json": "TEXT",
        "instrument_token": "INTEGER",
        "exit_order_id": "VARCHAR(100)",
        "exit_order_status": "VARCHAR(50)",
        "exit_order_response_json": "TEXT",
        "exit_attempt_count": "INTEGER DEFAULT 0",
        "exit_last_error": "TEXT",
        "exit_requested_at": "DATETIME",
        "exit_confirmed_at": "DATETIME",
        "price_source": "VARCHAR(50)",
        "price_timestamp": "DATETIME",
        "price_age_seconds": "FLOAT",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE trades ADD COLUMN {column} {column_type}"))


def _ensure_strategy_validation_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "strategy_validations" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("strategy_validations")}
    required = {
        "max_drawdown_pct": "FLOAT",
        "passed": "INTEGER",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE strategy_validations ADD COLUMN {column} {column_type}"))


def get_session():
    if SessionLocal is None:
        init_db()
    else:
        _ensure_opportunity_columns()
        _ensure_trade_columns()
        _ensure_strategy_validation_columns()
    return SessionLocal()
