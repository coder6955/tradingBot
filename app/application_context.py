from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.providers.kite_provider import KiteProvider
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.banknifty_option_prewarm_service import BankNiftyOptionPrewarmService
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed
from app.services.market_data_runtime_service import MarketDataRuntimeService
from app.services.market_session_service import MarketSessionService


@dataclass(frozen=True)
class ApplicationContext:
    """Composition root for cross-cutting runtime services."""

    market_session_service: MarketSessionService
    kite_websocket_price_feed: KiteWebSocketPriceFeed
    active_trade_price_feed: ActiveTradePriceFeed
    banknifty_option_prewarm_service: BankNiftyOptionPrewarmService
    market_data_runtime_service: MarketDataRuntimeService
    kite_provider_factory: Callable[[], KiteProvider]
