import unittest
from datetime import datetime, timedelta

from app.services.tick_replay_service import TickReplayService


class TickReplayServiceTests(unittest.TestCase):
    def test_replay_orders_ticks_by_receive_timestamp_without_sleeping(self) -> None:
        seen = []
        base = datetime(2026, 7, 17, 14, 10)
        result = TickReplayService(lambda tick: seen.append(tick.price)).replay(
            [
                {
                    "instrument_token": 1,
                    "price": 102,
                    "timestamp": base + timedelta(seconds=2),
                },
                {"instrument_token": 1, "price": 100, "timestamp": base},
                {
                    "instrument_token": 1,
                    "price": 101,
                    "timestamp": base + timedelta(seconds=1),
                },
            ]
        )

        self.assertEqual(result["ticks_replayed"], 3)
        self.assertEqual(seen, [100, 101, 102])
