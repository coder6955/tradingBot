import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.config import settings
from app.models import Signal
from app.providers.kite_auth_state import kite_auth_state
from app.services.active_price_feed import ActiveTradePriceFeed, PriceTick
from app.services.database import Candle, get_session, init_db
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed, WebSocketTick
from app.services.paper_trading_service import PaperTradingService
from app.services.trade_exit_service import TradeExitService
from app.services.trade_repository import TradeRepository
from app.services.time_utils import ist_now_naive


def regular_market_now() -> datetime:
    return datetime(2026, 7, 3, 10, 30, 0)


def weekend_now() -> datetime:
    return datetime(2026, 7, 4, 10, 30, 0)


def after_market_now() -> datetime:
    return datetime(2026, 7, 3, 16, 0, 0)


class FakeTicker:
    MODE_FULL = "full"

    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.subscribed: list[list[int]] = []
        self.unsubscribed: list[list[int]] = []
        self.modes: list[tuple[str, list[int]]] = []
        self.on_connect = None
        self.on_ticks = None
        self.on_close = None
        self.on_error = None
        self.on_reconnect = None
        self.on_noreconnect = None

    def connect(self, threaded: bool = False) -> None:
        if self.on_connect:
            self.on_connect(self, {"threaded": threaded})

    def subscribe(self, tokens: list[int]) -> None:
        self.subscribed.append(list(tokens))

    def unsubscribe(self, tokens: list[int]) -> None:
        self.unsubscribed.append(list(tokens))

    def set_mode(self, mode: str, tokens: list[int]) -> None:
        self.modes.append((mode, list(tokens)))


class FakeProvider:
    def __init__(self, order_status: str = "OPEN", position_quantity: int = 15) -> None:
        self.order_status = order_status
        self.position_quantity = position_quantity
        self.place_order_count = 0

    def quote(self, instruments):
        return {
            instruments[0]: {
                "last_price": 101.0,
                "volume": 1000,
                "depth": {"buy": [{"price": 100.5}], "sell": [{"price": 101.5}]},
            }
        }

    def instruments(self, exchange=None):
        if exchange == "NFO":
            return [{"tradingsymbol": "BANKNIFTY26JUL58000CE", "exchange": "NFO", "instrument_token": 123}]
        return [{"tradingsymbol": "NIFTY BANK", "name": "NIFTY BANK", "instrument_token": 260105}]

    def place_order(self, **kwargs):
        self.place_order_count += 1
        return {"status": "submitted", "order_id": "exit-1", **kwargs}

    def order_history(self, order_id):
        return [{"order_id": order_id, "status": self.order_status, "filled_quantity": 15, "quantity": 15, "average_price": 111.0}]

    def positions(self):
        return {
            "net": [
                {
                    "exchange": "NFO",
                    "tradingsymbol": "BANKNIFTY26JUL58000CE",
                    "quantity": self.position_quantity,
                }
            ]
        }


class FakeActiveFeed:
    def __init__(self, price: float | None, reason: str | None = None) -> None:
        self.price = price
        self.last_reason = reason
        self.fallback_active = False
        self.active_trade_tokens: set[int] = set()
        self.subscriptions: list[set[int]] = []

    def latest_price(self, *, provider, exchange, tradingsymbol, instrument_token=None, mode="paper"):
        if self.price is None:
            return None
        return PriceTick(
            instrument=f"{exchange}:{tradingsymbol}",
            price=self.price,
            timestamp=ist_now_naive(),
            source="kite_websocket",
            instrument_token=instrument_token,
        )

    def subscribe(self, tokens):
        clean = {int(token) for token in tokens}
        self.active_trade_tokens.update(clean)
        self.subscriptions.append(clean)
        return {"subscribed": sorted(clean)}

    def unsubscribe(self, tokens):
        self.active_trade_tokens.difference_update({int(token) for token in tokens})
        return {"unsubscribed": sorted(tokens)}

    def status(self):
        return {
            "websocket_enabled": True,
            "websocket_connected": True,
            "subscribed_tokens": sorted(self.active_trade_tokens),
            "latest_tick_age": {},
            "active_trade_tokens": sorted(self.active_trade_tokens),
            "fallback_active": self.fallback_active,
            "reconnect_count": 0,
            "last_reason": self.last_reason,
        }


class KiteWebSocketPriceFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_kite_websocket": settings.enable_kite_websocket,
            "websocket_live_stale_blocks": settings.websocket_live_stale_blocks,
            "enable_underlying_invalidation_exit": settings.enable_underlying_invalidation_exit,
            "enable_premium_invalidation_exit": settings.enable_premium_invalidation_exit,
            "live_auto_squareoff": settings.live_auto_squareoff,
            "option_time_stop_minutes": settings.option_time_stop_minutes,
            "exit_open_trades_before_close_minutes": settings.exit_open_trades_before_close_minutes,
            "enable_websocket_candle_persistence": settings.enable_websocket_candle_persistence,
            "enable_websocket_candle_daily_cleanup": settings.enable_websocket_candle_daily_cleanup,
            "websocket_reconnect_min_gap_seconds": settings.websocket_reconnect_min_gap_seconds,
            "websocket_reconnect_window_seconds": settings.websocket_reconnect_window_seconds,
            "websocket_reconnect_max_attempts_per_window": settings.websocket_reconnect_max_attempts_per_window,
            "websocket_event_queue_size": settings.websocket_event_queue_size,
            "websocket_candle_persist_queue_size": settings.websocket_candle_persist_queue_size,
            "enable_market_data_gap_detection": settings.enable_market_data_gap_detection,
            "max_websocket_gap_seconds": settings.max_websocket_gap_seconds,
            "websocket_live_gap_polling_fallback": settings.websocket_live_gap_polling_fallback,
            "cancel_armed_entries_on_data_gap": settings.cancel_armed_entries_on_data_gap,
        }
        kite_auth_state.clear()
        object.__setattr__(settings, "enable_kite_websocket", True)
        object.__setattr__(settings, "websocket_live_stale_blocks", True)
        object.__setattr__(settings, "enable_websocket_candle_persistence", True)
        object.__setattr__(settings, "enable_websocket_candle_daily_cleanup", True)
        object.__setattr__(settings, "websocket_reconnect_min_gap_seconds", 5)
        object.__setattr__(settings, "websocket_reconnect_window_seconds", 60)
        object.__setattr__(settings, "websocket_reconnect_max_attempts_per_window", 5)
        object.__setattr__(settings, "enable_market_data_gap_detection", True)
        object.__setattr__(settings, "max_websocket_gap_seconds", 10)
        object.__setattr__(settings, "websocket_live_gap_polling_fallback", True)
        object.__setattr__(settings, "cancel_armed_entries_on_data_gap", True)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        kite_auth_state.clear()
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_websocket_feed_stores_latest_tick(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=regular_market_now)
        try:
            feed.start()
            feed._on_ticks(None, [{"instrument_token": 123, "last_price": 88.5, "volume_traded": 900}])

            tick = feed.get_latest_tick(123)

            self.assertIsNotNone(tick)
            self.assertEqual(tick.price, 88.5)
            self.assertEqual(feed.get_latest_price(123), 88.5)
        finally:
            feed.stop()

    def test_start_refreshes_access_token_from_store(self) -> None:
        with patch("app.services.kite_websocket_price_feed.load_access_token", return_value="fresh-token"):
            feed = KiteWebSocketPriceFeed(
                api_key="k",
                access_token="stale-token",
                ticker_factory=FakeTicker,
                clock=regular_market_now,
            )
            try:
                result = feed.start()

                self.assertTrue(result["started"])
                self.assertEqual(feed.access_token, "fresh-token")
                self.assertEqual(feed._ticker.access_token, "fresh-token")
            finally:
                feed.stop()

    def test_websocket_feed_dispatches_tick_handler(self) -> None:
        seen = []
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, tick_handler=seen.append)

        feed._on_ticks(None, [{"instrument_token": 123, "last_price": 88.5, "volume_traded": 900}])

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].instrument_token, 123)
        self.assertEqual(seen[0].price, 88.5)

    def test_websocket_ticks_create_and_update_one_minute_candles(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker)
        base = ist_now_naive().replace(second=10, microsecond=0)

        feed._on_ticks(
            None,
            [
                {"instrument_token": 123, "last_price": 100, "volume_traded": 1000, "exchange_timestamp": base},
                {"instrument_token": 123, "last_price": 106, "volume_traded": 1100, "exchange_timestamp": base + timedelta(seconds=20)},
                {"instrument_token": 123, "last_price": 98, "volume_traded": 1200, "exchange_timestamp": base + timedelta(seconds=40)},
            ],
        )

        candles = feed.get_current_session_premium_candles(123)

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].open_price, 100)
        self.assertEqual(candles[0].high_price, 106)
        self.assertEqual(candles[0].low_price, 98)
        self.assertEqual(candles[0].close_price, 98)
        self.assertEqual(candles[0].tick_count, 3)
        self.assertEqual(feed.tick_count(123), 3)

    def test_websocket_candle_rolls_on_minute_change(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker)
        base = ist_now_naive().replace(second=10, microsecond=0)

        feed._on_ticks(
            None,
            [
                {"instrument_token": 123, "last_price": 100, "exchange_timestamp": base},
                {"instrument_token": 123, "last_price": 104, "exchange_timestamp": base + timedelta(minutes=1)},
            ],
        )

        status = feed.premium_candle_status(123)

        self.assertEqual(status["current_session_candle_count"], 2)
        self.assertEqual(status["last_completed_candle"]["close"], 100)
        self.assertEqual(status["current_building_candle"]["close"], 104)

    def test_websocket_premium_candles_persist_and_rehydrate_from_db(self) -> None:
        base = regular_market_now().replace(second=10, microsecond=0)
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=lambda: base)

        feed._on_ticks(None, [{"instrument_token": 123, "last_price": 100, "exchange_timestamp": base}])
        feed._on_ticks(None, [{"instrument_token": 123, "last_price": 104, "exchange_timestamp": base + timedelta(seconds=20)}])

        session = get_session()
        try:
            stored = session.query(Candle).filter(Candle.symbol == "WS_TOKEN:123").all()
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].close_price, 104)
        finally:
            session.close()

        rehydrated = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=lambda: base + timedelta(minutes=1))
        rehydrated.subscribe({123})
        candles = rehydrated.get_current_session_premium_candles(123)
        status = rehydrated.status()

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].close_price, 104)
        self.assertEqual(candles[0].source, "websocket_builder_rehydrated")
        self.assertEqual(status["candle_persistence"]["rehydrated_candle_count"], 1)
        rehydrated.stop()

    def test_old_websocket_one_minute_candles_are_cleaned_without_touching_history(self) -> None:
        now = regular_market_now().replace(second=0, microsecond=0)
        yesterday = now - timedelta(days=1)
        current_day = now - timedelta(minutes=5)
        session = get_session()
        try:
            session.add_all(
                [
                    Candle(
                        symbol="WS_TOKEN:123",
                        timeframe="1minute",
                        timestamp=yesterday,
                        open_price=100,
                        high_price=101,
                        low_price=99,
                        close_price=100,
                        volume=10,
                    ),
                    Candle(
                        symbol="WS_TOKEN:123",
                        timeframe="1minute",
                        timestamp=current_day,
                        open_price=102,
                        high_price=103,
                        low_price=101,
                        close_price=102,
                        volume=10,
                    ),
                    Candle(
                        symbol="WS_TOKEN:123",
                        timeframe="5minute",
                        timestamp=yesterday,
                        open_price=104,
                        high_price=105,
                        low_price=103,
                        close_price=104,
                        volume=10,
                    ),
                    Candle(
                        symbol="BANKNIFTY26JUL58000CE",
                        timeframe="1minute",
                        timestamp=yesterday,
                        open_price=106,
                        high_price=107,
                        low_price=105,
                        close_price=106,
                        volume=10,
                    ),
                ]
            )
            session.commit()
        finally:
            session.close()

        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=lambda: now)
        result = feed.cleanup_old_persisted_candles(force=True)

        session = get_session()
        try:
            remaining = {
                (row.symbol, row.timeframe, row.timestamp)
                for row in session.query(Candle).order_by(Candle.symbol.asc(), Candle.timeframe.asc()).all()
            }
        finally:
            session.close()

        self.assertEqual(result["deleted"], 1)
        self.assertNotIn(("WS_TOKEN:123", "1minute", yesterday), remaining)
        self.assertIn(("WS_TOKEN:123", "1minute", current_day), remaining)
        self.assertIn(("WS_TOKEN:123", "5minute", yesterday), remaining)
        self.assertIn(("BANKNIFTY26JUL58000CE", "1minute", yesterday), remaining)

    def test_start_cleans_old_websocket_candles_even_when_market_is_closed(self) -> None:
        now = weekend_now().replace(second=0, microsecond=0)
        yesterday = now - timedelta(days=1)
        session = get_session()
        try:
            session.add(
                Candle(
                    symbol="WS_TOKEN:456",
                    timeframe="1minute",
                    timestamp=yesterday,
                    open_price=100,
                    high_price=101,
                    low_price=99,
                    close_price=100,
                    volume=10,
                )
            )
            session.commit()
        finally:
            session.close()

        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=lambda: now)
        start_result = feed.start()
        status = feed.status()

        session = get_session()
        try:
            count = session.query(Candle).filter(Candle.symbol == "WS_TOKEN:456").count()
        finally:
            session.close()

        self.assertFalse(start_result["started"])
        self.assertEqual(start_result["reason"], "market_closed")
        self.assertEqual(count, 0)
        self.assertEqual(status["candle_persistence"]["last_cleanup_deleted"], 1)

    def test_websocket_gap_detection_dispatches_event(self) -> None:
        seen: list[dict[str, object]] = []
        base = regular_market_now().replace(second=0, microsecond=0)
        now = {"value": base}
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=lambda: now["value"], gap_handler=seen.append)

        feed._on_ticks(None, [{"instrument_token": 123, "last_price": 100, "exchange_timestamp": base}])
        now["value"] = base + timedelta(seconds=30)
        feed._on_ticks(None, [{"instrument_token": 123, "last_price": 105, "exchange_timestamp": base + timedelta(seconds=30)}])

        status = feed.status()
        self.assertTrue(status["data_gap"]["data_gap_detected"])
        self.assertEqual(status["data_gap"]["latest_gap"]["reason"], "tick_gap_detected")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["instrument_token"], 123)

    def test_stale_tick_detection(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker)
        feed._ticks[123] = WebSocketTick(instrument_token=123, price=10, timestamp=ist_now_naive() - timedelta(seconds=10))

        self.assertFalse(feed.is_fresh(123, max_age_seconds=3))

    def test_reconnect_resubscribes_active_tokens(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=regular_market_now)
        try:
            feed.start()
            feed.subscribe({111, 222})
            ticker = feed._ticker
            ticker.subscribed.clear()

            feed._on_reconnect(ticker, 1)

            self.assertEqual(feed.reconnect_count, 1)
            self.assertIn([111, 222], [sorted(item) for item in ticker.subscribed])
        finally:
            feed.stop()

    def test_subscribe_starts_websocket_during_regular_market(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=regular_market_now)

        try:
            result = feed.subscribe({111, 222})

            self.assertTrue(feed.running)
            self.assertTrue(feed.connected)
            self.assertEqual(result["subscribed"], [111, 222])
        finally:
            feed.stop()

    def test_duplicate_start_is_prevented(self) -> None:
        created: list[FakeTicker] = []

        class CountingTicker(FakeTicker):
            def __init__(self, api_key: str, access_token: str) -> None:
                super().__init__(api_key, access_token)
                created.append(self)

        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=CountingTicker, clock=regular_market_now)
        try:
            first = feed.start()
            second = feed.start()
            status = feed.status()

            self.assertTrue(first["started"])
            self.assertTrue(second["already_running"])
            self.assertEqual(len(created), 1)
            self.assertEqual(status["duplicate_start_prevented_count"], 1)
        finally:
            feed.stop()

    def test_subscribe_queues_without_connecting_on_weekend(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=weekend_now)

        result = feed.subscribe({111, 222})
        status = feed.status()

        self.assertFalse(feed.running)
        self.assertFalse(feed.connected)
        self.assertEqual(result["reason"], "market_closed")
        self.assertEqual(result["queued"], [111, 222])
        self.assertEqual(status["websocket_status"], "DISABLED_OUTSIDE_MARKET_HOURS")
        self.assertEqual(status["reconnect_skipped_reason"], "market_closed")

    def test_websocket_start_skips_weekend_without_error_or_reconnect(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=weekend_now)

        result = feed.start()
        status = feed.status()

        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "market_closed")
        self.assertEqual(status["market_session"], "WEEKEND")
        self.assertEqual(status["websocket_status"], "DISABLED_OUTSIDE_MARKET_HOURS")
        self.assertEqual(status["reconnect_skipped_reason"], "market_closed")
        self.assertIsNone(status["last_error"])
        self.assertFalse(status["running"])
        self.assertFalse(status["websocket_connected"])
        self.assertEqual(feed.reconnect_count, 0)

    def test_websocket_close_skips_reconnect_after_market(self) -> None:
        reconnect_calls: list[bool] = []

        class ClosingTicker(FakeTicker):
            def reconnect(self) -> None:
                reconnect_calls.append(True)

        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=ClosingTicker, clock=after_market_now)

        feed._on_close(ClosingTicker("k", "t"), 1006, "closed")
        status = feed.status()

        self.assertEqual(reconnect_calls, [])
        self.assertEqual(status["market_session"], "AFTER_MARKET")
        self.assertEqual(status["websocket_status"], "MARKET_CLOSED")
        self.assertEqual(status["reconnect_skipped_reason"], "market_closed")
        self.assertIsNone(status["last_error"])

    def test_websocket_error_after_market_is_not_reported_as_failure(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=after_market_now)

        feed._on_error(FakeTicker("k", "t"), 1006, "connection was closed uncleanly (None)")
        status = feed.status()

        self.assertEqual(status["market_session"], "AFTER_MARKET")
        self.assertEqual(status["websocket_status"], "MARKET_CLOSED")
        self.assertEqual(status["reconnect_skipped_reason"], "market_closed")
        self.assertIsNone(status["last_error"])
        self.assertFalse(status["websocket_connected"])

    def test_websocket_error_during_market_requests_reconnect(self) -> None:
        reconnect_calls: list[bool] = []

        class ErrorTicker(FakeTicker):
            def reconnect(self) -> None:
                reconnect_calls.append(True)

        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=ErrorTicker, clock=regular_market_now)

        feed._on_error(ErrorTicker("k", "t"), 1006, "connection was closed uncleanly (None)")
        status = feed.status()

        self.assertEqual(reconnect_calls, [True])
        self.assertEqual(status["market_session"], "REGULAR_MARKET")
        self.assertEqual(status["websocket_status"], "RECONNECTING")
        self.assertEqual(status["last_error"], "connection was closed uncleanly (None)")
        self.assertTrue(status["data_gap"]["data_gap_detected"])
        self.assertFalse(status["websocket_connected"])

    def test_websocket_auth_failure_does_not_reconnect(self) -> None:
        reconnect_calls: list[bool] = []

        class ErrorTicker(FakeTicker):
            def reconnect(self) -> None:
                reconnect_calls.append(True)

        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=ErrorTicker, clock=regular_market_now)

        feed._on_error(ErrorTicker("k", "t"), 403, "Forbidden invalid token")
        status = feed.status()

        self.assertEqual(reconnect_calls, [])
        self.assertEqual(status["websocket_status"], "AUTH_FAILED")
        self.assertTrue(status["relogin_required"])
        self.assertEqual(status["reconnect_skipped_reason"], "auth_failed")
        self.assertEqual(status["last_error_code"], 403)
        self.assertFalse(status["running"])

    def test_websocket_reconnect_uses_local_backoff(self) -> None:
        reconnect_calls: list[bool] = []

        class ErrorTicker(FakeTicker):
            def reconnect(self) -> None:
                reconnect_calls.append(True)

        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=ErrorTicker, clock=regular_market_now)
        ticker = ErrorTicker("k", "t")

        feed._on_error(ticker, 1006, "connection was closed uncleanly (None)")
        feed._on_error(ticker, 1006, "connection was closed uncleanly (None)")
        status = feed.status()

        self.assertEqual(reconnect_calls, [True])
        self.assertEqual(status["reconnect_request_count"], 1)
        self.assertEqual(status["reconnect_skipped_reason"], "reconnect_backoff")
        self.assertEqual(status["websocket_status"], "RECONNECT_COOLDOWN")

    def test_noreconnect_does_not_mark_exhausted_outside_market(self) -> None:
        feed = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker, clock=weekend_now)

        feed._on_noreconnect(FakeTicker("k", "t"))
        status = feed.status()

        self.assertEqual(status["websocket_status"], "DISABLED_OUTSIDE_MARKET_HOURS")
        self.assertEqual(status["reconnect_skipped_reason"], "market_closed")
        self.assertIsNone(status["last_error"])

    def test_active_feed_falls_back_to_polling_in_paper_mode(self) -> None:
        ws = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker)
        active = ActiveTradePriceFeed(ws)

        tick = active.latest_price(
            provider=FakeProvider(),
            exchange="NFO",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            instrument_token=123,
            mode="paper",
        )

        self.assertIsNotNone(tick)
        self.assertEqual(tick.source, "kite_polling")
        self.assertTrue(active.fallback_active)

    def test_live_mode_uses_marked_polling_fallback_during_websocket_gap(self) -> None:
        ws = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker)
        active = ActiveTradePriceFeed(ws)

        tick = active.latest_price(
            provider=FakeProvider(),
            exchange="NFO",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            instrument_token=123,
            mode="live",
        )

        self.assertIsNotNone(tick)
        self.assertEqual(tick.source, "kite_polling_after_websocket_gap")
        self.assertTrue(active.fallback_active)

    def test_live_mode_blocks_on_stale_websocket_data(self) -> None:
        object.__setattr__(settings, "websocket_live_gap_polling_fallback", False)
        ws = KiteWebSocketPriceFeed(api_key="k", access_token="t", ticker_factory=FakeTicker)
        active = ActiveTradePriceFeed(ws)

        tick = active.latest_price(
            provider=FakeProvider(),
            exchange="NFO",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            instrument_token=123,
            mode="live",
        )

        self.assertIsNone(tick)
        self.assertEqual(active.last_reason, "websocket_disconnected")

    def test_trade_exit_service_uses_websocket_price_when_available(self) -> None:
        object.__setattr__(settings, "enable_underlying_invalidation_exit", False)
        object.__setattr__(settings, "enable_premium_invalidation_exit", False)
        object.__setattr__(settings, "live_auto_squareoff", True)
        object.__setattr__(settings, "option_time_stop_minutes", 0)
        object.__setattr__(settings, "exit_open_trades_before_close_minutes", 0)
        repo = TradeRepository()
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=123,
            entry_price=100,
            stop_loss=90,
            target_1=110,
            target_2=120,
            target_3=130,
            quantity=15,
            score=90,
        )
        record = repo.create_trade(signal, mode="paper", status="filled", requested_quantity=15, placed_quantity=15)
        active = FakeActiveFeed(price=111)
        service = TradeExitService(
            trade_repository=repo,
            kite_provider_factory=lambda: FakeProvider(),
            paper_trading_service=PaperTradingService(),
            active_price_feed=active,
        )

        result = service.evaluate_once(limit=10)

        target_result = next(item for item in result["results"] if item.get("trade_id") == record.id)
        self.assertEqual(target_result["outcome"], "target_1")
        self.assertEqual(target_result["price_source"], "kite_websocket")
        self.assertTrue(any(123 in tokens for tokens in active.subscriptions))

    def test_trade_exit_reports_live_stale_reason(self) -> None:
        repo = TradeRepository()
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=123,
            entry_price=100,
            stop_loss=90,
            target_1=110,
            quantity=15,
            score=90,
        )
        repo.create_trade(signal, mode="live", status="filled", requested_quantity=15, placed_quantity=15)
        active = FakeActiveFeed(price=None, reason="tick_stale")
        service = TradeExitService(
            trade_repository=repo,
            kite_provider_factory=lambda: FakeProvider(),
            paper_trading_service=PaperTradingService(),
            active_price_feed=active,
        )

        result = service.evaluate_once(limit=10)

        self.assertEqual(result["results"][0]["reason"], "tick_stale")

    def test_live_exit_waits_for_broker_confirmation_and_position_zero(self) -> None:
        object.__setattr__(settings, "enable_underlying_invalidation_exit", False)
        object.__setattr__(settings, "enable_premium_invalidation_exit", False)
        object.__setattr__(settings, "live_auto_squareoff", True)
        repo = TradeRepository()
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=123,
            entry_price=100,
            stop_loss=90,
            target_1=110,
            quantity=15,
            score=90,
        )
        repo.create_trade(signal, mode="live", status="filled", requested_quantity=15, placed_quantity=15)
        provider = FakeProvider(order_status="OPEN", position_quantity=15)
        service = TradeExitService(
            trade_repository=repo,
            kite_provider_factory=lambda: provider,
            paper_trading_service=PaperTradingService(),
            active_price_feed=FakeActiveFeed(price=111),
        )

        result = service.evaluate_once(limit=10)
        trades = repo.list_trades(limit=1)

        self.assertEqual(result["closed"], 0)
        self.assertEqual(result["results"][0]["status"], "closing")
        self.assertEqual(trades[0].status, "closing")
        self.assertEqual(trades[0].exit_order_id, "exit-1")

    def test_closing_live_trade_does_not_submit_duplicate_squareoff(self) -> None:
        object.__setattr__(settings, "enable_underlying_invalidation_exit", False)
        object.__setattr__(settings, "enable_premium_invalidation_exit", False)
        repo = TradeRepository()
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=123,
            entry_price=100,
            stop_loss=90,
            target_1=110,
            quantity=15,
            score=90,
        )
        record = repo.create_trade(signal, mode="live", status="filled", requested_quantity=15, placed_quantity=15)
        repo.mark_closing(
            int(record.id),
            outcome="target_1",
            exit_price=111,
            exit_order_id="exit-1",
            exit_order_response={"status": "submitted", "order_id": "exit-1"},
        )
        provider = FakeProvider(order_status="COMPLETE", position_quantity=0)
        service = TradeExitService(
            trade_repository=repo,
            kite_provider_factory=lambda: provider,
            paper_trading_service=PaperTradingService(),
            active_price_feed=FakeActiveFeed(price=111),
        )

        result = service.evaluate_once(limit=10)

        self.assertEqual(provider.place_order_count, 0)
        self.assertEqual(result["closed"], 1)
        self.assertEqual(result["results"][0]["closed"], True)


if __name__ == "__main__":
    unittest.main()
