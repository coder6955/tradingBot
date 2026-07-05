import unittest

from app.services.setup_family_classifier_service import SetupFamilyClassifierService


class SetupFamilyClassifierServiceTests(unittest.TestCase):
    def test_classifies_opening_breakout_continuation(self) -> None:
        result = SetupFamilyClassifierService().classify(
            symbol="BANKNIFTY",
            trend="bullish",
            side="BUY",
            snapshot={"price": 58100, "vwap": 58020},
            factor_scores={
                "banknifty_intelligence": {
                    "details": {
                        "openingRangeStatus": {"status": "breakout"},
                        "dteMode": {"risk": "normal"},
                    }
                },
                "option_premium_confirmation": {"score": 80, "details": {"last_close": 105, "option_vwap": 101}},
            },
        )

        self.assertEqual(result["name"], "opening_breakout_continuation")
        self.assertEqual(result["group"], "opening_drive")

    def test_classifies_range_breakout_after_compression(self) -> None:
        result = SetupFamilyClassifierService().classify(
            symbol="BANKNIFTY",
            trend="bullish",
            side="BUY",
            snapshot={"price": 58100, "vwap": 58020},
            factor_scores={
                "banknifty_intelligence": {"details": {"dteMode": {"risk": "normal"}}},
                "banknifty_regime_filter": {
                    "details": {
                        "compression_expansion": {
                            "day_type": "rotation_range",
                        }
                    }
                },
                "option_premium_confirmation": {
                    "score": 80,
                    "details": {"breakout": True, "volume_expansion": True, "last_close": 105, "option_vwap": 101},
                },
            },
        )

        self.assertEqual(result["name"], "range_breakout_after_compression")
        self.assertEqual(result["group"], "compression_expansion")

    def test_classifies_vwap_rejection_for_put(self) -> None:
        result = SetupFamilyClassifierService().classify(
            symbol="BANKNIFTY",
            trend="bearish",
            side="BUY",
            snapshot={"price": 57900, "vwap": 58020},
            factor_scores={
                "banknifty_intelligence": {"details": {"dteMode": {"risk": "normal"}}},
                "option_premium_confirmation": {"score": 72, "details": {"last_close": 112, "option_vwap": 110}},
            },
        )

        self.assertEqual(result["name"], "vwap_rejection_continuation")
        self.assertEqual(result["group"], "vwap_continuation")

    def test_classifies_expiry_scalp_before_other_labels(self) -> None:
        result = SetupFamilyClassifierService().classify(
            symbol="BANKNIFTY",
            trend="bullish",
            side="BUY",
            snapshot={"price": 58100, "vwap": 58020},
            factor_scores={
                "banknifty_intelligence": {
                    "details": {
                        "openingRangeStatus": {"status": "breakout"},
                        "dteMode": {"risk": "near_expiry", "mode": "gamma_scalp"},
                    }
                },
                "option_premium_confirmation": {"score": 82, "details": {"breakout": True}},
            },
        )

        self.assertEqual(result["name"], "expiry_scalp_setup")
        self.assertEqual(result["group"], "expiry")


if __name__ == "__main__":
    unittest.main()
