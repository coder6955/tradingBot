from __future__ import annotations

import sys
from typing import Optional

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.append(str(VENV_SITE_PACKAGES))

from sqlalchemy import BigInteger, Column, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings
from app.services.time_utils import ist_now_naive

Base = declarative_base()
engine = None
SessionLocal = None


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_candle_series_timestamp"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    open_price = Column(Float, nullable=False)
    high_price = Column(Float, nullable=False)
    low_price = Column(Float, nullable=False)
    close_price = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    instrument_token = Column(Integer, nullable=True, index=True)
    receive_timestamp = Column(DateTime, nullable=True, index=True)
    timestamp_source = Column(String(40), nullable=True, index=True)
    is_generated = Column(Integer, nullable=False, default=0, index=True)
    data_quality = Column(String(50), nullable=True, index=True)


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
    probability = Column(Float, nullable=True)
    heuristic_score_confidence = Column(Float, nullable=True)
    probability_source = Column(String(50), nullable=False, default="unavailable_insufficient_calibration", index=True)
    calibration_version = Column(String(100), nullable=True, index=True)
    strategy_version = Column(String(100), nullable=True, index=True)
    config_hash = Column(String(64), nullable=True, index=True)
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
    rejection_source = Column(String(50), nullable=True, index=True)
    rejection_context = Column(String(50), nullable=True, index=True)
    market_session = Column(String(30), nullable=True, index=True)
    learning_eligible = Column(Integer, nullable=False, default=0, index=True)
    learning_exclusion_reason = Column(String(150), nullable=True)
    episode_id = Column(Integer, nullable=True, index=True)
    episode_key = Column(String(64), nullable=True, index=True)
    strategy_version = Column(String(100), nullable=True, index=True)
    config_hash = Column(String(64), nullable=True, index=True)
    reasons_json = Column(Text, nullable=False)
    market_state_json = Column(Text, nullable=True)
    option_quality_json = Column(Text, nullable=True)
    premium_state_json = Column(Text, nullable=True)
    score_breakdown_json = Column(Text, nullable=True)
    factor_scores_json = Column(Text, nullable=True)
    later_outcome = Column(String(80), nullable=True, index=True)
    later_exit_price = Column(Float, nullable=True)
    later_outcome_at = Column(DateTime, nullable=True, index=True)
    later_outcome_minutes = Column(Float, nullable=True)
    later_outcome_source = Column(String(50), nullable=True, index=True)
    later_outcome_timeframe = Column(String(20), nullable=True, index=True)
    later_outcome_ambiguous = Column(Integer, nullable=False, default=0, index=True)
    later_outcome_confidence = Column(String(30), nullable=True, index=True)
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
    protective_order_id = Column(String(100), nullable=True, index=True)
    protective_order_status = Column(String(50), nullable=True, index=True)
    protective_trigger_price = Column(Float, nullable=True)
    protective_order_response_json = Column(Text, nullable=True)
    protective_last_error = Column(Text, nullable=True)
    protective_requested_at = Column(DateTime, nullable=True)
    protective_cancelled_at = Column(DateTime, nullable=True)
    price_source = Column(String(50), nullable=True)
    price_timestamp = Column(DateTime, nullable=True)
    price_age_seconds = Column(Float, nullable=True)
    exit_rule_first_triggered = Column(String(50), nullable=True, index=True)
    exit_triggered_rules_json = Column(Text, nullable=True)
    exit_ltp = Column(Float, nullable=True)
    exit_best_bid = Column(Float, nullable=True)
    exit_best_ask = Column(Float, nullable=True)
    exit_executable_price = Column(Float, nullable=True)
    exit_depth_coverage = Column(Float, nullable=True)
    exit_spread_pct = Column(Float, nullable=True)
    exit_execution_source = Column(String(60), nullable=True, index=True)
    exit_quote_timestamp = Column(DateTime, nullable=True)
    highest_price_during_trade = Column(Float, nullable=True)
    lowest_price_during_trade = Column(Float, nullable=True)
    mfe_points = Column(Float, nullable=True)
    mfe_percent = Column(Float, nullable=True)
    mae_points = Column(Float, nullable=True)
    mae_percent = Column(Float, nullable=True)
    time_to_mfe = Column(Float, nullable=True)
    time_to_mae = Column(Float, nullable=True)
    mfe_recorded_at = Column(DateTime, nullable=True)
    mae_recorded_at = Column(DateTime, nullable=True)
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
    strategy_version = Column(String(100), nullable=True, index=True)
    config_hash = Column(String(64), nullable=True, index=True)


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
    strategy_version = Column(String(100), nullable=True, index=True)
    config_hash = Column(String(64), nullable=True, index=True)
    fold_count = Column(Integer, nullable=False, default=0)
    out_of_sample_sessions = Column(Integer, nullable=False, default=0)
    result_json = Column(Text, nullable=False)


class SetupEpisodeRecord(Base):
    __tablename__ = "setup_episodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    updated_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    episode_key = Column(String(64), nullable=False, unique=True, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    action = Column(String(20), nullable=True, index=True)
    side = Column(String(10), nullable=False, index=True)
    tradingsymbol = Column(String(100), nullable=True, index=True)
    expiry = Column(String(20), nullable=True, index=True)
    strike = Column(Float, nullable=True)
    trigger_price = Column(Float, nullable=True)
    strategy_version = Column(String(100), nullable=False, index=True)
    config_hash = Column(String(64), nullable=False, index=True)
    observation_count = Column(Integer, nullable=False, default=1)
    independent_outcome = Column(String(80), nullable=True, index=True)
    outcome_source = Column(String(50), nullable=True, index=True)
    outcome_confidence = Column(String(30), nullable=True, index=True)


class RawTickRecord(Base):
    __tablename__ = "raw_ticks"
    __table_args__ = (UniqueConstraint("session_date", "sequence", name="uq_raw_tick_session_sequence"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    session_date = Column(String(20), nullable=False, index=True)
    sequence = Column(BigInteger, nullable=False, index=True)
    instrument_token = Column(Integer, nullable=False, index=True)
    symbol = Column(String(100), nullable=False, index=True)
    last_price = Column(Float, nullable=False)
    bid = Column(Float, nullable=True)
    ask = Column(Float, nullable=True)
    depth_json = Column(Text, nullable=True)
    cumulative_volume = Column(Float, nullable=True)
    exchange_timestamp = Column(DateTime, nullable=True, index=True)
    receive_timestamp = Column(DateTime, nullable=False, index=True)
    timestamp_source = Column(String(40), nullable=False, index=True)
    packet_type = Column(String(40), nullable=False, index=True)
    owners_json = Column(Text, nullable=True)
    capture_context = Column(String(40), nullable=False, index=True)
    strategy_version = Column(String(100), nullable=False, index=True)
    config_hash = Column(String(64), nullable=False, index=True)


class StrategyVersionRecord(Base):
    __tablename__ = "strategy_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=ist_now_naive, index=True)
    updated_at = Column(DateTime, nullable=False, default=ist_now_naive)
    last_seen_at = Column(DateTime, nullable=True, index=True)
    strategy_name = Column(String(100), nullable=False, index=True)
    version = Column(String(100), nullable=False, unique=True, index=True)
    status = Column(String(30), nullable=False, default="active", index=True)
    started_at = Column(DateTime, nullable=True, index=True)
    retired_at = Column(DateTime, nullable=True)
    human_note = Column(Text, nullable=True)
    reason_for_change = Column(Text, nullable=True)
    entry_logic_summary = Column(Text, nullable=True)
    exit_logic_summary = Column(Text, nullable=True)
    stoploss_logic_summary = Column(Text, nullable=True)
    target_logic_summary = Column(Text, nullable=True)
    config_snapshot_json = Column(Text, nullable=False)
    latest_config_snapshot_json = Column(Text, nullable=True)
    settings_purpose_json = Column(Text, nullable=True)
    config_hash = Column(String(64), nullable=False, index=True)
    latest_config_hash = Column(String(64), nullable=True, index=True)
    config_drift_detected = Column(Integer, nullable=False, default=0, index=True)


class RuntimeJobRunRecord(Base):
    __tablename__ = "runtime_job_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_name = Column(String(100), nullable=False, index=True)
    trading_date = Column(String(20), nullable=False, index=True)
    status = Column(String(30), nullable=False, default="pending", index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)


def init_db(database_url: Optional[str] = None) -> None:
    global engine, SessionLocal
    url = database_url or settings.database_url
    connect_args = {"connect_timeout": 5} if url.startswith("mysql") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    _ensure_candle_columns()
    _ensure_opportunity_columns()
    _ensure_trade_columns()
    _ensure_rejected_opportunity_columns()
    _ensure_strategy_validation_columns()
    _ensure_strategy_version_columns()
    _ensure_runtime_job_run_columns()
    _ensure_raw_tick_columns()
    _mark_legacy_rejected_outcomes_low_confidence()


def _ensure_candle_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "candles" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("candles")}
    required = {
        "instrument_token": "INTEGER",
        "receive_timestamp": "DATETIME",
        "timestamp_source": "VARCHAR(40)",
        "is_generated": "INTEGER DEFAULT 0",
        "data_quality": "VARCHAR(50)",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE candles ADD COLUMN {column} {column_type}"))


def _ensure_opportunity_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "opportunities" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("opportunities")}
    required = {
        "failure_tags_json": "TEXT",
        "heuristic_score_confidence": "FLOAT",
        "probability_source": "VARCHAR(50)",
        "calibration_version": "VARCHAR(100)",
        "strategy_version": "VARCHAR(100)",
        "config_hash": "VARCHAR(64)",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in columns:
                connection.execute(text(f"ALTER TABLE opportunities ADD COLUMN {column} {column_type}"))
        if engine.dialect.name.startswith("mysql"):
            connection.execute(text("ALTER TABLE opportunities MODIFY COLUMN probability FLOAT NULL"))


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
        "protective_order_id": "VARCHAR(100)",
        "protective_order_status": "VARCHAR(50)",
        "protective_trigger_price": "FLOAT",
        "protective_order_response_json": "TEXT",
        "protective_last_error": "TEXT",
        "protective_requested_at": "DATETIME",
        "protective_cancelled_at": "DATETIME",
        "price_source": "VARCHAR(50)",
        "price_timestamp": "DATETIME",
        "price_age_seconds": "FLOAT",
        "exit_rule_first_triggered": "VARCHAR(50)",
        "exit_triggered_rules_json": "TEXT",
        "exit_ltp": "FLOAT",
        "exit_best_bid": "FLOAT",
        "exit_best_ask": "FLOAT",
        "exit_executable_price": "FLOAT",
        "exit_depth_coverage": "FLOAT",
        "exit_spread_pct": "FLOAT",
        "exit_execution_source": "VARCHAR(60)",
        "exit_quote_timestamp": "DATETIME",
        "highest_price_during_trade": "FLOAT",
        "lowest_price_during_trade": "FLOAT",
        "mfe_points": "FLOAT",
        "mfe_percent": "FLOAT",
        "mae_points": "FLOAT",
        "mae_percent": "FLOAT",
        "time_to_mfe": "FLOAT",
        "time_to_mae": "FLOAT",
        "mfe_recorded_at": "DATETIME",
        "mae_recorded_at": "DATETIME",
        "strategy_version": "VARCHAR(100)",
        "config_hash": "VARCHAR(64)",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE trades ADD COLUMN {column} {column_type}"))


def _ensure_rejected_opportunity_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "rejected_opportunities" not in inspector.get_table_names():
        return
    columns = inspector.get_columns("rejected_opportunities")
    existing = {column["name"] for column in columns}
    required = {
        "rejection_source": "VARCHAR(50)",
        "rejection_context": "VARCHAR(50)",
        "market_session": "VARCHAR(30)",
        "learning_eligible": "INTEGER DEFAULT 0",
        "learning_exclusion_reason": "VARCHAR(150)",
        "later_outcome_at": "DATETIME",
        "later_outcome_minutes": "FLOAT",
        "later_outcome_source": "VARCHAR(50)",
        "later_outcome_timeframe": "VARCHAR(20)",
        "later_outcome_ambiguous": "INTEGER DEFAULT 0",
        "later_outcome_confidence": "VARCHAR(30)",
        "episode_id": "INTEGER",
        "episode_key": "VARCHAR(64)",
        "strategy_version": "VARCHAR(100)",
        "config_hash": "VARCHAR(64)",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE rejected_opportunities ADD COLUMN {column} {column_type}"))
        later_outcome_column = next((column for column in columns if column["name"] == "later_outcome"), None)
        later_outcome_type = str(later_outcome_column.get("type", "") if later_outcome_column else "").lower()
        if engine.dialect.name.startswith("mysql") and "80" not in later_outcome_type:
            connection.execute(text("ALTER TABLE rejected_opportunities MODIFY COLUMN later_outcome VARCHAR(80)"))


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
        "strategy_version": "VARCHAR(100)",
        "config_hash": "VARCHAR(64)",
        "fold_count": "INTEGER DEFAULT 0",
        "out_of_sample_sessions": "INTEGER DEFAULT 0",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE strategy_validations ADD COLUMN {column} {column_type}"))


def _ensure_strategy_version_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "strategy_versions" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("strategy_versions")}
    required = {
        "updated_at": "DATETIME",
        "last_seen_at": "DATETIME",
        "status": "VARCHAR(30)",
        "started_at": "DATETIME",
        "retired_at": "DATETIME",
        "human_note": "TEXT",
        "reason_for_change": "TEXT",
        "entry_logic_summary": "TEXT",
        "exit_logic_summary": "TEXT",
        "stoploss_logic_summary": "TEXT",
        "target_logic_summary": "TEXT",
        "latest_config_snapshot_json": "TEXT",
        "settings_purpose_json": "TEXT",
        "latest_config_hash": "VARCHAR(64)",
        "config_drift_detected": "INTEGER DEFAULT 0",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE strategy_versions ADD COLUMN {column} {column_type}"))


def _ensure_runtime_job_run_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "runtime_job_runs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("runtime_job_runs")}
    required = {
        "job_name": "VARCHAR(100)",
        "trading_date": "VARCHAR(20)",
        "status": "VARCHAR(30)",
        "started_at": "DATETIME",
        "completed_at": "DATETIME",
        "duration_ms": "INTEGER",
        "error_message": "TEXT",
        "metadata_json": "TEXT",
    }
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE runtime_job_runs ADD COLUMN {column} {column_type}"))


def _ensure_raw_tick_columns() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "raw_ticks" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("raw_ticks")}
    required = {"depth_json": "TEXT"}
    with engine.begin() as connection:
        for column, column_type in required.items():
            if column not in existing:
                connection.execute(text(f"ALTER TABLE raw_ticks ADD COLUMN {column} {column_type}"))


def _mark_legacy_rejected_outcomes_low_confidence() -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if "rejected_opportunities" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("rejected_opportunities")}
    required = {"later_outcome_source", "learning_eligible", "learning_exclusion_reason", "later_outcome_confidence"}
    if not required.issubset(columns):
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE rejected_opportunities "
                "SET learning_eligible = 0, "
                "learning_exclusion_reason = 'legacy_non_chronological_outcome', "
                "later_outcome_confidence = 'legacy_low' "
                "WHERE later_outcome IS NOT NULL "
                "AND (later_outcome_source IS NULL OR later_outcome_source IN ('current_quote', 'unknown'))"
            )
        )


def get_session():
    if SessionLocal is None:
        init_db()
    else:
        _ensure_candle_columns()
        _ensure_opportunity_columns()
        _ensure_trade_columns()
        _ensure_rejected_opportunity_columns()
        _ensure_strategy_validation_columns()
        _ensure_strategy_version_columns()
        _ensure_runtime_job_run_columns()
        _ensure_raw_tick_columns()
    return SessionLocal()
