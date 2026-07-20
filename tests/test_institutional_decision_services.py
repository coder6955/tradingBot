import unittest
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.services.market_regime_service import MarketRegimeService
from app.services.momentum_phase_service import MomentumPhaseService
from app.services.multi_timeframe_context_service import MultiTimeframeContextService
from app.services.setup_family_classifier_service import SetupFamilyClassifierService
from app.services.trade_candidate_ranking_service import TradeCandidateRankingService
from app.services.trade_setup_service import OptionContract, TradeSetupService
from app.services.evidence_matrix_service import EvidenceMatrixService
from app.services.strategy_promotion_service import StrategyPromotionService
from app.services.trade_exit_service import TradeExitService


def candles(step: float, count: int = 30):
    rows = []
    price = 58000.0
    for index in range(count):
        close = price + step * index
        rows.append(SimpleNamespace(open_price=close - step / 2, high_price=close + 8, low_price=close - 8, close_price=close, volume=1000 + index))
    return rows


class InstitutionalDecisionServiceTests(unittest.TestCase):
    def test_multi_timeframe_service_keeps_timeframe_responsibilities_separate(self) -> None:
        service = MultiTimeframeContextService()
        with patch.object(service, "_load", side_effect=lambda symbol, aliases, limit: candles(8.0)):
            result = service.evaluate(symbol="BANKNIFTY", trend="bullish", snapshot={"trend_bullish": True})

        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["available_timeframes"], 5)
        self.assertEqual(result["responsibilities"]["1minute"], "entry_trigger")
        self.assertEqual(result["responsibilities"]["30minute"], "structural_bias")
        self.assertEqual(result["responsibilities"]["tick"], "execution_and_fill_confirmation")

    def test_hierarchical_market_state_exposes_dimensions_and_invalidation(self) -> None:
        result = MarketRegimeService().evaluate(
            symbol="BANKNIFTY",
            trend="bullish",
            side="BUY",
            nifty={"trend_bullish": True},
            banknifty={"trend_bullish": True},
            vix={"price": 14},
            snapshot={"trend_bullish": True, "volume_confirmed": True},
            multi_timeframe={"available_timeframes": 5, "alignment_score": 82},
            volatility_eval={"score": 72, "classification": "expansion_supported"},
            premium_eval={"score": 78, "details": {"breakout": True, "spread_pct": 0.8}},
            price_action_eval={"score": 80, "details": {}},
        )

        self.assertEqual(result["regime"], "trend_expansion")
        self.assertTrue(result["option_buying_suitable"])
        self.assertEqual(set(result["dimensions"]), {"structure", "volatility", "participation", "location", "execution"})
        self.assertTrue(result["invalidation"])

    def test_momentum_phase_rejects_exhaustion_instead_of_chasing(self) -> None:
        result = MomentumPhaseService().evaluate(
            trend="bullish",
            snapshot={"rsi": 84, "adx": 34, "volume_confirmed": True},
            premium_eval={"score": 85, "details": {"breakout": True, "volume_expansion": True, "premium_change_pct": 12}},
            price_action={"details": {}},
            banknifty_eval={"details": {"openingRangeStatus": {"status": "breakout"}}},
            multi_timeframe={"alignment_score": 85, "regime": "trend_expansion"},
            volatility_eval={"score": 70, "classification": "expansion_supported"},
        )

        self.assertEqual(result["phase"], "exhaustion")
        self.assertFalse(result["suitable_for_entry"])
        self.assertEqual(result["abstention_code"], "MOMENTUM_EXHAUSTED")

    def test_setup_family_policy_is_regime_specific(self) -> None:
        result = SetupFamilyClassifierService().classify(
            symbol="BANKNIFTY",
            trend="bullish",
            side="BUY",
            snapshot={"price": 58100, "vwap": 58000},
            factor_scores={
                "market_regime": {"regime": "trend_expansion", "confidence": 0.8},
                "multi_timeframe": {"alignment_score": 80},
                "momentum_phase": {"phase": "confirmation"},
                "banknifty_intelligence": {"details": {"openingRangeStatus": {"status": "breakout"}, "dteMode": {"risk": "normal"}}},
                "option_premium_confirmation": {"score": 85, "details": {"breakout": True, "volume_expansion": True}},
            },
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["name"], "opening_breakout_continuation")
        self.assertEqual(result["exit_profile"]["target_style"], "scale_on_expansion")

    def test_contract_and_candidate_ranking_use_executable_costs(self) -> None:
        good = OptionContract("BANKNIFTYGOODCE", "NFO", 1, "BANKNIFTY", (datetime.now() + timedelta(days=5)).date().isoformat(), 58000, "CE", 15, 100, 50000, 5000, 99.5, 100, bid_quantity=100, ask_quantity=100, delta=0.52, theta=-2)
        wide = OptionContract("BANKNIFTYWIDECE", "NFO", 2, "BANKNIFTY", good.expiry, 58100, "CE", 15, 100, 50000, 5000, 92, 108, bid_quantity=2, ask_quantity=2, delta=0.20, theta=-15)
        ranked = TradeSetupService().rank_contracts(candidates=[wide, good], spot_price=58030, target_strike=58000)
        self.assertEqual(ranked[0]["contract"].tradingsymbol, "BANKNIFTYGOODCE")

        candidate = TradeCandidateRankingService().evaluate(
            contract=good,
            prices={"entry_price": 100, "stop_loss": 80, "target_1": 130},
            combined_score=84,
            market_state={"confidence": 0.8, "uncertainty": 0.1},
            setup_family={"policy_score": 82},
            momentum_phase={"score": 80},
            volatility_edge={"score": 70},
        )
        self.assertTrue(candidate["eligible"])
        self.assertIsNone(candidate["expected_net_value"])
        self.assertEqual(candidate["expectancy_source"], "unavailable_until_outcome_calibration")

    def test_evidence_matrix_does_not_mix_rejections_into_expectancy(self) -> None:
        service = EvidenceMatrixService()
        accepted = [
            {"setup_family": "opening_breakout_continuation", "market_regime": "trend_expansion", "direction": "CALL", "pnl": 120},
            {"setup_family": "opening_breakout_continuation", "market_regime": "trend_expansion", "direction": "CALL", "pnl": -40},
        ]
        rejected = [
            {"setup_family": "opening_breakout_continuation", "market_regime": "trend_expansion", "direction": "CALL"}
            for _ in range(10)
        ]
        with patch.object(service, "_accepted", return_value=accepted), patch.object(service, "_rejected", return_value=rejected):
            report = service.report(group_by=["setup_family", "market_regime", "direction"])

        row = report["groups"][0]
        self.assertEqual(row["trades"], 2)
        self.assertEqual(row["rejections"], 10)
        self.assertEqual(row["expectancy_per_trade"], 40)

    def test_promotion_service_is_advisory_and_never_self_modifies(self) -> None:
        evidence = SimpleNamespace(report=lambda **kwargs: {"groups": [], "accepted_outcomes": 0})
        validations = SimpleNamespace(recent=lambda limit=100: [])
        result = StrategyPromotionService(evidence=evidence, validations=validations).evaluate()

        self.assertFalse(result["promotion_eligible"])
        self.assertFalse(result["automatic_live_mutation"])
        self.assertTrue(result["manual_registration_required"])

    def test_exit_service_uses_setup_family_exit_profile(self) -> None:
        service = TradeExitService.__new__(TradeExitService)
        factors = {"setup_family": {"exit_profile": {"time_stop_minutes": 7, "trail_after_r": 1.2, "target_style": "gamma_scalp"}}}
        trade = SimpleNamespace(
            side="BUY",
            average_price=100,
            entry_price=100,
            stop_loss=80,
            target_1=140,
            tradingsymbol="BANKNIFTYCE",
            created_at=datetime.now(),
            order_response_json=json.dumps({"signal_factor_scores": factors}),
        )
        with patch.object(service, "_high_since_entry", return_value=125):
            outcome = service._trailing_exit_outcome(trade, 100.5)

        self.assertEqual(service._exit_profile(trade)["time_stop_minutes"], 7)
        self.assertEqual(outcome, "trailing_stop")


if __name__ == "__main__":
    unittest.main()
