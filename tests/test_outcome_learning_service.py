import unittest

from app.services.outcome_learning_service import OutcomeLearningService


class OutcomeLearningServiceTests(unittest.TestCase):
    def test_sample_confidence_requires_50_trades_before_blocking(self) -> None:
        service = OutcomeLearningService()

        self.assertEqual(service._sample_confidence(0), "insufficient")
        self.assertEqual(service._sample_confidence(8), "weak")
        self.assertEqual(service._sample_confidence(50), "moderate")
        self.assertEqual(service._sample_confidence(100), "stronger")
        self.assertEqual(service._sample_confidence(200), "high")


if __name__ == "__main__":
    unittest.main()
