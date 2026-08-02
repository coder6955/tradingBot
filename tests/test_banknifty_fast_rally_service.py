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

        service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58100,
                timestamp=start,
                receive_timestamp=start,
            )
        )
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

        event = service.on_tick(
            WebSocketTick(
                instrument_token=580001,
                price=120,
                timestamp=datetime(2026, 7, 17, 14, 10),
            )
        )

        self.assertIsNone(event)

    def test_status_explains_below_threshold_observation(self) -> None:
        service = BankNiftyFastRallyService()
        service.set_underlying_token(260105)
        start = datetime(2026, 7, 17, 14, 10, 0)

        service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58100,
                timestamp=start,
                receive_timestamp=start,
            )
        )
        event = service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58120,
                timestamp=start + timedelta(seconds=3),
                receive_timestamp=start + timedelta(seconds=3),
            )
        )

        status = service.status()
        self.assertIsNone(event)
        self.assertEqual(status["evaluated_tick_count"], 1)
        self.assertEqual(status["last_observation"]["reason"], "below_threshold")
        self.assertGreater(status["last_observation"]["threshold_progress_pct"], 0)
        self.assertEqual(status["trigger_count"], 0)

    def test_callback_suppression_is_visible_on_trigger_event(self) -> None:
        service = BankNiftyFastRallyService(
            lambda event: {
                "scheduled": False,
                "reason": "fast_rescan_cooldown",
                "event": event["type"],
            }
        )
        service.set_underlying_token(260105)
        start = datetime(2026, 7, 17, 14, 10, 0)

        service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58100,
                timestamp=start,
                receive_timestamp=start,
            )
        )
        event = service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58150,
                timestamp=start + timedelta(seconds=3),
                receive_timestamp=start + timedelta(seconds=3),
            )
        )

        status = service.status()
        self.assertEqual(event["dispatch"]["reason"], "fast_rescan_cooldown")
        self.assertEqual(status["callback_suppressed_count"], 1)
        self.assertEqual(status["suppressed_reasons"], {"fast_rescan_cooldown": 1})

    def test_callback_failure_is_contained_and_observable(self) -> None:
        def fail(_event):
            raise RuntimeError("dispatch unavailable")

        service = BankNiftyFastRallyService(fail)
        service.set_underlying_token(260105)
        start = datetime(2026, 7, 17, 14, 10, 0)

        service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58100,
                timestamp=start,
                receive_timestamp=start,
            )
        )
        event = service.on_tick(
            WebSocketTick(
                instrument_token=260105,
                price=58150,
                timestamp=start + timedelta(seconds=3),
                receive_timestamp=start + timedelta(seconds=3),
            )
        )

        status = service.status()
        self.assertEqual(event["dispatch"]["reason"], "fast_rally_callback_failed")
        self.assertEqual(status["callback_error_count"], 1)
        self.assertEqual(status["last_callback_error"]["error_type"], "RuntimeError")
