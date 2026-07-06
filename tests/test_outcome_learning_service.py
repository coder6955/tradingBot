import unittest
from datetime import datetime

from app.services.outcome_learning_service import OutcomeLearningService


class OutcomeLearningServiceTests(unittest.TestCase):
    def test_sample_confidence_requires_50_trades_before_blocking(self) -> None:
        service = OutcomeLearningService()

        self.assertEqual(service._sample_confidence(0), "insufficient")
        self.assertEqual(service._sample_confidence(8), "weak")
        self.assertEqual(service._sample_confidence(50), "moderate")
        self.assertEqual(service._sample_confidence(100), "stronger")
        self.assertEqual(service._sample_confidence(200), "high")

    def test_closed_history_uses_memory_cache_within_ttl(self) -> None:
        now = datetime(2026, 7, 3, 10, 30, 0)
        service = OutcomeLearningService(clock=lambda: now)
        cached = [{"symbol": "BANKNIFTY", "groups": ["BANKNIFTY:BUY_CE"]}]
        service._history_cache = cached
        service._history_cache_at = now

        self.assertEqual(service._closed_history(), cached)


if __name__ == "__main__":
    unittest.main()
