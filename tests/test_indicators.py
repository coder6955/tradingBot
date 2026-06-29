import unittest

from app.services.indicator_service import (
    compute_ema,
    compute_rsi,
    compute_macd,
    compute_bollinger_bands,
    compute_supertrend,
)


class IndicatorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prices = [10, 11, 10.5, 12, 13, 14, 13.5, 15, 16, 17]
        self.highs = [10.5, 11.5, 11.0, 12.2, 13.5, 14.5, 14.0, 15.5, 16.5, 17.5]
        self.lows = [9.5, 10.2, 10.0, 11.0, 12.2, 13.0, 12.8, 14.2, 15.0, 16.0]
        self.volumes = [100, 110, 120, 130, 140, 150, 160, 170, 180, 190]

    def test_compute_ema_returns_expected_length(self) -> None:
        values = compute_ema(self.prices, 3)
        self.assertEqual(len(values), len(self.prices))
        self.assertGreater(values[-1], 0)

    def test_compute_rsi_returns_values_between_zero_and_hundred(self) -> None:
        values = compute_rsi(self.prices, 3)
        self.assertEqual(len(values), len(self.prices))
        self.assertTrue(all(0 <= value <= 100 for value in values))

    def test_compute_macd_returns_expected_series(self) -> None:
        macd, signal = compute_macd(self.prices, 12, 26, 9)
        self.assertEqual(len(macd), len(self.prices))
        self.assertEqual(len(signal), len(self.prices))

    def test_compute_bollinger_bands_returns_three_series(self) -> None:
        upper, middle, lower = compute_bollinger_bands(self.prices, 3)
        self.assertEqual(len(upper), len(self.prices))
        self.assertEqual(len(middle), len(self.prices))
        self.assertEqual(len(lower), len(self.prices))

    def test_compute_supertrend_returns_series(self) -> None:
        values = compute_supertrend(self.highs, self.lows, self.prices, 3, 2.0)
        self.assertEqual(len(values), len(self.prices))
        self.assertTrue(all(isinstance(value, (int, float)) for value in values))


if __name__ == "__main__":
    unittest.main()
