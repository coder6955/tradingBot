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
    paper_trading_mode: bool = os.getenv("PAPER_TRADING_MODE", "true").lower() == "true"
    live_trading_mode: bool = os.getenv("LIVE_TRADING_MODE", "false").lower() == "true"
    use_kite_market_data: bool = os.getenv("USE_KITE_MARKET_DATA", "true").lower() == "true"
    default_exchange: str = os.getenv("DEFAULT_EXCHANGE", "NSE")
    option_exchange: str = os.getenv("OPTION_EXCHANGE", "NFO")
    max_risk_per_trade_pct: float = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "1.0"))
    max_option_premium_pct: float = float(os.getenv("MAX_OPTION_PREMIUM_PCT", "8.0"))
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


settings = Settings()
