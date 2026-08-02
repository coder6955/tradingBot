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
    strategy_name: str = os.getenv("STRATEGY_NAME", "banknifty_option_buying")
    strategy_version: str = os.getenv("STRATEGY_VERSION", "banknifty_option_buying_v6")
    strategy_version_note: str = os.getenv("STRATEGY_VERSION_NOTE", "Price-structure opening and continuation entries with unified opportunity checks and runner exits")
    strategy_change_reason: str = os.getenv("STRATEGY_CHANGE_REASON", "Improve Bank Nifty intraday option-buying timing while keeping execution and risk gates conservative")
    active_decision_timeframes: str = os.getenv("ACTIVE_DECISION_TIMEFRAMES", "1minute,5minute")
    structure_min_completed_candles: int = int(os.getenv("STRUCTURE_MIN_COMPLETED_CANDLES", "6"))
    opening_structure_min_1m_candles: int = int(os.getenv("OPENING_STRUCTURE_MIN_1M_CANDLES", "5"))
    opening_structure_min_5m_candles: int = int(os.getenv("OPENING_STRUCTURE_MIN_5M_CANDLES", "3"))
    opening_structure_end_time: str = os.getenv("OPENING_STRUCTURE_END_TIME", "09:45")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    python_version: str = "3.12"
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./app.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    api_auth_token: Optional[str] = os.getenv("API_AUTH_TOKEN") or os.getenv("APP_API_KEY")
    api_auth_required: bool = os.getenv("API_AUTH_REQUIRED", "false").lower() == "true"
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
    risk_policy_version: str = os.getenv("RISK_POLICY_VERSION", "banknifty_risk_v1")
    active_entry_policy: str = os.getenv("ACTIVE_ENTRY_POLICY", "banknifty_option_buying_v6")
    shadow_entry_policy: str = os.getenv("SHADOW_ENTRY_POLICY", "banknifty_option_buying_v6_shadow")
    active_risk_policy: str = os.getenv("ACTIVE_RISK_POLICY", "base_only_v1")
    shadow_risk_policy: str = os.getenv("SHADOW_RISK_POLICY", "tier_spectrum_research_v1")
    risk_tier_1_base_pct: float = float(os.getenv("RISK_TIER_1_BASE_PERCENT", "1.0"))
    risk_tier_2_strong_pct: float = float(os.getenv("RISK_TIER_2_STRONG_PERCENT", "2.0"))
    risk_tier_3_high_pct: float = float(os.getenv("RISK_TIER_3_HIGH_PERCENT", "3.0"))
    risk_tier_4_exceptional_pct: float = float(os.getenv("RISK_TIER_4_EXCEPTIONAL_PERCENT", "5.0"))
    absolute_max_risk_per_trade_percent: float = float(os.getenv("ABSOLUTE_MAX_RISK_PER_TRADE_PERCENT", "5.0"))
    max_risk_per_trade_percent: float = float(os.getenv("MAX_RISK_PER_TRADE_PERCENT", "5.0"))
    active_paper_max_risk_tier: str = os.getenv("ACTIVE_PAPER_MAX_RISK_TIER", "TIER_1_BASE")
    active_live_max_risk_tier: str = os.getenv("ACTIVE_LIVE_MAX_RISK_TIER", "TIER_1_BASE")
    shadow_max_risk_tier: str = os.getenv("SHADOW_MAX_RISK_TIER", "TIER_4_EXCEPTIONAL")
    enable_validated_higher_risk_active: bool = os.getenv("ENABLE_VALIDATED_HIGHER_RISK_ACTIVE", "false").lower() == "true"
    enable_exceptional_live_risk: bool = os.getenv("ENABLE_EXCEPTIONAL_LIVE_RISK", "false").lower() == "true"
    max_daily_planned_risk_percent: float = float(os.getenv("MAX_DAILY_PLANNED_RISK_PERCENT", "2.0"))
    max_realized_daily_loss_percent: float = float(os.getenv("MAX_REALIZED_DAILY_LOSS_PERCENT", os.getenv("MAX_DAILY_LOSS_PCT", "2.0")))
    max_total_open_risk_percent: float = float(os.getenv("MAX_TOTAL_OPEN_RISK_PERCENT", "2.0"))
    max_banknifty_open_risk_percent: float = float(os.getenv("MAX_BANKNIFTY_OPEN_RISK_PERCENT", "2.0"))
    max_consecutive_losses: int = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2"))
    risk_reduction_after_drawdown_percent: float = float(os.getenv("RISK_REDUCTION_AFTER_DRAWDOWN_PERCENT", "5.0"))
    risk_expected_entry_slippage_pct: float = float(os.getenv("RISK_EXPECTED_ENTRY_SLIPPAGE_PERCENT", "0.15"))
    risk_expected_exit_slippage_pct: float = float(os.getenv("RISK_EXPECTED_EXIT_SLIPPAGE_PERCENT", "0.25"))
    risk_allocated_entry_cost_pct: float = float(os.getenv("RISK_ALLOCATED_ENTRY_COST_PERCENT", "0.05"))
    risk_allocated_exit_cost_pct: float = float(os.getenv("RISK_ALLOCATED_EXIT_COST_PERCENT", "0.05"))
    risk_tier_2_min_oos_trades: int = int(os.getenv("RISK_TIER_2_MIN_OOS_TRADES", "100"))
    risk_tier_3_min_oos_trades: int = int(os.getenv("RISK_TIER_3_MIN_OOS_TRADES", "200"))
    risk_tier_4_min_oos_trades: int = int(os.getenv("RISK_TIER_4_MIN_OOS_TRADES", "300"))
    risk_tier_2_min_sessions: int = int(os.getenv("RISK_TIER_2_MIN_SESSIONS", "20"))
    risk_tier_3_min_sessions: int = int(os.getenv("RISK_TIER_3_MIN_SESSIONS", "30"))
    risk_tier_4_min_sessions: int = int(os.getenv("RISK_TIER_4_MIN_SESSIONS", "40"))
    risk_tier_2_min_folds: int = int(os.getenv("RISK_TIER_2_MIN_FOLDS", "3"))
    risk_tier_3_min_folds: int = int(os.getenv("RISK_TIER_3_MIN_FOLDS", "4"))
    risk_tier_4_min_folds: int = int(os.getenv("RISK_TIER_4_MIN_FOLDS", "5"))
    risk_tier_2_min_profit_factor: float = float(os.getenv("RISK_TIER_2_MIN_PROFIT_FACTOR", "1.15"))
    risk_tier_3_min_profit_factor: float = float(os.getenv("RISK_TIER_3_MIN_PROFIT_FACTOR", "1.25"))
    risk_tier_4_min_profit_factor: float = float(os.getenv("RISK_TIER_4_MIN_PROFIT_FACTOR", "1.40"))
    risk_tier_2_max_drawdown_pct: float = float(os.getenv("RISK_TIER_2_MAX_DRAWDOWN_PERCENT", "15.0"))
    risk_tier_3_max_drawdown_pct: float = float(os.getenv("RISK_TIER_3_MAX_DRAWDOWN_PERCENT", "12.0"))
    risk_tier_4_max_drawdown_pct: float = float(os.getenv("RISK_TIER_4_MAX_DRAWDOWN_PERCENT", "10.0"))
    setup_episode_reservation_seconds: int = int(os.getenv("SETUP_EPISODE_RESERVATION_SECONDS", "120"))
    evidence_queue_max_size: int = int(os.getenv("EVIDENCE_QUEUE_MAX_SIZE", "2048"))
    evidence_queue_max_retries: int = int(os.getenv("EVIDENCE_QUEUE_MAX_RETRIES", "3"))
    outcome_queue_max_size: int = int(os.getenv("OUTCOME_QUEUE_MAX_SIZE", "8192"))
    outcome_queue_max_retries: int = int(os.getenv("OUTCOME_QUEUE_MAX_RETRIES", "3"))
    outcome_horizons_seconds: str = os.getenv("OUTCOME_HORIZONS_SECONDS", "30,60,180,300,900")
    outcome_session_cutoff_time: str = os.getenv("OUTCOME_SESSION_CUTOFF_TIME", "15:25")
    outcome_min_executable_coverage_pct: float = float(os.getenv("OUTCOME_MIN_EXECUTABLE_COVERAGE_PERCENT", "80.0"))
    outcome_missing_interval_seconds: float = float(os.getenv("OUTCOME_MISSING_INTERVAL_SECONDS", "5.0"))
    max_option_premium_pct: float = float(os.getenv("MAX_OPTION_PREMIUM_PCT", "80.0"))
    min_option_buy_premium: float = float(os.getenv("MIN_OPTION_BUY_PREMIUM", "5.0"))
    block_expiry_day_option_buying: bool = os.getenv("BLOCK_EXPIRY_DAY_OPTION_BUYING", "true").lower() == "true"
    min_directional_room_pct: float = float(os.getenv("MIN_DIRECTIONAL_ROOM_PCT", "0.4"))
    enable_directional_room_hard_gate: bool = os.getenv("ENABLE_DIRECTIONAL_ROOM_HARD_GATE", "false").lower() == "true"
    min_signal_score: int = int(os.getenv("MIN_SIGNAL_SCORE", "80"))
    min_option_liquidity_score: int = int(os.getenv("MIN_OPTION_LIQUIDITY_SCORE", "70"))
    account_equity: float = float(os.getenv("ACCOUNT_EQUITY", "100000"))
    default_product: str = os.getenv("KITE_DEFAULT_PRODUCT", "MIS")
    max_scan_symbols: int = int(os.getenv("MAX_SCAN_SYMBOLS", "40"))
    min_market_regime_score: int = int(os.getenv("MIN_MARKET_REGIME_SCORE", "55"))
    enable_hierarchical_market_state: bool = os.getenv("ENABLE_HIERARCHICAL_MARKET_STATE", "true").lower() == "true"
    market_state_min_confidence: float = float(os.getenv("MARKET_STATE_MIN_CONFIDENCE", "0.55"))
    market_state_max_uncertainty: float = float(os.getenv("MARKET_STATE_MAX_UNCERTAINTY", "0.45"))
    mtf_min_timeframes: int = int(os.getenv("MTF_MIN_TIMEFRAMES", "2"))
    mtf_min_alignment_score: int = int(os.getenv("MTF_MIN_ALIGNMENT_SCORE", "55"))
    momentum_min_entry_score: int = int(os.getenv("MOMENTUM_MIN_ENTRY_SCORE", "55"))
    momentum_exhaustion_rsi: float = float(os.getenv("MOMENTUM_EXHAUSTION_RSI", "78.0"))
    setup_policy_min_score: int = int(os.getenv("SETUP_POLICY_MIN_SCORE", "55"))
    candidate_min_utility_score: float = float(os.getenv("CANDIDATE_MIN_UTILITY_SCORE", "50.0"))
    candidate_round_trip_cost_pct: float = float(os.getenv("CANDIDATE_ROUND_TRIP_COST_PCT", "0.45"))
    contract_min_depth_quantity: int = int(os.getenv("CONTRACT_MIN_DEPTH_QUANTITY", "15"))
    contract_max_ranked_candidates: int = int(os.getenv("CONTRACT_MAX_RANKED_CANDIDATES", "5"))
    armed_entry_recovery_enabled: bool = os.getenv("ARMED_ENTRY_RECOVERY_ENABLED", "true").lower() == "true"
    evidence_matrix_min_trades: int = int(os.getenv("EVIDENCE_MATRIX_MIN_TRADES", "30"))
    evidence_matrix_min_expectancy_pct: float = float(os.getenv("EVIDENCE_MATRIX_MIN_EXPECTANCY_PCT", "0.05"))
    evidence_matrix_min_profit_factor: float = float(os.getenv("EVIDENCE_MATRIX_MIN_PROFIT_FACTOR", "1.15"))
    evidence_matrix_max_drawdown_pct: float = float(os.getenv("EVIDENCE_MATRIX_MAX_DRAWDOWN_PCT", "15.0"))
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
    runtime_pre_market_start_time: str = os.getenv("RUNTIME_PRE_MARKET_START_TIME", "09:00")
    runtime_market_open_time: str = os.getenv("RUNTIME_MARKET_OPEN_TIME", "09:15")
    runtime_market_closing_start_time: str = os.getenv("RUNTIME_MARKET_CLOSING_START_TIME", "15:20")
    runtime_market_close_time: str = os.getenv("RUNTIME_MARKET_CLOSE_TIME", "15:30")
    runtime_after_market_review_start_time: str = os.getenv("RUNTIME_AFTER_MARKET_REVIEW_START_TIME", "15:35")
    runtime_holidays: str = os.getenv("RUNTIME_HOLIDAYS", os.getenv("MARKET_HOLIDAYS", ""))
    runtime_manual_override: bool = os.getenv("RUNTIME_MANUAL_OVERRIDE", "false").lower() == "true"
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
    target_option_buy_delta: float = float(os.getenv("TARGET_OPTION_BUY_DELTA", "0.52"))
    preferred_option_buy_min_dte: int = int(os.getenv("PREFERRED_OPTION_BUY_MIN_DTE", "1"))
    preferred_option_buy_max_dte: int = int(os.getenv("PREFERRED_OPTION_BUY_MAX_DTE", "7"))
    max_option_buy_theta_pct: float = float(os.getenv("MAX_OPTION_BUY_THETA_PCT", "12.0"))
    min_option_buy_iv: float = float(os.getenv("MIN_OPTION_BUY_IV", "0.05"))
    max_option_buy_iv: float = float(os.getenv("MAX_OPTION_BUY_IV", "1.20"))
    min_option_quality_score: int = int(os.getenv("MIN_OPTION_QUALITY_SCORE", "60"))
    enable_volatility_edge: bool = os.getenv("ENABLE_VOLATILITY_EDGE", "true").lower() == "true"
    enable_volatility_edge_hard_gate: bool = os.getenv("ENABLE_VOLATILITY_EDGE_HARD_GATE", "false").lower() == "true"
    min_volatility_edge_score: int = int(os.getenv("MIN_VOLATILITY_EDGE_SCORE", "55"))
    vol_edge_iv_lookback_days: int = int(os.getenv("VOL_EDGE_IV_LOOKBACK_DAYS", "20"))
    vol_edge_min_iv_samples: int = int(os.getenv("VOL_EDGE_MIN_IV_SAMPLES", "30"))
    vol_edge_max_iv_to_rv_ratio_for_buy: float = float(os.getenv("VOL_EDGE_MAX_IV_TO_RV_RATIO_FOR_BUY", "1.5"))
    vol_edge_min_expected_move_coverage: float = float(os.getenv("VOL_EDGE_MIN_EXPECTED_MOVE_COVERAGE", "0.8"))
    vol_edge_iv_crush_warning_threshold: float = float(os.getenv("VOL_EDGE_IV_CRUSH_WARNING_THRESHOLD", "80"))
    backtest_horizon_candles: int = int(os.getenv("BACKTEST_HORIZON_CANDLES", "12"))
    backtest_option_stop_loss_pct: float = float(os.getenv("BACKTEST_OPTION_STOP_LOSS_PCT", "22.0"))
    backtest_option_target_pct: float = float(os.getenv("BACKTEST_OPTION_TARGET_PCT", "35.0"))
    backtest_slippage_pct: float = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "1.0"))
    backtest_charges_pct: float = float(os.getenv("BACKTEST_CHARGES_PCT", "0.20"))
    backtest_walk_forward_train_pct: float = float(os.getenv("BACKTEST_WALK_FORWARD_TRAIN_PCT", "70.0"))
    walk_forward_folds: int = int(os.getenv("WALK_FORWARD_FOLDS", "5"))
    walk_forward_embargo_candles: int = int(os.getenv("WALK_FORWARD_EMBARGO_CANDLES", "12"))
    walk_forward_min_train_candles: int = int(os.getenv("WALK_FORWARD_MIN_TRAIN_CANDLES", "120"))
    walk_forward_min_validation_candles: int = int(os.getenv("WALK_FORWARD_MIN_VALIDATION_CANDLES", "60"))
    readiness_min_oos_trades: int = int(os.getenv("READINESS_MIN_OOS_TRADES", "100"))
    readiness_min_oos_sessions: int = int(os.getenv("READINESS_MIN_OOS_SESSIONS", "20"))
    readiness_min_validation_folds: int = int(os.getenv("READINESS_MIN_VALIDATION_FOLDS", "3"))
    readiness_max_drawdown_pct: float = float(os.getenv("READINESS_MAX_DRAWDOWN_PCT", "15.0"))
    readiness_min_regime_trades: int = int(os.getenv("READINESS_MIN_REGIME_TRADES", "10"))
    readiness_volatile_day_range_pct: float = float(os.getenv("READINESS_VOLATILE_DAY_RANGE_PCT", "1.0"))
    probability_calibration_min_samples: int = int(os.getenv("PROBABILITY_CALIBRATION_MIN_SAMPLES", "200"))
    enable_execution_realism: bool = os.getenv("ENABLE_EXECUTION_REALISM", "true").lower() == "true"
    realism_entry_buy_slippage_pct: float = float(os.getenv("REALISM_ENTRY_BUY_SLIPPAGE_PCT", "0.30"))
    realism_exit_target_slippage_pct: float = float(os.getenv("REALISM_EXIT_TARGET_SLIPPAGE_PCT", "0.35"))
    realism_exit_stop_overshoot_pct: float = float(os.getenv("REALISM_EXIT_STOP_OVERSHOOT_PCT", "0.80"))
    realism_no_fill_touch_buffer_pct: float = float(os.getenv("REALISM_NO_FILL_TOUCH_BUFFER_PCT", "0.20"))
    realism_first_15_min_extra_slippage_pct: float = float(os.getenv("REALISM_FIRST_15_MIN_EXTRA_SLIPPAGE_PCT", "0.50"))
    realism_expiry_day_extra_slippage_pct: float = float(os.getenv("REALISM_EXPIRY_DAY_EXTRA_SLIPPAGE_PCT", "0.50"))
    realism_high_iv_extra_slippage_pct: float = float(os.getenv("REALISM_HIGH_IV_EXTRA_SLIPPAGE_PCT", "0.30"))
    realism_wide_spread_extra_slippage_pct: float = float(os.getenv("REALISM_WIDE_SPREAD_EXTRA_SLIPPAGE_PCT", "0.30"))
    enable_after_market_research_job: bool = os.getenv("ENABLE_AFTER_MARKET_RESEARCH_JOB", "true").lower() == "true"
    after_market_research_time: str = os.getenv("AFTER_MARKET_RESEARCH_TIME", "15:45")
    after_market_research_symbol: str = os.getenv("AFTER_MARKET_RESEARCH_SYMBOL", "BANKNIFTY")
    after_market_research_timeframe: str = os.getenv("AFTER_MARKET_RESEARCH_TIMEFRAME", "5minute")
    after_market_research_direction: str = os.getenv("AFTER_MARKET_RESEARCH_DIRECTION", "BOTH")
    after_market_research_horizon_candles: int = int(os.getenv("AFTER_MARKET_RESEARCH_HORIZON_CANDLES", "12"))
    after_market_research_limit: int = int(os.getenv("AFTER_MARKET_RESEARCH_LIMIT", "3000"))
    after_market_research_decision_mode: str = os.getenv("AFTER_MARKET_RESEARCH_DECISION_MODE", "scanner_parity")
    after_market_research_step_delay_seconds: float = float(os.getenv("AFTER_MARKET_RESEARCH_STEP_DELAY_SECONDS", "1.0"))
    automation_stop_after_after_market_complete: bool = os.getenv("AUTOMATION_STOP_AFTER_AFTER_MARKET_COMPLETE", "false").lower() == "true"
    enable_strategy_edge_guard: bool = os.getenv("ENABLE_STRATEGY_EDGE_GUARD", "false").lower() == "true"
    min_strategy_trades: int = int(os.getenv("MIN_STRATEGY_TRADES", "30"))
    min_strategy_expectancy_pct: float = float(os.getenv("MIN_STRATEGY_EXPECTANCY_PCT", "0.05"))
    min_strategy_profit_factor: float = float(os.getenv("MIN_STRATEGY_PROFIT_FACTOR", "1.15"))
    min_strategy_win_rate_pct: float = float(os.getenv("MIN_STRATEGY_WIN_RATE_PCT", "35.0"))
    strategy_edge_cache_seconds: int = int(os.getenv("STRATEGY_EDGE_CACHE_SECONDS", "1800"))
    time_bucket_cache_ttl_seconds: int = int(os.getenv("TIME_BUCKET_CACHE_TTL_SECONDS", "86400"))
    volatility_history_cache_ttl_seconds: int = int(os.getenv("VOLATILITY_HISTORY_CACHE_TTL_SECONDS", "60"))
    enable_day_type_filter: bool = os.getenv("ENABLE_DAY_TYPE_FILTER", "true").lower() == "true"
    min_day_type_score: int = int(os.getenv("MIN_DAY_TYPE_SCORE", "55"))
    opening_range_minutes: int = int(os.getenv("OPENING_RANGE_MINUTES", "30"))
    enable_option_premium_confirmation: bool = os.getenv("ENABLE_OPTION_PREMIUM_CONFIRMATION", "true").lower() == "true"
    min_option_premium_confirmation_score: int = int(os.getenv("MIN_OPTION_PREMIUM_CONFIRMATION_SCORE", "55"))
    option_premium_lookback_candles: int = int(os.getenv("OPTION_PREMIUM_LOOKBACK_CANDLES", "6"))
    enable_banknifty_intelligence: bool = os.getenv("ENABLE_BANKNIFTY_INTELLIGENCE", "true").lower() == "true"
    banknifty_top_bank_min_alignment: float = float(os.getenv("BANKNIFTY_TOP_BANK_MIN_ALIGNMENT", "0.55"))
    banknifty_top_bank_min_direction_count: int = int(os.getenv("BANKNIFTY_TOP_BANK_MIN_DIRECTION_COUNT", "3"))
    banknifty_constituent_snapshot_file: str = os.getenv(
        "BANKNIFTY_CONSTITUENT_SNAPSHOT_FILE",
        str(BASE_DIR / "app" / "data" / "banknifty_constituents_2026-06-30.json"),
    )
    banknifty_constituent_max_age_days: int = int(os.getenv("BANKNIFTY_CONSTITUENT_MAX_AGE_DAYS", "45"))
    banknifty_constituent_min_weight_coverage: float = float(os.getenv("BANKNIFTY_CONSTITUENT_MIN_WEIGHT_COVERAGE", "0.70"))
    banknifty_constituent_hard_gate_weight_cap: float = float(os.getenv("BANKNIFTY_CONSTITUENT_HARD_GATE_WEIGHT_CAP", "0.20"))
    banknifty_opposing_heavyweight_weight: float = float(os.getenv("BANKNIFTY_OPPOSING_HEAVYWEIGHT_WEIGHT", "0.30"))
    banknifty_extreme_divergence_pct: float = float(os.getenv("BANKNIFTY_EXTREME_DIVERGENCE_PCT", "0.35"))
    banknifty_opening_range_start: str = os.getenv("BANKNIFTY_OPENING_RANGE_START", "09:15")
    banknifty_opening_range_end: str = os.getenv("BANKNIFTY_OPENING_RANGE_END", "09:30")
    banknifty_no_trade_start_time: str = os.getenv("BANKNIFTY_NO_TRADE_START_TIME", "09:15")
    banknifty_first_trade_time: str = os.getenv("BANKNIFTY_FIRST_TRADE_TIME", "09:30")
    banknifty_major_zone_points: int = int(os.getenv("BANKNIFTY_MAJOR_ZONE_POINTS", "500"))
    banknifty_very_major_zone_points: int = int(os.getenv("BANKNIFTY_VERY_MAJOR_ZONE_POINTS", "1000"))
    banknifty_zone_risk_points: float = float(os.getenv("BANKNIFTY_ZONE_RISK_POINTS", "80"))
    banknifty_expected_move_min_coverage: float = float(os.getenv("BANKNIFTY_EXPECTED_MOVE_MIN_COVERAGE", "1.05"))
    banknifty_event_dates: str = os.getenv("BANKNIFTY_EVENT_DATES", "")
    banknifty_event_preferred_after_time: str = os.getenv("BANKNIFTY_EVENT_PREFERRED_AFTER_TIME", "10:00")
    enable_banknifty_regime_filter: bool = os.getenv("ENABLE_BANKNIFTY_REGIME_FILTER", "true").lower() == "true"
    min_banknifty_regime_score: int = int(os.getenv("MIN_BANKNIFTY_REGIME_SCORE", "65"))
    banknifty_significant_gap_pct: float = float(os.getenv("BANKNIFTY_SIGNIFICANT_GAP_PCT", "0.35"))
    banknifty_compression_day_range_pct: float = float(os.getenv("BANKNIFTY_COMPRESSION_DAY_RANGE_PCT", "0.45"))
    banknifty_late_trade_cutoff_time: str = os.getenv("BANKNIFTY_LATE_TRADE_CUTOFF_TIME", "14:45")
    banknifty_late_trade_min_premium_score: int = int(os.getenv("BANKNIFTY_LATE_TRADE_MIN_PREMIUM_SCORE", "80"))
    banknifty_expiry_min_premium_score: int = int(os.getenv("BANKNIFTY_EXPIRY_MIN_PREMIUM_SCORE", "75"))
    option_time_stop_minutes: int = int(os.getenv("OPTION_TIME_STOP_MINUTES", "15"))
    option_time_stop_min_move_pct: float = float(os.getenv("OPTION_TIME_STOP_MIN_MOVE_PCT", "6.0"))
    option_time_stop_trend_multiplier: float = float(os.getenv("OPTION_TIME_STOP_TREND_MULTIPLIER", "2.0"))
    option_trailing_stop_lock_pct: float = float(os.getenv("OPTION_TRAILING_STOP_LOCK_PCT", "2.0"))
    option_runner_atr_multiplier: float = float(os.getenv("OPTION_RUNNER_ATR_MULTIPLIER", "1.8"))
    option_runner_min_risk_trail: float = float(os.getenv("OPTION_RUNNER_MIN_RISK_TRAIL", "0.55"))
    exit_open_trades_before_close_minutes: int = int(os.getenv("EXIT_OPEN_TRADES_BEFORE_CLOSE_MINUTES", "10"))
    enable_time_bucket_filter: bool = os.getenv("ENABLE_TIME_BUCKET_FILTER", "false").lower() == "true"
    min_time_bucket_trades: int = int(os.getenv("MIN_TIME_BUCKET_TRADES", "8"))
    min_time_bucket_expectancy_pct: float = float(os.getenv("MIN_TIME_BUCKET_EXPECTANCY_PCT", "0.05"))
    enable_outcome_learning_guard: bool = os.getenv("ENABLE_OUTCOME_LEARNING_GUARD", "true").lower() == "true"
    min_outcome_learning_trades: int = int(os.getenv("MIN_OUTCOME_LEARNING_TRADES", "8"))
    min_outcome_learning_expectancy_pct: float = float(os.getenv("MIN_OUTCOME_LEARNING_EXPECTANCY_PCT", "0.0"))
    min_outcome_learning_win_rate_pct: float = float(os.getenv("MIN_OUTCOME_LEARNING_WIN_RATE_PCT", "35.0"))
    outcome_learning_lookback: int = int(os.getenv("OUTCOME_LEARNING_LOOKBACK", "500"))
    outcome_learning_cache_ttl_seconds: int = int(os.getenv("OUTCOME_LEARNING_CACHE_TTL_SECONDS", "900"))
    enable_rejected_outcome_candle_replay: bool = os.getenv("ENABLE_REJECTED_OUTCOME_CANDLE_REPLAY", "true").lower() == "true"
    rejected_outcome_replay_timeframes: str = os.getenv("REJECTED_OUTCOME_REPLAY_TIMEFRAMES", "1minute,5minute")
    rejected_outcome_replay_max_candles: int = int(os.getenv("REJECTED_OUTCOME_REPLAY_MAX_CANDLES", "500"))
    rejected_outcome_use_ws_token_candles: bool = os.getenv("REJECTED_OUTCOME_USE_WS_TOKEN_CANDLES", "true").lower() == "true"
    rejected_outcome_ambiguous_candle_policy: str = os.getenv("REJECTED_OUTCOME_AMBIGUOUS_CANDLE_POLICY", "label_ambiguous")
    rejected_outcome_batch_limit: int = int(os.getenv("REJECTED_OUTCOME_BATCH_LIMIT", "100"))
    rejected_outcome_max_batches: int = int(os.getenv("REJECTED_OUTCOME_MAX_BATCHES", "20"))
    rejected_outcome_batch_delay_seconds: float = float(os.getenv("REJECTED_OUTCOME_BATCH_DELAY_SECONDS", "0.5"))
    rejected_outcome_horizon_minutes: int = int(os.getenv("REJECTED_OUTCOME_HORIZON_MINUTES", "90"))
    setup_episode_window_seconds: int = int(os.getenv("SETUP_EPISODE_WINDOW_SECONDS", "60"))
    automation_exhaust_rejected_outcomes_after_close: bool = os.getenv("AUTOMATION_EXHAUST_REJECTED_OUTCOMES_AFTER_CLOSE", "true").lower() == "true"
    enable_targeted_option_candle_backfill: bool = os.getenv("ENABLE_TARGETED_OPTION_CANDLE_BACKFILL", "true").lower() == "true"
    targeted_option_candle_backfill_timeframes: str = os.getenv("TARGETED_OPTION_CANDLE_BACKFILL_TIMEFRAMES", "1minute,5minute")
    targeted_option_candle_backfill_batch_limit: int = int(os.getenv("TARGETED_OPTION_CANDLE_BACKFILL_BATCH_LIMIT", "25"))
    targeted_option_candle_backfill_max_contracts: int = int(os.getenv("TARGETED_OPTION_CANDLE_BACKFILL_MAX_CONTRACTS", "120"))
    targeted_option_candle_backfill_delay_seconds: float = float(os.getenv("TARGETED_OPTION_CANDLE_BACKFILL_DELAY_SECONDS", "0.5"))
    min_targeted_option_candle_coverage_pct: float = float(os.getenv("MIN_TARGETED_OPTION_CANDLE_COVERAGE_PCT", "80.0"))
    enable_live_option_candle_gap_backfill: bool = os.getenv("ENABLE_LIVE_OPTION_CANDLE_GAP_BACKFILL", "true").lower() == "true"
    live_option_candle_backfill_timeframes: str = os.getenv("LIVE_OPTION_CANDLE_BACKFILL_TIMEFRAMES", "1minute")
    live_option_candle_backfill_lookback_minutes: int = int(os.getenv("LIVE_OPTION_CANDLE_BACKFILL_LOOKBACK_MINUTES", "30"))
    live_option_candle_backfill_interval_seconds: int = int(os.getenv("LIVE_OPTION_CANDLE_BACKFILL_INTERVAL_SECONDS", "120"))
    live_option_candle_backfill_min_gap_seconds: int = int(os.getenv("LIVE_OPTION_CANDLE_BACKFILL_MIN_GAP_SECONDS", "90"))
    live_option_candle_backfill_max_contracts: int = int(os.getenv("LIVE_OPTION_CANDLE_BACKFILL_MAX_CONTRACTS", "20"))
    live_option_candle_backfill_batch_limit: int = int(os.getenv("LIVE_OPTION_CANDLE_BACKFILL_BATCH_LIMIT", "10"))
    live_option_candle_backfill_delay_seconds: float = float(os.getenv("LIVE_OPTION_CANDLE_BACKFILL_DELAY_SECONDS", "0.2"))
    live_option_candle_backfill_market_open_on_first_seen: bool = os.getenv("LIVE_OPTION_CANDLE_BACKFILL_MARKET_OPEN_ON_FIRST_SEEN", "true").lower() == "true"
    live_option_candle_backfill_session_start_time: str = os.getenv("LIVE_OPTION_CANDLE_BACKFILL_SESSION_START_TIME", "09:15")
    live_option_candle_backfill_max_historical_calls_per_run: int = int(os.getenv("LIVE_OPTION_CANDLE_BACKFILL_MAX_HISTORICAL_CALLS_PER_RUN", "6"))
    enable_on_demand_premium_candle_backfill: bool = os.getenv("ENABLE_ON_DEMAND_PREMIUM_CANDLE_BACKFILL", "true").lower() == "true"
    on_demand_premium_candle_backfill_cooldown_seconds: int = int(os.getenv("ON_DEMAND_PREMIUM_CANDLE_BACKFILL_COOLDOWN_SECONDS", "120"))
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
    automation_intraday_candle_sync: bool = os.getenv("AUTOMATION_INTRADAY_CANDLE_SYNC", "false").lower() == "true"
    automation_intraday_candle_sync_minutes: int = int(os.getenv("AUTOMATION_INTRADAY_CANDLE_SYNC_MINUTES", "5"))
    automation_place_orders: bool = os.getenv("AUTOMATION_PLACE_ORDERS", "true").lower() == "true"
    automation_confirm_live: bool = os.getenv("AUTOMATION_CONFIRM_LIVE", "false").lower() == "true"
    automation_scan_limit: int = int(os.getenv("AUTOMATION_SCAN_LIMIT", "3"))
    automation_strike_window_pct: float = float(os.getenv("AUTOMATION_STRIKE_WINDOW_PCT", "4.0"))
    automation_max_contracts_per_symbol: int = int(os.getenv("AUTOMATION_MAX_CONTRACTS_PER_SYMBOL", "120"))
    max_live_quote_age_seconds: int = int(os.getenv("MAX_LIVE_QUOTE_AGE_SECONDS", "8"))
    max_live_option_quote_age_seconds: int = int(os.getenv("MAX_LIVE_OPTION_QUOTE_AGE_SECONDS", "8"))
    max_live_chain_age_seconds: int = int(os.getenv("MAX_LIVE_CHAIN_AGE_SECONDS", "10"))
    max_live_candle_age_seconds: int = int(os.getenv("MAX_LIVE_CANDLE_AGE_SECONDS", "420"))
    max_paper_candle_age_seconds: int = int(os.getenv("MAX_PAPER_CANDLE_AGE_SECONDS", "1800"))
    max_premium_confirmation_candle_age_seconds: int = int(os.getenv("MAX_PREMIUM_CONFIRMATION_CANDLE_AGE_SECONDS", "900"))
    kite_snapshot_cache_ttl_seconds: int = int(os.getenv("KITE_SNAPSHOT_CACHE_TTL_SECONDS", "3"))
    kite_quote_cache_ttl_seconds: int = int(os.getenv("KITE_QUOTE_CACHE_TTL_SECONDS", "2"))
    kite_instrument_cache_ttl_seconds: int = int(os.getenv("KITE_INSTRUMENT_CACHE_TTL_SECONDS", "21600"))
    kite_instrument_cache_file: str = os.getenv("KITE_INSTRUMENT_CACHE_FILE", str(BASE_DIR / ".cache" / "kite_instruments.json"))
    kite_option_chain_quote_limit: int = int(os.getenv("KITE_OPTION_CHAIN_QUOTE_LIMIT", "80"))
    kite_option_chain_strike_radius: int = int(os.getenv("KITE_OPTION_CHAIN_STRIKE_RADIUS", "3"))
    kite_snapshot_historical_fallback_enabled: bool = os.getenv("KITE_SNAPSHOT_HISTORICAL_FALLBACK_ENABLED", "false").lower() == "true"
    kite_api_timeout_seconds: int = int(os.getenv("KITE_API_TIMEOUT_SECONDS", "5"))
    scanner_response_cache_ttl_seconds: int = int(os.getenv("SCANNER_RESPONSE_CACHE_TTL_SECONDS", "5"))
    scanner_response_stale_ttl_seconds: int = int(os.getenv("SCANNER_RESPONSE_STALE_TTL_SECONDS", "60"))
    scanner_refresh_stuck_seconds: int = int(os.getenv("SCANNER_REFRESH_STUCK_SECONDS", "30"))
    account_funds_cache_ttl_seconds: int = int(os.getenv("ACCOUNT_FUNDS_CACHE_TTL_SECONDS", "60"))
    dashboard_broker_cache_ttl_seconds: int = int(os.getenv("DASHBOARD_BROKER_CACHE_TTL_SECONDS", "60"))
    slow_api_log_threshold_seconds: float = float(os.getenv("SLOW_API_LOG_THRESHOLD_SECONDS", "3.0"))
    option_quote_premium_mismatch_tolerance_pct: float = float(os.getenv("OPTION_QUOTE_PREMIUM_MISMATCH_TOLERANCE_PCT", "25.0"))
    estimated_brokerage_per_order: float = float(os.getenv("ESTIMATED_BROKERAGE_PER_ORDER", "20.0"))
    estimated_stt_sell_pct: float = float(os.getenv("ESTIMATED_STT_SELL_PCT", "0.0625"))
    estimated_exchange_txn_pct: float = float(os.getenv("ESTIMATED_EXCHANGE_TXN_PCT", "0.053"))
    estimated_sebi_pct: float = float(os.getenv("ESTIMATED_SEBI_PCT", "0.0001"))
    estimated_gst_pct: float = float(os.getenv("ESTIMATED_GST_PCT", "18.0"))
    estimated_stamp_buy_pct: float = float(os.getenv("ESTIMATED_STAMP_BUY_PCT", "0.003"))
    paper_slippage_pct_per_side: float = float(os.getenv("PAPER_SLIPPAGE_PCT_PER_SIDE", "0.50"))
    paper_spread_impact_pct_per_side: float = float(os.getenv("PAPER_SPREAD_IMPACT_PCT_PER_SIDE", "0.25"))
    fast_exit_interval_seconds: int = int(os.getenv("FAST_EXIT_INTERVAL_SECONDS", "2"))
    enable_kite_websocket: bool = os.getenv("ENABLE_KITE_WEBSOCKET", "false").lower() == "true"
    websocket_price_stale_seconds: int = int(os.getenv("WEBSOCKET_PRICE_STALE_SECONDS", "3"))
    websocket_reconnect_enabled: bool = os.getenv("WEBSOCKET_RECONNECT_ENABLED", "true").lower() == "true"
    websocket_reconnect_min_gap_seconds: int = int(os.getenv("WEBSOCKET_RECONNECT_MIN_GAP_SECONDS", "5"))
    websocket_reconnect_window_seconds: int = int(os.getenv("WEBSOCKET_RECONNECT_WINDOW_SECONDS", "60"))
    websocket_reconnect_max_attempts_per_window: int = int(os.getenv("WEBSOCKET_RECONNECT_MAX_ATTEMPTS_PER_WINDOW", "5"))
    websocket_reconnect_max_delay_seconds: int = int(os.getenv("WEBSOCKET_RECONNECT_MAX_DELAY_SECONDS", "60"))
    websocket_rate_limit_cooldown_seconds: int = int(os.getenv("WEBSOCKET_RATE_LIMIT_COOLDOWN_SECONDS", "120"))
    websocket_live_stale_blocks: bool = os.getenv("WEBSOCKET_LIVE_STALE_BLOCKS", "true").lower() == "true"
    websocket_live_require_exchange_timestamp: bool = os.getenv("WEBSOCKET_LIVE_REQUIRE_EXCHANGE_TIMESTAMP", "true").lower() == "true"
    websocket_event_queue_size: int = int(os.getenv("WEBSOCKET_EVENT_QUEUE_SIZE", "1000"))
    websocket_candle_persist_queue_size: int = int(os.getenv("WEBSOCKET_CANDLE_PERSIST_QUEUE_SIZE", "1000"))
    enable_websocket_premium_candle_builder: bool = os.getenv("ENABLE_WEBSOCKET_PREMIUM_CANDLE_BUILDER", "true").lower() == "true"
    websocket_premium_candle_timeframe: str = os.getenv("WEBSOCKET_PREMIUM_CANDLE_TIMEFRAME", "1minute")
    websocket_premium_candle_retention_minutes: int = int(os.getenv("WEBSOCKET_PREMIUM_CANDLE_RETENTION_MINUTES", "60"))
    enable_websocket_candle_persistence: bool = os.getenv("ENABLE_WEBSOCKET_CANDLE_PERSISTENCE", "true").lower() == "true"
    websocket_candle_storage_prefix: str = os.getenv("WEBSOCKET_CANDLE_STORAGE_PREFIX", "WS_TOKEN")
    enable_websocket_candle_daily_cleanup: bool = os.getenv("ENABLE_WEBSOCKET_CANDLE_DAILY_CLEANUP", "true").lower() == "true"
    enable_websocket_candle_context_recovery: bool = os.getenv("ENABLE_WEBSOCKET_CANDLE_CONTEXT_RECOVERY", "true").lower() == "true"
    websocket_candle_context_recovery_lookback_minutes: int = int(os.getenv("WEBSOCKET_CANDLE_CONTEXT_RECOVERY_LOOKBACK_MINUTES", "390"))
    enable_market_data_gap_detection: bool = os.getenv("ENABLE_MARKET_DATA_GAP_DETECTION", "true").lower() == "true"
    max_websocket_gap_seconds: int = int(os.getenv("MAX_WEBSOCKET_GAP_SECONDS", "10"))
    enable_websocket_gap_backfill: bool = os.getenv("ENABLE_WEBSOCKET_GAP_BACKFILL", "false").lower() == "true"
    websocket_live_gap_polling_fallback: bool = os.getenv("WEBSOCKET_LIVE_GAP_POLLING_FALLBACK", "true").lower() == "true"
    cancel_armed_entries_on_data_gap: bool = os.getenv("CANCEL_ARMED_ENTRIES_ON_DATA_GAP", "true").lower() == "true"
    enable_banknifty_option_prewarm: bool = os.getenv("ENABLE_BANKNIFTY_OPTION_PREWARM", "true").lower() == "true"
    banknifty_prewarm_strike_depth: int = int(os.getenv("BANKNIFTY_PREWARM_STRIKE_DEPTH", "3"))
    banknifty_prewarm_refresh_seconds: int = int(os.getenv("BANKNIFTY_PREWARM_REFRESH_SECONDS", "60"))
    banknifty_prewarm_overlap_seconds: int = int(os.getenv("BANKNIFTY_PREWARM_OVERLAP_SECONDS", "180"))
    banknifty_prewarm_rotation_hysteresis_pct: float = float(os.getenv("BANKNIFTY_PREWARM_ROTATION_HYSTERESIS_PCT", "60.0"))
    banknifty_contract_stickiness_seconds: int = int(os.getenv("BANKNIFTY_CONTRACT_STICKINESS_SECONDS", "180"))
    banknifty_contract_switch_score_advantage: float = float(os.getenv("BANKNIFTY_CONTRACT_SWITCH_SCORE_ADVANTAGE", "10.0"))
    min_websocket_premium_candles: int = int(os.getenv("MIN_WEBSOCKET_PREMIUM_CANDLES", "3"))
    max_websocket_premium_candle_age_seconds: int = int(os.getenv("MAX_WEBSOCKET_PREMIUM_CANDLE_AGE_SECONDS", "180"))
    max_stored_premium_candle_age_seconds: int = int(os.getenv("MAX_STORED_PREMIUM_CANDLE_AGE_SECONDS", "300"))
    max_entry_chase_pct: float = float(os.getenv("MAX_ENTRY_CHASE_PCT", "1.0"))
    max_premium_move_from_base_pct: float = float(os.getenv("MAX_PREMIUM_MOVE_FROM_BASE_PCT", "4.0"))
    min_remaining_risk_reward: float = float(os.getenv("MIN_REMAINING_RISK_REWARD", "1.3"))
    min_target1_room_pct: float = float(os.getenv("MIN_TARGET1_ROOM_PCT", "8.0"))
    entry_armed_distance_to_trigger_pct: float = float(os.getenv("ENTRY_ARMED_DISTANCE_TO_TRIGGER_PCT", "0.75"))
    entry_trigger_lookback_candles: int = int(os.getenv("ENTRY_TRIGGER_LOOKBACK_CANDLES", "3"))
    min_entry_expected_move_coverage: float = float(os.getenv("MIN_ENTRY_EXPECTED_MOVE_COVERAGE", "0.90"))
    min_entry_room_to_level_pct: float = float(os.getenv("MIN_ENTRY_ROOM_TO_LEVEL_PCT", "0.25"))
    enable_event_driven_paper_entry: bool = os.getenv("ENABLE_EVENT_DRIVEN_PAPER_ENTRY", "true").lower() == "true"
    enable_event_driven_live_entry: bool = os.getenv("ENABLE_EVENT_DRIVEN_LIVE_ENTRY", "false").lower() == "true"
    armed_entry_valid_seconds: int = int(os.getenv("ARMED_ENTRY_VALID_SECONDS", "90"))
    enable_early_armed_entry: bool = os.getenv("ENABLE_EARLY_ARMED_ENTRY", "true").lower() == "true"
    early_armed_entry_paper_only: bool = os.getenv("EARLY_ARMED_ENTRY_PAPER_ONLY", "true").lower() == "true"
    early_arm_min_score: int = int(os.getenv("EARLY_ARM_MIN_SCORE", "75"))
    early_arm_trigger_buffer_pct: float = float(os.getenv("EARLY_ARM_TRIGGER_BUFFER_PCT", "0.25"))
    early_arm_allow_premium_pending: bool = os.getenv("EARLY_ARM_ALLOW_PREMIUM_PENDING", "true").lower() == "true"
    enable_tick_quality_confirmation: bool = os.getenv("ENABLE_TICK_QUALITY_CONFIRMATION", "true").lower() == "true"
    tick_quality_min_ticks_above_trigger: int = int(os.getenv("TICK_QUALITY_MIN_TICKS_ABOVE_TRIGGER", "2"))
    tick_quality_hold_seconds: float = float(os.getenv("TICK_QUALITY_HOLD_SECONDS", "1.0"))
    tick_quality_fast_min_ticks: int = int(os.getenv("TICK_QUALITY_FAST_MIN_TICKS", "4"))
    tick_quality_fast_hold_seconds: float = float(os.getenv("TICK_QUALITY_FAST_HOLD_SECONDS", "0.25"))
    tick_quality_require_bid_progress: bool = os.getenv("TICK_QUALITY_REQUIRE_BID_PROGRESS", "true").lower() == "true"
    tick_quality_max_spread_multiplier: float = float(os.getenv("TICK_QUALITY_MAX_SPREAD_MULTIPLIER", "1.5"))
    enable_normalized_entry_chase: bool = os.getenv("ENABLE_NORMALIZED_ENTRY_CHASE", "true").lower() == "true"
    normalized_entry_chase_max_atr: float = float(os.getenv("NORMALIZED_ENTRY_CHASE_MAX_ATR", "0.75"))
    normalized_entry_chase_lookback_ticks: int = int(os.getenv("NORMALIZED_ENTRY_CHASE_LOOKBACK_TICKS", "20"))
    fast_rally_window_seconds: float = float(os.getenv("FAST_RALLY_WINDOW_SECONDS", "5.0"))
    fast_rally_trigger_pct: float = float(os.getenv("FAST_RALLY_TRIGGER_PCT", "0.08"))
    fast_rally_rescan_cooldown_seconds: float = float(os.getenv("FAST_RALLY_RESCAN_COOLDOWN_SECONDS", "2.0"))
    fast_scan_context_max_age_seconds: float = float(os.getenv("FAST_SCAN_CONTEXT_MAX_AGE_SECONDS", "90.0"))
    fast_scan_context_refresh_seconds: float = float(os.getenv("FAST_SCAN_CONTEXT_REFRESH_SECONDS", "30.0"))
    scheduled_scan_max_rest_calls: int = int(os.getenv("SCHEDULED_SCAN_MAX_REST_CALLS", "3"))
    enable_underlying_candle_pipeline: bool = os.getenv("ENABLE_UNDERLYING_CANDLE_PIPELINE", "true").lower() == "true"
    underlying_candle_symbol: str = os.getenv("UNDERLYING_CANDLE_SYMBOL", "BANKNIFTY")
    enable_raw_tick_capture: bool = os.getenv("ENABLE_RAW_TICK_CAPTURE", "true").lower() == "true"
    raw_tick_queue_size: int = int(os.getenv("RAW_TICK_QUEUE_SIZE", "10000"))
    raw_tick_batch_size: int = int(os.getenv("RAW_TICK_BATCH_SIZE", "250"))
    raw_tick_retention_days: int = int(os.getenv("RAW_TICK_RETENTION_DAYS", "10"))
    raw_tick_cleanup_interval_seconds: int = int(os.getenv("RAW_TICK_CLEANUP_INTERVAL_SECONDS", "3600"))
    latency_sample_limit: int = int(os.getenv("LATENCY_SAMPLE_LIMIT", "5000"))
    live_exit_max_retry_count: int = int(os.getenv("LIVE_EXIT_MAX_RETRY_COUNT", "2"))
    live_reconciliation_blocks_automation: bool = os.getenv("LIVE_RECONCILIATION_BLOCKS_AUTOMATION", "true").lower() == "true"
    enable_underlying_invalidation_exit: bool = os.getenv("ENABLE_UNDERLYING_INVALIDATION_EXIT", "true").lower() == "true"
    enable_premium_invalidation_exit: bool = os.getenv("ENABLE_PREMIUM_INVALIDATION_EXIT", "true").lower() == "true"
    enable_broker_emergency_sl: bool = os.getenv("ENABLE_BROKER_EMERGENCY_SL", "false").lower() == "true"
    require_broker_protective_stop_for_live_entry: bool = os.getenv("REQUIRE_BROKER_PROTECTIVE_STOP_FOR_LIVE_ENTRY", "true").lower() == "true"
    enable_partial_booking: bool = os.getenv("ENABLE_PARTIAL_BOOKING", "true").lower() == "true"
    partial_target1_pct: float = float(os.getenv("PARTIAL_TARGET1_PCT", "50.0"))
    partial_move_sl_to_cost: bool = os.getenv("PARTIAL_MOVE_SL_TO_COST", "true").lower() == "true"


settings = Settings()
