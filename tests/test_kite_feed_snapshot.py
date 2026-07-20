import os
import tempfile
import unittest
from datetime import datetime, time, timedelta

from app.services.database import Candle, get_session, init_db
from app.services.time_utils import ist_now_naive
from app.providers.kite_feed import KiteFeed
from app.config import settings


class HistoricalBlockingClient:
    def __init__(self) -> None:
        self.historical_calls = 0

    def quote(self, instruments):
        return {
            instruments[0]: {
                "last_price": 58100,
                "instrument_token": 260105,
                "volume": 1000,
                "ohlc": {"open": 58000, "high": 58200, "low": 57900, "close": 58050},
            }
        }

    def historical_data(self, *args, **kwargs):
        self.historical_calls += 1
        raise AssertionError("historical_data should not be called when stored candles are available")


class InstrumentClient:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls = 0

    def instruments(self, exchange):
        self.calls += 1
        if self.should_fail:
            raise AssertionError("instrument master should be loaded from persistent cache")
        return [{"tradingsymbol": "BANKNIFTY26JUL58000CE", "exchange": exchange, "instrument_token": 580001}]


class KiteFeedSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        try:
            os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_snapshot_uses_stored_candles_before_kite_historical_data(self) -> None:
        now = ist_now_naive().replace(second=0, microsecond=0)
        session_day = now.date() - timedelta(days=1)
        while session_day.weekday() >= 5:
            session_day -= timedelta(days=1)
        session_end = datetime.combine(session_day, time(12, 0))
        session = get_session()
        try:
            for index in range(30):
                price = 58000 + index
                session.add(
                    Candle(
                        symbol="BANKNIFTY",
                        timeframe="5minute",
                        timestamp=session_end - timedelta(minutes=(30 - index) * 5),
                        open_price=price - 5,
                        high_price=price + 10,
                        low_price=price - 10,
                        close_price=price,
                        volume=1000 + index,
                    )
                )
            session.commit()
        finally:
            session.close()

        client = HistoricalBlockingClient()
        feed = KiteFeed()
        feed.client = client

        snapshot = feed.get_snapshot("BANKNIFTY")

        self.assertEqual(snapshot["source"], "stored_candles")
        self.assertTrue(snapshot["is_real_data"])
        self.assertEqual(client.historical_calls, 0)

    def test_snapshot_does_not_call_kite_historical_when_fallback_disabled(self) -> None:
        original = settings.kite_snapshot_historical_fallback_enabled
        client = HistoricalBlockingClient()
        try:
            object.__setattr__(settings, "kite_snapshot_historical_fallback_enabled", False)
            feed = KiteFeed()
            feed.client = client

            snapshot = feed.get_snapshot("BANKNIFTY")
        finally:
            object.__setattr__(settings, "kite_snapshot_historical_fallback_enabled", original)

        self.assertEqual(snapshot["source"], "kite")
        self.assertTrue(snapshot["is_real_data"])
        self.assertEqual(client.historical_calls, 0)

    def test_instruments_use_persistent_cache_after_restart(self) -> None:
        original_file = settings.kite_instrument_cache_file
        cache_file = f"{self.temp_db.name}.instruments.json"
        try:
            object.__setattr__(settings, "kite_instrument_cache_file", cache_file)
            first_client = InstrumentClient()
            first_feed = KiteFeed()
            first_feed.client = first_client
            first = first_feed.get_instruments("NFO")

            second_client = InstrumentClient(should_fail=True)
            second_feed = KiteFeed()
            second_feed.client = second_client
            second = second_feed.get_instruments("NFO")
        finally:
            object.__setattr__(settings, "kite_instrument_cache_file", original_file)
            try:
                os.remove(cache_file)
            except OSError:
                pass

        self.assertEqual(first, second)
        self.assertEqual(first_client.calls, 1)
        self.assertEqual(second_client.calls, 0)


if __name__ == "__main__":
    unittest.main()
