from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed, WebSocketTick
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.time_utils import ist_now_naive


@dataclass(frozen=True)
class PriceTick:
    instrument: str
    price: float
    timestamp: datetime
    source: str
    instrument_token: int | None = None
    volume: float | None = None
    bid: float | None = None
    ask: float | None = None
    buy_depth: tuple[dict[str, Any], ...] = ()
    sell_depth: tuple[dict[str, Any], ...] = ()
    age_seconds: float | None = None
    timestamp_source: str | None = None


class ActivePriceFeed(Protocol):
    def latest_price(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        instrument_token: int | None = None,
        mode: str = "paper",
        source_override: str | None = None,
    ) -> PriceTick | None: ...


class KitePollingPriceFeed:
    """Current active-trade feed; replaceable with Kite ticker/WebSocket later."""

    def __init__(
        self,
        provider: KiteProvider,
        market_data_coordinator: MarketDataCoordinator | None = None,
    ) -> None:
        self.provider = provider
        self.market_data_coordinator = market_data_coordinator

    def latest_price(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        instrument_token: int | None = None,
        mode: str = "paper",
        source_override: str | None = None,
    ) -> PriceTick | None:
        instrument = f"{exchange or settings.option_exchange}:{tradingsymbol}"
        try:
            if self.market_data_coordinator is not None:
                quote = self.market_data_coordinator.quote(
                    [instrument], provider=self.provider
                )
            else:
                quote = self.provider.quote([instrument])
        except Exception:
            return None
        data = quote.get(instrument) or quote.get(tradingsymbol) or {}
        if not isinstance(data, dict):
            return None
        for key in ("last_price", "last_traded_price"):
            value = data.get(key)
            try:
                if value is not None:
                    depth = data.get("depth", {}) if isinstance(data, dict) else {}
                    buy_depth = depth.get("buy", []) if isinstance(depth, dict) else []
                    sell_depth = (
                        depth.get("sell", []) if isinstance(depth, dict) else []
                    )
                    return PriceTick(
                        instrument=instrument,
                        price=float(value),
                        timestamp=ist_now_naive(),
                        source=source_override or "kite_polling",
                        instrument_token=instrument_token,
                        volume=self._float(
                            data.get("volume") or data.get("volume_traded")
                        ),
                        bid=self._float(buy_depth[0].get("price"))
                        if buy_depth
                        else None,
                        ask=self._float(sell_depth[0].get("price"))
                        if sell_depth
                        else None,
                        buy_depth=tuple(
                            dict(level)
                            for level in buy_depth
                            if isinstance(level, dict)
                        ),
                        sell_depth=tuple(
                            dict(level)
                            for level in sell_depth
                            if isinstance(level, dict)
                        ),
                        age_seconds=0.0,
                        timestamp_source="local_receive_time",
                    )
            except (TypeError, ValueError):
                continue
        return None

    def _float(self, value: Any) -> float | None:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            return None
        return None


class ActiveTradePriceFeed:
    """WebSocket-first active trade feed with polling fallback for paper mode."""

    def __init__(
        self,
        websocket_feed: KiteWebSocketPriceFeed | None = None,
        market_data_coordinator: MarketDataCoordinator | None = None,
    ) -> None:
        self.websocket_feed = websocket_feed
        self.market_data_coordinator = market_data_coordinator
        self.last_reason: str | None = None
        self.fallback_active = False
        self.fallback_count = 0
        self.active_trade_tokens: set[int] = set()
        self.last_tick: PriceTick | None = None

    def start(self) -> dict[str, Any]:
        if self.websocket_feed is None:
            return {"started": False, "reason": "websocket_feed_missing"}
        return self.websocket_feed.start()

    def stop(self) -> dict[str, Any]:
        if self.websocket_feed is None:
            return {"stopped": True, "reason": "websocket_feed_missing"}
        return self.websocket_feed.stop()

    def subscribe(
        self, tokens: set[int] | list[int] | tuple[int, ...]
    ) -> dict[str, Any]:
        clean_tokens = {int(token) for token in tokens if self._safe_token(token)}
        self.active_trade_tokens.update(clean_tokens)
        if not clean_tokens:
            self.last_reason = "token_missing"
            return {"subscribed": [], "reason": self.last_reason}
        if not settings.enable_kite_websocket or self.websocket_feed is None:
            self.last_reason = "websocket_disabled"
            return {"subscribed": [], "reason": self.last_reason}
        try:
            result = self.websocket_feed.subscribe(
                clean_tokens, owner="active_trade", mode="full"
            )
        except TypeError:
            result = self.websocket_feed.subscribe(clean_tokens)
        self.last_reason = str(result.get("reason")) if result.get("reason") else None
        return result

    def unsubscribe(
        self, tokens: set[int] | list[int] | tuple[int, ...]
    ) -> dict[str, Any]:
        clean_tokens = {int(token) for token in tokens if self._safe_token(token)}
        self.active_trade_tokens.difference_update(clean_tokens)
        if self.websocket_feed is None:
            return {
                "unsubscribed": sorted(clean_tokens),
                "reason": "websocket_feed_missing",
            }
        release = getattr(self.websocket_feed, "release_owner", None)
        if callable(release):
            return release("active_trade", clean_tokens)
        return self.websocket_feed.unsubscribe(clean_tokens)

    def latest_price(
        self,
        *,
        provider: KiteProvider,
        exchange: str,
        tradingsymbol: str,
        instrument_token: int | None = None,
        mode: str = "paper",
    ) -> PriceTick | None:
        self.last_reason = None
        self.fallback_active = False
        if settings.enable_kite_websocket:
            ws_tick = self._websocket_tick(instrument_token, mode=mode)
            if ws_tick:
                self.last_tick = self._to_price_tick(
                    exchange=exchange, tradingsymbol=tradingsymbol, tick=ws_tick
                )
                return self.last_tick
            if str(mode).lower() == "live" and settings.websocket_live_stale_blocks:
                if (
                    settings.websocket_live_gap_polling_fallback
                    and self.last_reason
                    in {"websocket_disconnected", "tick_stale", "token_missing"}
                ):
                    tick = KitePollingPriceFeed(
                        provider, self.market_data_coordinator
                    ).latest_price(
                        exchange=exchange,
                        tradingsymbol=tradingsymbol,
                        instrument_token=instrument_token,
                        mode=mode,
                        source_override="kite_polling_after_websocket_gap",
                    )
                    if tick is not None:
                        self.fallback_active = True
                        self.fallback_count += 1
                        self.last_tick = tick
                        return tick
                self.last_tick = None
                return None

        self.fallback_active = True
        self.fallback_count += 1
        tick = KitePollingPriceFeed(
            provider, self.market_data_coordinator
        ).latest_price(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            instrument_token=instrument_token,
            mode=mode,
        )
        self.last_tick = tick
        if tick is None and self.last_reason is None:
            self.last_reason = "quote_unavailable"
        return tick

    def is_fresh(
        self, instrument_token: int, max_age_seconds: int | None = None
    ) -> bool:
        if self.websocket_feed is None:
            return False
        return self.websocket_feed.is_fresh(instrument_token, max_age_seconds)

    def status(self) -> dict[str, Any]:
        if self.websocket_feed is None:
            return {
                "websocket_enabled": settings.enable_kite_websocket,
                "websocket_connected": False,
                "subscribed_tokens": [],
                "latest_tick_age": {},
                "active_trade_tokens": sorted(self.active_trade_tokens),
                "fallback_active": self.fallback_active,
                "fallback_count": self.fallback_count,
                "reconnect_count": 0,
                "last_reason": self.last_reason,
            }
        status = self.websocket_feed.status(
            active_trade_tokens=self.active_trade_tokens,
            fallback_active=self.fallback_active,
        )
        status["last_reason"] = self.last_reason
        status["fallback_count"] = self.fallback_count
        return status

    def _websocket_tick(
        self, instrument_token: int | None, *, mode: str = "paper"
    ) -> WebSocketTick | None:
        if self.websocket_feed is None:
            self.last_reason = "websocket_feed_missing"
            return None
        if not instrument_token:
            self.last_reason = "token_missing"
            return None
        if not self.websocket_feed.connected:
            self.last_reason = "websocket_disconnected"
            return None
        tick = self.websocket_feed.get_latest_tick(int(instrument_token))
        if tick is None:
            self.last_reason = "token_missing"
            return None
        if not self.websocket_feed.is_fresh(
            int(instrument_token), settings.websocket_price_stale_seconds
        ):
            self.last_reason = "tick_stale"
            return None
        if (
            str(mode).lower() == "live"
            and settings.websocket_live_require_exchange_timestamp
            and tick.timestamp_source == "local_receive_time"
        ):
            self.last_reason = "tick_missing_exchange_timestamp"
            return None
        return tick

    def _to_price_tick(
        self, *, exchange: str, tradingsymbol: str, tick: WebSocketTick
    ) -> PriceTick:
        return PriceTick(
            instrument=f"{exchange or settings.option_exchange}:{tradingsymbol}",
            price=tick.price,
            timestamp=tick.timestamp,
            source="kite_websocket",
            instrument_token=tick.instrument_token,
            volume=tick.volume,
            bid=tick.bid,
            ask=tick.ask,
            buy_depth=tick.buy_depth,
            sell_depth=tick.sell_depth,
            age_seconds=round(
                max(
                    0.0,
                    (
                        ist_now_naive() - tick.timestamp.replace(tzinfo=None)
                    ).total_seconds(),
                ),
                3,
            ),
            timestamp_source=tick.timestamp_source,
        )

    def _safe_token(self, value: Any) -> bool:
        try:
            return int(value) > 0
        except (TypeError, ValueError):
            return False
