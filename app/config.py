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
    max_option_premium_pct: float = float(os.getenv("MAX_OPTION_PREMIUM_PCT", "1.5"))
    min_signal_score: int = int(os.getenv("MIN_SIGNAL_SCORE", "80"))
    min_option_liquidity_score: int = int(os.getenv("MIN_OPTION_LIQUIDITY_SCORE", "70"))
    account_equity: float = float(os.getenv("ACCOUNT_EQUITY", "100000"))
    default_product: str = os.getenv("KITE_DEFAULT_PRODUCT", "MIS")


settings = Settings()
