import unittest

from app.services.market_regime_service import MarketRegimeService


class MarketRegimeServiceTests(unittest.TestCase):
    def test_aligned_low_vix_regime_passes(self) -> None:
        service = MarketRegimeService()

        result = service.evaluate(
            symbol="HDFCBANK",
            trend="bullish",
            side="BUY",
            nifty={"trend_bullish": True},
            banknifty={"trend_bullish": True},
            vix={"price": 14.0},
        )

        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["score"], 55)


if __name__ == "__main__":
    unittest.main()
