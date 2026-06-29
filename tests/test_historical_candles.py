import unittest

from app.services.historical_candles_service import HistoricalCandlesService


class HistoricalCandlesServiceTests(unittest.TestCase):
    def test_generate_candles_returns_expected_shape(self) -> None:
        service = HistoricalCandlesService()
        candles = service.generate_candles("NIFTY", 10, 22000.0)
        self.assertEqual(len(candles), 10)
        self.assertIn("close", candles[0])
        self.assertIn("volume", candles[0])


if __name__ == "__main__":
    unittest.main()
