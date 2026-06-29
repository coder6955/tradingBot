import unittest

from app.services.market_analysis_service import MarketAnalysisService


class MarketAnalysisServiceTests(unittest.TestCase):
    def test_analyze_market_returns_expected_fields(self) -> None:
        service = MarketAnalysisService()
        result = service.analyze_market({"nifty": 22000, "banknifty": 47000, "vix": 15.0})
        self.assertIn("nifty_trend", result)
        self.assertIn("banknifty_trend", result)
        self.assertIn("vix", result)
        self.assertGreaterEqual(result["score"], 0)


if __name__ == "__main__":
    unittest.main()
