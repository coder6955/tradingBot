import unittest

from app.services.price_action_service import PriceActionService


class PriceActionServiceTests(unittest.TestCase):
    def test_bullish_price_action_scores_alignment(self) -> None:
        service = PriceActionService()
        snapshot = {
            "price": 105.0,
            "ema_alignment": True,
            "vwap": 103.0,
            "macd_positive": True,
            "volume_confirmed": True,
            "rsi": 58,
            "previous_day_high": 110.0,
            "previous_day_low": 98.0,
            "previous_day_close": 102.0,
            "day_high": 106.0,
            "day_low": 101.0,
        }

        result = service.evaluate(snapshot, "bullish", "BUY")

        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["score"], 55)

    def test_directional_buy_fails_when_room_to_level_is_too_small(self) -> None:
        service = PriceActionService()
        snapshot = {
            "price": 796.0,
            "ema_alignment": False,
            "vwap": 799.0,
            "macd_positive": False,
            "volume_confirmed": True,
            "rsi": 44,
            "previous_day_high": 804.45,
            "previous_day_low": 794.75,
            "previous_day_close": 796.0,
            "day_high": 805.9,
            "day_low": 793.3,
        }

        result = service.evaluate(snapshot, "bearish", "BUY")

        self.assertFalse(result["passed"])
        self.assertIn("directional option buying has insufficient room to nearest level", result["reasons"])


if __name__ == "__main__":
    unittest.main()
