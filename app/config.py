import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_AUTOMATION_SYMBOLS = "BANKNIFTY"


@dataclass(frozen=True)
class Settings:
    app_name: str = "AI Option Trader"
    app_environment: str = os.getenv("APP_ENV", "development")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    python_version: str = "3.12"
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./app.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    kite_api_key: Optional[str] = os.getenv("KITE_API_KEY")
    kite_api_secret: Optional[str] = os.getenv("KITE_API_SECRET")
    kite_access_token: Optional[str] = os.getenv("KITE_ACCESS_TOKEN")
    telegram_bot_token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = os.getenv("TELEGRAM_CHAT_ID")
    scanner_interval_seconds: int = int(os.getenv("SCANNER_INTERVAL_SECONDS", "60"))
    default_order_mode: str = os.getenv("ORDER_MODE", os.getenv("TRADING_MODE", "paper")).lower()
    paper_trading_mode: bool = os.getenv("PAPER_TRADING_MODE", "true").lower() == "true"
    live_trading_mode: bool = os.getenv("LIVE_TRADING_MODE", "false").lower() == "true"
    use_kite_market_data: bool = os.getenv("USE_KITE_MARKET_DATA", "true").lower() == "true"
    default_exchange: str = os.getenv("DEFAULT_EXCHANGE", "NSE")
    option_exchange: str = os.getenv("OPTION_EXCHANGE", "NFO")
    max_risk_per_trade_pct: float = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "1.0"))
    max_option_premium_pct: float = float(os.getenv("MAX_OPTION_PREMIUM_PCT", "80.0"))
    min_option_buy_premium: float = float(os.getenv("MIN_OPTION_BUY_PREMIUM", "5.0"))
    block_expiry_day_option_buying: bool = os.getenv("BLOCK_EXPIRY_DAY_OPTION_BUYING", "true").lower() == "true"
    min_directional_room_pct: float = float(os.getenv("MIN_DIRECTIONAL_ROOM_PCT", "0.4"))
    min_signal_score: int = int(os.getenv("MIN_SIGNAL_SCORE", "80"))
    min_option_liquidity_score: int = int(os.getenv("MIN_OPTION_LIQUIDITY_SCORE", "70"))
    account_equity: float = float(os.getenv("ACCOUNT_EQUITY", "100000"))
    default_product: str = os.getenv("KITE_DEFAULT_PRODUCT", "MIS")
    max_scan_symbols: int = int(os.getenv("MAX_SCAN_SYMBOLS", "40"))
    min_market_regime_score: int = int(os.getenv("MIN_MARKET_REGIME_SCORE", "55"))
    min_price_action_score: int = int(os.getenv("MIN_PRICE_ACTION_SCORE", "55"))
    min_option_chain_score: int = int(os.getenv("MIN_OPTION_CHAIN_SCORE", "55"))
    min_risk_reward: float = float(os.getenv("MIN_RISK_REWARD", "1.2"))
    max_trend_momentum_score: int = int(os.getenv("MAX_TREND_MOMENTUM_SCORE", "75"))
    max_bid_ask_spread_pct: float = float(os.getenv("MAX_BID_ASK_SPREAD_PCT", "5.0"))
    min_option_volume: int = int(os.getenv("MIN_OPTION_VOLUME", "500"))
    min_option_oi: int = int(os.getenv("MIN_OPTION_OI", "5000"))
    enforce_market_hours: bool = os.getenv("ENFORCE_MARKET_HOURS", "false").lower() == "true"
    market_open_time: str = os.getenv("MARKET_OPEN_TIME", "09:20")
    market_close_time: str = os.getenv("MARKET_CLOSE_TIME", "15:10")
    blocked_event_dates: str = os.getenv("BLOCKED_EVENT_DATES", "")
    blocked_symbols: str = os.getenv("BLOCKED_SYMBOLS", "")
    allow_option_selling: bool = os.getenv("ALLOW_OPTION_SELLING", "true").lower() == "true"
    max_vix_for_buying: float = float(os.getenv("MAX_VIX_FOR_BUYING", "22.0"))
    max_vix_for_selling: float = float(os.getenv("MAX_VIX_FOR_SELLING", "28.0"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "2.0"))
    max_trades_per_day: int = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
    max_stop_losses_per_day: int = int(os.getenv("MAX_STOP_LOSSES_PER_DAY", "2"))
    max_open_trades: int = int(os.getenv("MAX_OPEN_TRADES", "1"))
    max_symbol_open_trades: int = int(os.getenv("MAX_SYMBOL_OPEN_TRADES", "1"))
    max_open_premium_exposure_pct: float = float(os.getenv("MAX_OPEN_PREMIUM_EXPOSURE_PCT", "80.0"))
    cooldown_after_stop_minutes: int = int(os.getenv("COOLDOWN_AFTER_STOP_MINUTES", "30"))
    enable_auto_squareoff: bool = os.getenv("ENABLE_AUTO_SQUAREOFF", "true").lower() == "true"
    live_auto_squareoff: bool = os.getenv("LIVE_AUTO_SQUAREOFF", "false").lower() == "true"
    require_candle_confirmation: bool = os.getenv("REQUIRE_CANDLE_CONFIRMATION", "true").lower() == "true"
    candle_confirmation_timeframe: str = os.getenv("CANDLE_CONFIRMATION_TIMEFRAME", "5minute")
    min_option_buy_delta: float = float(os.getenv("MIN_OPTION_BUY_DELTA", "0.35"))
    max_option_buy_delta: float = float(os.getenv("MAX_OPTION_BUY_DELTA", "0.75"))
    max_option_buy_theta_pct: float = float(os.getenv("MAX_OPTION_BUY_THETA_PCT", "12.0"))
    min_option_buy_iv: float = float(os.getenv("MIN_OPTION_BUY_IV", "0.05"))
    max_option_buy_iv: float = float(os.getenv("MAX_OPTION_BUY_IV", "1.20"))
    min_option_quality_score: int = int(os.getenv("MIN_OPTION_QUALITY_SCORE", "60"))
    backtest_horizon_candles: int = int(os.getenv("BACKTEST_HORIZON_CANDLES", "12"))
    backtest_option_stop_loss_pct: float = float(os.getenv("BACKTEST_OPTION_STOP_LOSS_PCT", "22.0"))
    backtest_option_target_pct: float = float(os.getenv("BACKTEST_OPTION_TARGET_PCT", "35.0"))
    backtest_slippage_pct: float = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "1.0"))
    backtest_charges_pct: float = float(os.getenv("BACKTEST_CHARGES_PCT", "0.20"))
    backtest_walk_forward_train_pct: float = float(os.getenv("BACKTEST_WALK_FORWARD_TRAIN_PCT", "70.0"))
    enable_strategy_edge_guard: bool = os.getenv("ENABLE_STRATEGY_EDGE_GUARD", "false").lower() == "true"
    min_strategy_trades: int = int(os.getenv("MIN_STRATEGY_TRADES", "30"))
    min_strategy_expectancy_pct: float = float(os.getenv("MIN_STRATEGY_EXPECTANCY_PCT", "0.05"))
    min_strategy_profit_factor: float = float(os.getenv("MIN_STRATEGY_PROFIT_FACTOR", "1.15"))
    min_strategy_win_rate_pct: float = float(os.getenv("MIN_STRATEGY_WIN_RATE_PCT", "35.0"))
    strategy_edge_cache_seconds: int = int(os.getenv("STRATEGY_EDGE_CACHE_SECONDS", "1800"))
    enable_day_type_filter: bool = os.getenv("ENABLE_DAY_TYPE_FILTER", "true").lower() == "true"
    min_day_type_score: int = int(os.getenv("MIN_DAY_TYPE_SCORE", "55"))
    opening_range_minutes: int = int(os.getenv("OPENING_RANGE_MINUTES", "30"))
    enable_option_premium_confirmation: bool = os.getenv("ENABLE_OPTION_PREMIUM_CONFIRMATION", "true").lower() == "true"
    min_option_premium_confirmation_score: int = int(os.getenv("MIN_OPTION_PREMIUM_CONFIRMATION_SCORE", "55"))
    option_premium_lookback_candles: int = int(os.getenv("OPTION_PREMIUM_LOOKBACK_CANDLES", "6"))
    option_time_stop_minutes: int = int(os.getenv("OPTION_TIME_STOP_MINUTES", "15"))
    option_time_stop_min_move_pct: float = float(os.getenv("OPTION_TIME_STOP_MIN_MOVE_PCT", "6.0"))
    exit_open_trades_before_close_minutes: int = int(os.getenv("EXIT_OPEN_TRADES_BEFORE_CLOSE_MINUTES", "10"))
    enable_time_bucket_filter: bool = os.getenv("ENABLE_TIME_BUCKET_FILTER", "false").lower() == "true"
    min_time_bucket_trades: int = int(os.getenv("MIN_TIME_BUCKET_TRADES", "8"))
    min_time_bucket_expectancy_pct: float = float(os.getenv("MIN_TIME_BUCKET_EXPECTANCY_PCT", "0.05"))
    enable_outcome_learning_guard: bool = os.getenv("ENABLE_OUTCOME_LEARNING_GUARD", "true").lower() == "true"
    min_outcome_learning_trades: int = int(os.getenv("MIN_OUTCOME_LEARNING_TRADES", "8"))
    min_outcome_learning_expectancy_pct: float = float(os.getenv("MIN_OUTCOME_LEARNING_EXPECTANCY_PCT", "0.0"))
    min_outcome_learning_win_rate_pct: float = float(os.getenv("MIN_OUTCOME_LEARNING_WIN_RATE_PCT", "35.0"))
    outcome_learning_lookback: int = int(os.getenv("OUTCOME_LEARNING_LOOKBACK", "500"))
    enforce_execution_quality: bool = os.getenv("ENFORCE_EXECUTION_QUALITY", "true").lower() == "true"
    max_execution_spread_pct: float = float(os.getenv("MAX_EXECUTION_SPREAD_PCT", "3.0"))
    max_entry_price_deviation_pct: float = float(os.getenv("MAX_ENTRY_PRICE_DEVIATION_PCT", "8.0"))
    min_execution_quote_price: float = float(os.getenv("MIN_EXECUTION_QUOTE_PRICE", "0.05"))
    automation_enabled: bool = os.getenv("AUTOMATION_ENABLED", "false").lower() == "true"
    automation_symbols: str = os.getenv("AUTOMATION_SYMBOLS", DEFAULT_AUTOMATION_SYMBOLS)
    automation_side: str = os.getenv("AUTOMATION_SIDE", "BUY")
    automation_scan_interval_seconds: int = int(os.getenv("AUTOMATION_SCAN_INTERVAL_SECONDS", "30"))
    automation_snapshot_interval_seconds: int = int(os.getenv("AUTOMATION_SNAPSHOT_INTERVAL_SECONDS", "300"))
    automation_outcome_interval_seconds: int = int(os.getenv("AUTOMATION_OUTCOME_INTERVAL_SECONDS", "30"))
    automation_ingest_days: int = int(os.getenv("AUTOMATION_INGEST_DAYS", "365"))
    automation_checkpoint_overlap_minutes: int = int(os.getenv("AUTOMATION_CHECKPOINT_OVERLAP_MINUTES", "30"))
    automation_intraday_candle_sync: bool = os.getenv("AUTOMATION_INTRADAY_CANDLE_SYNC", "true").lower() == "true"
    automation_intraday_candle_sync_minutes: int = int(os.getenv("AUTOMATION_INTRADAY_CANDLE_SYNC_MINUTES", "5"))
    automation_place_orders: bool = os.getenv("AUTOMATION_PLACE_ORDERS", "true").lower() == "true"
    automation_confirm_live: bool = os.getenv("AUTOMATION_CONFIRM_LIVE", "false").lower() == "true"
    automation_scan_limit: int = int(os.getenv("AUTOMATION_SCAN_LIMIT", "3"))
    automation_strike_window_pct: float = float(os.getenv("AUTOMATION_STRIKE_WINDOW_PCT", "4.0"))
    automation_max_contracts_per_symbol: int = int(os.getenv("AUTOMATION_MAX_CONTRACTS_PER_SYMBOL", "120"))


settings = Settings()
