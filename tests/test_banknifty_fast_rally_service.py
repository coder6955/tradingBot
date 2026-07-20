import unittest
from datetime import datetime, timedelta

from app.config import settings
from app.services.banknifty_fast_rally_service import BankNiftyFastRallyService
from app.services.kite_websocket_price_feed import WebSocketTick


class BankNiftyFastRallyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "fast_rally_window_seconds": settings.fast_rally_window_seconds,
            "fast_rally_trigger_pct": settings.fast_rally_trigger_pct,
        }
        object.__setattr__(settings, "fast_rally_window_seconds", 5.0)
        object.__setattr__(settings, "fast_rally_trigger_pct", 0.08)

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)

    def test_friday_style_acceleration_requests_immediate_bullish_rescan(self) -> None:
        events = []
        service = BankNiftyFastRallyService(events.append)
        service.set_underlying_token(260105)
        start = datetime(2026, 7, 17, 14, 10, 0)

        service.on_tick(WebSocketTick(instrument_token=260105, price=58100, timestamp=start, receive_timestamp=start))
        event = service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58150,
                timestamp=start + timedelta(seconds=3),
                receive_timestamp=start + timedelta(seconds=3),
            )
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["direction"], "bullish")
        self.assertEqual(len(events), 1)

    def test_unrelated_option_tick_does_not_trigger_rally_rescan(self) -> None:
        service = BankNiftyFastRallyService()
        service.set_underlying_token(260105)

        event = service.on_tick(WebSocketTick(instrument_token=580001, price=120, timestamp=datetime(2026, 7, 17, 14, 10)))

        self.assertIsNone(event)
