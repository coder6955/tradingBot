import unittest

from app.config import settings
from app.services.entry_timing_service import EntryTimingService
from app.services.trade_setup_service import OptionContract


class EntryTimingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "max_entry_chase_pct": settings.max_entry_chase_pct,
            "max_premium_move_from_base_pct": settings.max_premium_move_from_base_pct,
            "min_remaining_risk_reward": settings.min_remaining_risk_reward,
            "min_target1_room_pct": settings.min_target1_room_pct,
            "entry_armed_distance_to_trigger_pct": settings.entry_armed_distance_to_trigger_pct,
            "min_entry_expected_move_coverage": settings.min_entry_expected_move_coverage,
            "min_entry_room_to_level_pct": settings.min_entry_room_to_level_pct,
        }
        object.__setattr__(settings, "max_entry_chase_pct", 1.0)
        object.__setattr__(settings, "max_premium_move_from_base_pct", 4.0)
        object.__setattr__(settings, "min_remaining_risk_reward", 1.3)
        object.__setattr__(settings, "min_target1_room_pct", 8.0)
        object.__setattr__(settings, "entry_armed_distance_to_trigger_pct", 0.75)
        object.__setattr__(settings, "min_entry_expected_move_coverage", 0.90)
        object.__setattr__(settings, "min_entry_room_to_level_pct", 0.25)

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)

    def test_setup_below_trigger_returns_armed_for_entry(self) -> None:
        result = self._evaluate(
            current=101.5,
            trigger=102.0,
            base=100.0,
            target=120.0,
            stop=95.0,
            breakout=False,
        )

        self.assertEqual(result["state"], EntryTimingService.ARMED_FOR_ENTRY)
        self.assertTrue(result["entry_should_wait"])
        self.assertIn("waiting_for_entry_trigger", result["reasons"])

    def test_premium_breakout_with_good_room_returns_enter_now(self) -> None:
        result = self._evaluate(
            current=103.0,
            trigger=102.0,
            base=100.0,
            target=120.0,
            stop=96.0,
            breakout=True,
        )

        self.assertEqual(result["state"], EntryTimingService.ENTER_NOW)
        self.assertTrue(result["passed"])

    def test_premium_spike_far_above_trigger_returns_too_late(self) -> None:
        result = self._evaluate(
            current=110.0,
            trigger=102.0,
            base=100.0,
            target=130.0,
            stop=96.0,
            breakout=True,
        )

        self.assertEqual(result["state"], EntryTimingService.TOO_LATE)
        self.assertIn("chase_risk_high", result["reasons"])

    def test_weak_premium_participation_remains_watching_setup(self) -> None:
        result = self._evaluate(
            current=98.0,
            trigger=102.0,
            base=100.0,
            target=120.0,
            stop=94.0,
            breakout=False,
            premium_change=-2.0,
        )

        self.assertEqual(result["state"], EntryTimingService.WATCHING_SETUP)

    def test_insufficient_room_blocks_enter_now(self) -> None:
        result = self._evaluate(
            current=103.0,
            trigger=102.0,
            base=100.0,
            target=106.0,
            stop=96.0,
            breakout=True,
        )

        self.assertEqual(result["state"], EntryTimingService.TOO_LATE)
        self.assertIn("insufficient_target_room_after_entry", result["reasons"])

    def test_expected_move_too_small_warns_but_does_not_block_enter_now(self) -> None:
        result = self._evaluate(
            current=103.0,
            trigger=102.0,
            base=100.0,
            target=120.0,
            stop=96.0,
            breakout=True,
            expected_coverage=0.5,
        )

        self.assertEqual(result["state"], EntryTimingService.ENTER_NOW)
        self.assertIn("expected_move_coverage_weak", result["opportunity_warnings"])

    def test_spread_widening_after_trigger_blocks_enter_now(self) -> None:
        result = self._evaluate(
            current=103.0,
            trigger=102.0,
            base=100.0,
            target=120.0,
            stop=96.0,
            breakout=True,
            spread_pct=8.0,
        )

        self.assertNotEqual(result["state"], EntryTimingService.ENTER_NOW)
        self.assertIn("entry_spread_too_wide", result["reasons"])

    def test_proxy_quality_and_liquidity_scores_do_not_block_executable_breakout(
        self,
    ) -> None:
        result = self._evaluate(
            current=103.0,
            trigger=102.0,
            base=100.0,
            target=120.0,
            stop=96.0,
            breakout=True,
            option_quality_passed=False,
            liquidity_score=10,
        )

        self.assertEqual(result["state"], EntryTimingService.ENTER_NOW)
        self.assertFalse(result["soft_confirmation_evidence"]["option_quality_passed"])
        self.assertEqual(result["soft_confirmation_evidence"]["liquidity_score"], 10)

    def _evaluate(
        self,
        *,
        current: float,
        trigger: float,
        base: float,
        target: float,
        stop: float,
        breakout: bool,
        premium_change: float = 2.0,
        expected_coverage: float = 1.1,
        spread_pct: float = 1.0,
        option_quality_passed: bool = True,
        liquidity_score: int = 90,
    ) -> dict[str, object]:
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            current,
            100000,
            100000,
            current - 0.5,
            current,
        )
        return EntryTimingService().evaluate(
            contract=contract,
            prices={
                "entry_price": current,
                "stop_loss": stop,
                "target_1": target,
                "risk_reward": 1.5,
            },
            premium_eval={
                "passed": breakout,
                "details": {
                    "last_close": current,
                    "recent_high": trigger,
                    "first_close": base,
                    "option_vwap": base,
                    "breakout": breakout,
                    "participation_confirmed": premium_change > 0,
                    "premium_change_pct": premium_change,
                    "spread_pct": spread_pct,
                },
            },
            data_quality={"passed": True},
            freshness={"passed": True},
            option_quality={"passed": option_quality_passed, "score": 25},
            banknifty_eval={
                "score": 75,
                "passed": True,
                "details": {"expectedMoveCheck": {"coverage": expected_coverage}},
            },
            price_action={
                "score": 75,
                "passed": True,
                "details": {"room_to_level_pct": 0.8},
            },
            liquidity_score=liquidity_score,
            trend="bullish",
        )


if __name__ == "__main__":
    unittest.main()
