import unittest

from app.services.indicator_scoring_service import IndicatorScoringService


class IndicatorScoringServiceTests(unittest.TestCase):
    def test_score_symbol_uses_indicator_values(self) -> None:
        service = IndicatorScoringService()
        score = service.score_symbol(
            rsi=58,
            adx=25,
            macd_positive=True,
            ema_alignment=True,
            vwap_above_price=False,
            volume_confirmed=True,
            trend_bullish=True,
            market_context="strong",
        )
        self.assertGreaterEqual(score, 70)


if __name__ == "__main__":
    unittest.main()
