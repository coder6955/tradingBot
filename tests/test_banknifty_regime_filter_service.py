import unittest
from datetime import datetime

from app.config import settings
from app.services.banknifty_regime_filter_service import BankNiftyRegimeFilterService
from app.services.trade_setup_service import OptionContract


class BankNiftyRegimeFilterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_banknifty_regime_filter": settings.enable_banknifty_regime_filter,
            "min_banknifty_regime_score": settings.min_banknifty_regime_score,
            "banknifty_significant_gap_pct": settings.banknifty_significant_gap_pct,
            "banknifty_compression_day_range_pct": settings.banknifty_compression_day_range_pct,
            "banknifty_late_trade_cutoff_time": settings.banknifty_late_trade_cutoff_time,
            "banknifty_late_trade_min_premium_score": settings.banknifty_late_trade_min_premium_score,
            "banknifty_expiry_min_premium_score": settings.banknifty_expiry_min_premium_score,
        }
        object.__setattr__(settings, "enable_banknifty_regime_filter", True)
        object.__setattr__(settings, "min_banknifty_regime_score", 65)
        object.__setattr__(settings, "banknifty_significant_gap_pct", 0.35)
        object.__setattr__(settings, "banknifty_compression_day_range_pct", 0.45)
        object.__setattr__(settings, "banknifty_late_trade_cutoff_time", "14:45")
        object.__setattr__(settings, "banknifty_late_trade_min_premium_score", 80)
        object.__setattr__(settings, "banknifty_expiry_min_premium_score", 75)

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)

    def test_favorable_opening_drive_regime_passes(self) -> None:
        result = self._evaluate()

        self.assertTrue(result["passed"])
        self.assertEqual(result["classification"], "OPTION_BUYING_FAVORABLE")
        self.assertEqual(result["hard_reasons"], [])

    def test_opening_trap_blocks_option_buying(self) -> None:
        result = self._evaluate(
            opening={"status": "failed_breakout", "largeWick": True}
        )

        self.assertFalse(result["passed"])
        self.assertIn("opening_trap_structure", result["hard_reasons"])

    def test_compression_without_premium_expansion_blocks(self) -> None:
        result = self._evaluate(
            day_type={
                "passed": False,
                "score": 35,
                "details": {"day_type": "rotation_range", "day_range_pct": 0.28},
            },
            premium={
                "passed": True,
                "score": 78,
                "details": {
                    "last_close": 120,
                    "option_vwap": 115,
                    "breakout": False,
                    "volume_expansion": False,
                },
            },
        )

        self.assertFalse(result["passed"])
        self.assertIn("range_compression_without_expansion", result["hard_reasons"])

    def test_vwap_reclaim_failure_blocks_bullish_option_buy(self) -> None:
        result = self._evaluate(snapshot={"price": 58000, "vwap": 58100})

        self.assertFalse(result["passed"])
        self.assertIn("banknifty_vwap_reclaim_not_confirmed", result["hard_reasons"])

    def test_late_day_decay_environment_blocks_without_strong_premium(self) -> None:
        result = self._evaluate(
            now=datetime(2026, 7, 3, 14, 50),
            premium={
                "passed": True,
                "score": 62,
                "details": {
                    "last_close": 120,
                    "option_vwap": 115,
                    "breakout": True,
                    "volume_expansion": True,
                },
            },
        )

        self.assertFalse(result["passed"])
        self.assertIn("late_day_premium_decay_environment", result["hard_reasons"])

    def test_expiry_day_requires_strong_premium_expansion(self) -> None:
        result = self._evaluate(
            dte={"daysToExpiry": 0, "risk": "near_expiry"},
            premium={
                "passed": True,
                "score": 60,
                "details": {
                    "last_close": 120,
                    "option_vwap": 115,
                    "breakout": True,
                    "volume_expansion": True,
                },
            },
        )

        self.assertFalse(result["passed"])
        self.assertIn(
            "expiry_day_without_strong_premium_expansion", result["hard_reasons"]
        )

    def test_iv_crush_or_overpriced_premium_blocks(self) -> None:
        result = self._evaluate(
            volatility={
                "score": 45,
                "main_risk": "iv_crush",
                "details": {"best_expected_move_coverage": 0.9},
            }
        )

        self.assertFalse(result["passed"])
        self.assertIn("iv_crush", result["hard_reasons"])

    def _evaluate(self, **overrides):
        now = overrides.get("now") or datetime(2026, 7, 3, 10, 30)
        service = BankNiftyRegimeFilterService(clock=lambda: now)
        premium = overrides.get("premium") or {
            "passed": True,
            "score": 85,
            "details": {
                "last_close": 125,
                "option_vwap": 115,
                "breakout": True,
                "volume_expansion": True,
            },
        }
        day_type = overrides.get("day_type") or {
            "passed": True,
            "score": 90,
            "details": {"day_type": "trend_expansion", "day_range_pct": 0.85},
        }
        opening = overrides.get("opening") or {"status": "breakout", "largeWick": False}
        dte = overrides.get("dte") or {"daysToExpiry": 3, "risk": "normal"}
        volatility = overrides.get("volatility") or {
            "score": 82,
            "main_risk": "none",
            "details": {"best_expected_move_coverage": 1.2},
        }
        return service.evaluate(
            symbol="BANKNIFTY",
            trend="bullish",
            snapshot={
                "price": 58200,
                "vwap": 58100,
                "day_open": 58050,
                "previous_day_close": 58000,
                **overrides.get("snapshot", {}),
            },
            contract=OptionContract(
                "BANKNIFTY26JUL58200CE",
                "NFO",
                1,
                "BANKNIFTY",
                "2026-07-26",
                58200,
                "CE",
                15,
                125,
                100000,
                5000,
                124,
                125,
            ),
            prices=overrides.get("prices")
            or {
                "entry_price": 125,
                "stop_loss": 100,
                "target_1": 170,
                "target_2": 190,
                "target_3": 220,
                "risk_reward": 1.8,
            },
            premium_eval=premium,
            day_type_eval=day_type,
            time_bucket_eval=overrides.get("time_bucket")
            or {"passed": True, "reasons": []},
            banknifty_eval={
                "passed": True,
                "score": 88,
                "details": {"openingRangeStatus": opening, "dteMode": dte},
            },
            volatility_eval=volatility,
        )


if __name__ == "__main__":
    unittest.main()
