import unittest

from app.services.scoring_service import ScoringService
from app.services.signal_service import SignalService


class SignalEngineTests(unittest.TestCase):
    def test_scoring_service_requires_high_score(self) -> None:
        service = ScoringService()
        with self.assertRaises(ValueError):
            service.score_signal("NIFTY", 0.8, 70)

    def test_signal_service_generates_recommendation(self) -> None:
        signal_service = SignalService()
        signal = signal_service.generate_signal(
            symbol="NIFTY",
            score=88,
            confidence=0.86,
            trend="bullish",
            market_context="strong",
        )
        self.assertEqual(signal.symbol, "NIFTY")
        self.assertEqual(signal.action, "BUY_CE")
        self.assertGreater(signal.score, 80)
        self.assertGreater(signal.confidence, 0.8)

    def test_ranking_only_scanner_path_can_generate_below_legacy_score_threshold(
        self,
    ) -> None:
        signal_service = SignalService()
        with self.assertRaises(ValueError):
            signal_service.generate_signal(
                symbol="BANKNIFTY",
                score=55,
                confidence=0.55,
                trend="bullish",
                market_context="neutral",
            )

        signal = signal_service.generate_signal(
            symbol="BANKNIFTY",
            score=55,
            confidence=0.55,
            trend="bullish",
            market_context="neutral",
            enforce_score_threshold=False,
        )

        self.assertEqual(signal.score, 55)
        self.assertIn("ranking only", signal.explanation)


if __name__ == "__main__":
    unittest.main()
