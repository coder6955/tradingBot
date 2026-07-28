import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.services.auto_trader_service import AutoTraderService
from app.services.completed_structure_service import classify_completed_structure
from app.services.entry_opportunity_service import EntryOpportunityService
from app.services.multi_timeframe_context_service import MultiTimeframeContextService
from app.services.trade_exit_service import TradeExitService
from app.services.trade_setup_service import TradeSetupService


def _candles(count: int, *, timeframe_minutes: int, step: float = 20.0):
    start = datetime(2026, 7, 28, 9, 15)
    rows = []
    for index in range(count):
        close = 58000 + index * step
        rows.append(
            SimpleNamespace(
                open_price=close - step * 0.4,
                high_price=close + 5,
                low_price=close - 5,
                close_price=close,
                volume=1000 + index * 100,
                timestamp=start + timedelta(minutes=index * timeframe_minutes),
            )
        )
    return rows


class V6CoreStrategyTests(unittest.TestCase):
    def test_structure_requires_distributed_impulse_and_reports_acceptance(self):
        spike = classify_completed_structure([58000, 58000, 58000, 58000, 58000, 58400])
        trend = classify_completed_structure(
            [58000, 58020, 58045, 58070, 58095, 58125],
            opens=[57995, 58005, 58025, 58050, 58075, 58100],
            highs=[58005, 58025, 58050, 58075, 58100, 58130],
            lows=[57990, 58000, 58020, 58045, 58070, 58095],
        )
        self.assertEqual(spike["direction"], "neutral")
        self.assertEqual(trend["direction"], "bullish")
        self.assertIn(trend["phase"], {"impulse", "pullback_continuation", "breakout_acceptance"})
        self.assertGreaterEqual(trend["bullish_evidence_votes"], 2)

    def test_opening_policy_is_ready_before_0945_without_weakening_normal_session(self):
        service = MultiTimeframeContextService()
        opening = service.evaluate(
            symbol="BANKNIFTY",
            trend="bullish",
            as_of=datetime(2026, 7, 28, 9, 35),
            candle_sets={"1minute": _candles(5, timeframe_minutes=1), "5minute": _candles(3, timeframe_minutes=5)},
        )
        normal = service.evaluate(
            symbol="BANKNIFTY",
            trend="bullish",
            as_of=datetime(2026, 7, 28, 10, 0),
            candle_sets={"1minute": _candles(5, timeframe_minutes=1), "5minute": _candles(3, timeframe_minutes=5)},
        )
        self.assertTrue(opening["passed"], opening)
        self.assertTrue(opening["opening_session"])
        self.assertFalse(normal["passed"])
        self.assertEqual(normal["available_timeframes"], 0)

    def test_nearby_level_is_context_not_veto_after_breakout_acceptance(self):
        common = dict(
            current=102,
            trigger=101.5,
            base=99,
            stop=90,
            target=130,
            spread_pct=0.5,
            observations=[99, 100, 101, 102],
            volatility_scale=2.0,
            expected_move_coverage=0.5,
            room_to_level_pct=0.1,
        )
        rejected = EntryOpportunityService().evaluate(**common, breakout_accepted=False)
        accepted = EntryOpportunityService().evaluate(**common, breakout_accepted=True)
        self.assertFalse(rejected["passed"])
        self.assertTrue(accepted["passed"])
        self.assertIn("nearest_level_room_too_small", accepted["warnings"])
        self.assertNotIn("nearest_level_room_too_small", accepted["blockers"])

    def test_expiry_day_buying_uses_next_listed_expiry(self):
        today = datetime.now().date()
        next_expiry = today + timedelta(days=7)
        instruments = [
            {
                "tradingsymbol": f"BANKNIFTY{suffix}58000CE",
                "exchange": "NFO",
                "name": "BANKNIFTY",
                "expiry": expiry.isoformat(),
                "strike": 58000,
                "instrument_type": "CE",
                "instrument_token": token,
                "lot_size": 15,
            }
            for suffix, expiry, token in (("TODAY", today, 1), ("NEXT", next_expiry, 2))
        ]
        selected = TradeSetupService().select_contract(
            instruments,
            "BANKNIFTY",
            58020,
            "bullish",
            quotes={
                f"NFO:BANKNIFTYNEXT58000CE": {
                    "last_price": 100,
                    "volume": 5000,
                    "oi": 50000,
                    "depth": {"buy": [{"price": 99, "quantity": 100}], "sell": [{"price": 100, "quantity": 100}]},
                }
            },
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.instrument_token, 2)

    def test_fast_validation_promotes_precomputed_plan_outside_io_budget(self):
        calls = []

        class Context:
            def validate_candidate(self, event):
                return {
                    "passed": True,
                    "action": "promote_precomputed_plan_to_armed_entry",
                    "plan": {"order_mode": "paper", "contract": {"instrument_token": 1}},
                }

        class Promoter:
            def register_from_fast_plan(self, plan):
                calls.append(plan)
                return {"registered": True, "setup_id": "armed-v6"}

        service = AutoTraderService(
            lambda: object(),
            lambda: object(),
            fast_scan_context_service=Context(),
            fast_candidate_promoter=Promoter(),
        )
        service._run_fast_candidate_validation({"direction": "bullish", "timestamp": datetime.now().isoformat()})
        self.assertEqual(len(calls), 1)
        self.assertTrue(service.last_fast_candidate_decision["passed"])
        self.assertEqual(service.last_fast_candidate_decision["stage"], "fast_candidate_promoted_to_armed_entry")
        self.assertEqual(service.last_fast_candidate_decision["io_calls"], {"rest_calls": 0, "database_queries": 0})

    def test_partial_runner_uses_whole_lots_once_and_high_watermark_trail(self):
        service = TradeExitService.__new__(TradeExitService)
        factors = {
            "contract": {"lot_size": 15},
            "setup_family": {
                "exit_profile": {
                    "partial_at_r": 1.0,
                    "trail_after_r": 1.0,
                    "target_style": "runner",
                }
            },
        }
        trade = SimpleNamespace(
            side="BUY",
            average_price=100,
            entry_price=100,
            stop_loss=90,
            target_1=120,
            target_2=140,
            target_3=160,
            remaining_quantity=30,
            filled_quantity=30,
            placed_quantity=30,
            requested_quantity=30,
            partial_exit_json=None,
            order_response_json=json.dumps({"signal_factor_scores": factors}),
            tradingsymbol="BANKNIFTY_TEST_CE",
            created_at=datetime.now() - timedelta(minutes=5),
            highest_price_during_trade=125,
        )
        self.assertTrue(service._can_partial_at_r(trade, 110))
        trade.partial_exit_json = json.dumps([{"quantity": 15, "outcome": "partial_target_1"}])
        trade.remaining_quantity = 15
        self.assertFalse(service._can_partial_at_r(trade, 112))
        with patch.object(service, "_high_since_entry", return_value=125), patch.object(service, "_premium_atr", return_value=4):
            self.assertEqual(service._trailing_exit_outcome(trade, 116), "trailing_stop")

    def test_time_stop_is_extended_only_for_trend_runner_profile(self):
        service = TradeExitService.__new__(TradeExitService)
        base = {
            "contract": {"lot_size": 15},
            "setup_family": {"exit_profile": {"time_stop_minutes": 12, "target_style": "runner"}},
        }
        trade = SimpleNamespace(
            side="BUY",
            average_price=100,
            entry_price=100,
            created_at=datetime.now() - timedelta(minutes=18),
            order_response_json=json.dumps({"signal_factor_scores": base}),
        )
        with patch("app.services.trade_exit_service.ist_now", return_value=datetime.now().astimezone()):
            self.assertIsNone(service._time_exit_outcome(trade, 101))
        base["setup_family"]["exit_profile"]["target_style"] = "fixed_structure"
        trade.order_response_json = json.dumps({"signal_factor_scores": base})
        with patch("app.services.trade_exit_service.ist_now", return_value=datetime.now().astimezone()):
            self.assertEqual(service._time_exit_outcome(trade, 101), "time_exit")


if __name__ == "__main__":
    unittest.main()
