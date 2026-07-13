import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from app.models import Signal
from app.services.database import Candle, OptionQuoteSnapshot, get_session, init_db
from app.services.execution_analytics_service import ExecutionAnalyticsService
from app.services.opportunity_analytics_service import OpportunityAnalyticsService
from app.services.opportunity_repository import OpportunityRepository
from app.services.professional_insights_service import ProfessionalInsightsService
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.trade_repository import TradeRepository


class ProfessionalAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_opportunity_analytics_segments_closed_signals(self) -> None:
        repo = OpportunityRepository()
        winner = repo.save_opportunity(self._signal("BUY_CE", "BANKNIFTY26JUL58000CE", 100, 80, 140))
        loser = repo.save_opportunity(self._signal("BUY_PE", "BANKNIFTY26JUL57000PE", 100, 80, 140))
        repo.update_outcome(winner.id, outcome="target_1", exit_price=140)
        repo.update_outcome(loser.id, outcome="stop_loss", exit_price=80, failure_tags=["spread_slippage_drag"])

        result = OpportunityAnalyticsService().analyze(symbol="BANKNIFTY", limit=100)

        self.assertEqual(result["overall"]["trades"], 2)
        self.assertEqual(result["overall"]["wins"], 1)
        self.assertEqual(result["segments"]["ce_vs_pe"]["CE"]["wins"], 1)
        self.assertEqual(result["segments"]["ce_vs_pe"]["PE"]["losses"], 1)
        self.assertIn("spread_slippage_drag", result["segments"]["failure_tag"])

    def test_execution_analytics_separates_paper_and_live(self) -> None:
        repo = TradeRepository()
        paper = repo.create_trade(
            self._signal("BUY_CE", "BANKNIFTY26JUL58000CE", 100, 80, 140),
            mode="paper",
            status="filled",
            requested_quantity=15,
            placed_quantity=15,
        )
        live = repo.create_trade(
            self._signal("BUY_PE", "BANKNIFTY26JUL57000PE", 100, 80, 140),
            mode="live",
            status="filled",
            requested_quantity=30,
            placed_quantity=15,
        )
        repo.update_broker_status(live.id, status="filled", broker_payload={"status": "COMPLETE"}, filled_quantity=15, average_price=101)
        repo.close_trade(paper.id, outcome="target_1", exit_price=140)
        repo.close_trade(live.id, outcome="stop_loss", exit_price=80)

        result = ExecutionAnalyticsService().analyze(symbol="BANKNIFTY", limit=100)

        self.assertEqual(result["sample"]["paper"], 1)
        self.assertEqual(result["sample"]["live"], 1)
        self.assertEqual(result["segments"]["mode"]["paper"]["wins"], 1)
        self.assertEqual(result["segments"]["mode"]["live"]["losses"], 1)
        self.assertEqual(result["execution_quality"]["avg_requested_fill_ratio_pct"], 50.0)

    def test_professional_insights_compare_accepted_and_rejected_setups(self) -> None:
        opportunity_repo = OpportunityRepository()
        rejected_repo = RejectedOpportunityRepository()
        winner = opportunity_repo.save_opportunity(self._signal("BUY_CE", "BANKNIFTY26JUL58000CE", 100, 80, 140))
        opportunity_repo.update_outcome(winner.id, outcome="target_1", exit_price=140)
        rejection = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_PE",
            score=78,
            reasons=["entry_too_late"],
            contract=SimpleNamespace(
                tradingsymbol="BANKNIFTY26JUL57900PE",
                exchange="NFO",
                expiry="2026-07-26",
                strike=57900,
                option_type="PE",
            ),
            factor_scores={
                "prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80},
                "option_premium_confirmation": {"passed": False, "source": "stored_candles"},
                "strategy_metadata": {"strategy_version": "test_strategy"},
            },
            score_breakdown={"score": 78},
            market_session="REGULAR_MARKET",
        )
        rejected_repo.mark_later_outcome(rejection.id, outcome="would_have_hit_target", exit_price=130)

        result = ProfessionalInsightsService().analyze(symbol="BANKNIFTY", limit=100)

        self.assertEqual(result["accepted_vs_rejected"]["accepted"]["wins"], 1)
        self.assertEqual(result["accepted_vs_rejected"]["rejected"]["missed_winners"], 1)
        self.assertIn("rejection_gate:entry_too_late", result["factor_attribution"])
        self.assertIn("test_strategy", result["strategy_versions"]["versions"])

    def test_gate_effectiveness_report_separates_missed_saved_unresolved_and_ambiguous(self) -> None:
        rejected_repo = RejectedOpportunityRepository()
        missed = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=78,
            reasons=["premium confirmation failed"],
            contract=SimpleNamespace(tradingsymbol="BANKNIFTY26JUL58000CE", exchange="NFO", expiry="2026-07-26", strike=58000, option_type="CE"),
            factor_scores={"prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80}},
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        saved = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_PE",
            score=75,
            reasons=["premium confirmation failed"],
            contract=SimpleNamespace(tradingsymbol="BANKNIFTY26JUL57000PE", exchange="NFO", expiry="2026-07-26", strike=57000, option_type="PE"),
            factor_scores={"prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80}},
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        ambiguous = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=74,
            reasons=["premium confirmation failed"],
            contract=SimpleNamespace(tradingsymbol="BANKNIFTY26JUL58100CE", exchange="NFO", expiry="2026-07-26", strike=58100, option_type="CE"),
            factor_scores={"prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80}},
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=73,
            reasons=["premium confirmation failed"],
            contract=SimpleNamespace(tradingsymbol="BANKNIFTY26JUL58200CE", exchange="NFO", expiry="2026-07-26", strike=58200, option_type="CE"),
            factor_scores={"prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80}},
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        rejected_repo.mark_later_outcome(missed.id, outcome="would_have_hit_target_1", exit_price=130, outcome_minutes=3, outcome_source="candle_replay", outcome_timeframe="1minute")
        rejected_repo.mark_later_outcome(saved.id, outcome="would_have_hit_stop_loss", exit_price=80, outcome_minutes=5, outcome_source="candle_replay", outcome_timeframe="1minute")
        rejected_repo.mark_later_outcome(
            ambiguous.id,
            outcome="ambiguous_stop_and_target_same_candle",
            exit_price=101,
            outcome_minutes=2,
            outcome_source="candle_replay",
            outcome_timeframe="1minute",
            ambiguous=True,
        )

        result = ProfessionalInsightsService().gate_effectiveness_report(symbol="BANKNIFTY", limit=100)
        gate = {row["gate_or_reason"]: row for row in result["gates"]}["premium_confirmation_failed"]

        self.assertEqual(result["rejected_summary"]["missed_winners"], 1)
        self.assertEqual(result["rejected_summary"]["saved_losers"], 1)
        self.assertEqual(result["rejected_summary"]["ambiguous"], 1)
        self.assertEqual(result["rejected_summary"]["unresolved"], 1)
        self.assertEqual(gate["missed_winners"], 1)
        self.assertEqual(gate["saved_losers"], 1)
        self.assertEqual(gate["ambiguous"], 1)
        self.assertEqual(gate["unresolved"], 1)
        self.assertEqual(gate["avg_minutes_to_outcome"], 3.33)

    def test_research_engine_report_ranks_filters_and_segments_expectancy(self) -> None:
        opportunity_repo = OpportunityRepository()
        rejected_repo = RejectedOpportunityRepository()
        ce_winner = opportunity_repo.save_opportunity(
            self._signal(
                "BUY_CE",
                "BANKNIFTY26JUL58000CE",
                100,
                80,
                140,
                factor_scores={
                    "volatility_edge": {"classification": "iv_expansion_supported", "details": {"iv_rank": 45}},
                    "day_type": {"details": {"day_type": "trend_expansion"}},
                },
            )
        )
        pe_loser = opportunity_repo.save_opportunity(
            self._signal(
                "BUY_PE",
                "BANKNIFTY26JUL57000PE",
                100,
                80,
                140,
                factor_scores={
                    "volatility_edge": {"classification": "iv_crush_risk", "main_risk": "iv_crush", "details": {"iv_rank": 88}},
                    "day_type": {"details": {"day_type": "rotation_range"}},
                },
            )
        )
        opportunity_repo.update_outcome(ce_winner.id, outcome="target_1", exit_price=140)
        opportunity_repo.update_outcome(pe_loser.id, outcome="stop_loss", exit_price=80)
        missed = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=76,
            reasons=["range_compression_without_expansion", "option premium has not broken recent high"],
            contract=SimpleNamespace(
                tradingsymbol="BANKNIFTY26JUL58100CE",
                exchange="NFO",
                expiry="2026-07-26",
                strike=58100,
                option_type="CE",
            ),
            factor_scores={
                "prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80},
                "volatility_edge": {"classification": "iv_expansion_supported", "details": {"iv_rank": 50}},
                "day_type": {"details": {"day_type": "trend_expansion"}},
            },
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        saved = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_PE",
            score=70,
            reasons=["late_day_premium_decay_environment"],
            contract=SimpleNamespace(
                tradingsymbol="BANKNIFTY26JUL57000PE",
                exchange="NFO",
                expiry="2026-07-26",
                strike=57000,
                option_type="PE",
            ),
            factor_scores={
                "prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80},
                "volatility_edge": {"classification": "iv_crush_risk", "main_risk": "iv_crush", "details": {"iv_rank": 90}},
                "day_type": {"details": {"day_type": "rotation_range"}},
            },
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        rejected_repo.mark_later_outcome(missed.id, outcome="would_have_hit_target_1", exit_price=130)
        rejected_repo.mark_later_outcome(saved.id, outcome="would_have_hit_stop_loss", exit_price=80)

        result = ProfessionalInsightsService().research_engine_report(symbol="BANKNIFTY", limit=100)

        self.assertEqual(result["sample"]["closed_accepted_opportunities"], 2)
        self.assertEqual(result["sample"]["reviewed_rejections_with_later_outcome"], 2)
        filters = {row["filter_name"]: row for row in result["filter_rejection_quality"]["filters"]}
        self.assertEqual(filters["range_compression_without_expansion"]["later_winner_count"], 1)
        self.assertEqual(filters["late_day_premium_decay_environment"]["later_loser_count"], 1)
        self.assertEqual(result["accepted_trade_loss_impact"]["losing_count"], 1)
        self.assertIn("buy_ce", result["segment_expectancy"]["setup_family"])
        self.assertIn("iv:iv_expansion_supported", result["segment_expectancy"]["iv_regime"])
        self.assertIn("trend_expansion", result["segment_expectancy"]["trend_regime"])
        self.assertEqual(result["setup_family_ranking"][0]["setup_family"], "buy_ce")
        self.assertFalse(result["research_readiness"]["safe_to_enable_live"])

    def test_research_engine_uses_professional_setup_family_label(self) -> None:
        opportunity_repo = OpportunityRepository()
        trade_repo = TradeRepository()
        signal = self._signal(
            "BUY_CE",
            "BANKNIFTY26JUL58000CE",
            100,
            80,
            140,
            factor_scores={
                "setup_family": {
                    "name": "vwap_reclaim_continuation",
                    "group": "vwap_continuation",
                    "reasons": ["Bank Nifty and option premium are above VWAP"],
                }
            },
        )
        opportunity = opportunity_repo.save_opportunity(signal)
        opportunity_repo.update_outcome(opportunity.id, outcome="target_1", exit_price=140)
        trade = trade_repo.create_trade(signal, mode="paper", status="filled", requested_quantity=15, placed_quantity=15)
        trade_repo.close_trade(trade.id, outcome="target_1", exit_price=140)

        result = ProfessionalInsightsService().research_engine_report(symbol="BANKNIFTY", limit=100)

        self.assertIn("vwap_reclaim_continuation", result["segment_expectancy"]["setup_family"])
        self.assertEqual(result["setup_family_ranking"][0]["setup_family"], "vwap_reclaim_continuation")

    def test_threshold_validation_report_scores_rejections_and_sensitivity(self) -> None:
        opportunity_repo = OpportunityRepository()
        trade_repo = TradeRepository()
        rejected_repo = RejectedOpportunityRepository()
        original_min_score = 86
        signal = self._signal(
            "BUY_CE",
            "BANKNIFTY26JUL58000CE",
            100,
            80,
            140,
            factor_scores={"setup_family": {"name": "opening_drive_continuation", "group": "opening_drive"}},
        )
        opportunity = opportunity_repo.save_opportunity(signal)
        opportunity_repo.update_outcome(opportunity.id, outcome="target_1", exit_price=140)
        trade = trade_repo.create_trade(signal, mode="paper", status="filled", requested_quantity=15, placed_quantity=15)
        trade_repo.update_mfe_mae(trade.id, price=150)
        trade_repo.close_trade(trade.id, outcome="target_1", exit_price=135)
        rejected = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=78,
            reasons=["option premium has not broken recent high"],
            contract=SimpleNamespace(
                tradingsymbol="BANKNIFTY26JUL58100CE",
                exchange="NFO",
                expiry="2026-07-26",
                strike=58100,
                option_type="CE",
            ),
            factor_scores={
                "prices": {"entry_price": 100, "target_1": 130, "stop_loss": 80},
                "setup_family": {"name": "opening_drive_continuation", "group": "opening_drive"},
            },
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        rejected_repo.mark_later_outcome(rejected.id, outcome="would_have_hit_stop_loss", exit_price=80)

        result = ProfessionalInsightsService().threshold_validation_report(symbol="BANKNIFTY", limit=100)

        self.assertEqual(result["status"], "ok")
        self.assertIn("threshold_inventory", result)
        self.assertTrue(any(item["threshold_name"] == "min_signal_score" for item in result["threshold_inventory"]))
        self.assertIn("high_score", result["score_threshold_validation"]["buckets"])
        self.assertGreaterEqual(result["score_threshold_validation"]["buckets"]["high_score"]["accepted_count"], 1)
        reasons = {row["gate_or_reason"]: row for row in result["rejection_threshold_validation"]["rows"]}
        self.assertIn("option_premium_has_not_broken_recent_high", reasons)
        self.assertEqual(reasons["option_premium_has_not_broken_recent_high"]["later_loser_count"], 1)
        self.assertIn("opening_drive_continuation", result["setup_family_threshold_validation"])
        self.assertTrue(result["threshold_sensitivity"]["minimum_score"])
        self.assertEqual(original_min_score, signal.score)

    def test_threshold_validation_empty_report_is_clean_and_inconclusive(self) -> None:
        result = ProfessionalInsightsService().threshold_validation_report(symbol="BANKNIFTY", limit=100)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["score_threshold_validation"]["buckets"]["below_threshold"]["trades"], 0)
        self.assertFalse(result["data_support"]["accepted_vs_rejected"])
        self.assertTrue(result["threshold_sensitivity"]["minimum_score"])
        self.assertTrue(result["not_measurable_yet"])

    def test_professional_insights_daily_review_and_journal(self) -> None:
        repo = OpportunityRepository()
        trade_repo = TradeRepository()
        signal = self._signal("BUY_CE", "BANKNIFTY26JUL58000CE", 100, 80, 140)
        opportunity = repo.save_opportunity(signal)
        repo.update_outcome(opportunity.id, outcome="stop_loss", exit_price=80)
        trade = trade_repo.create_trade(signal, mode="paper", status="filled", requested_quantity=15, placed_quantity=15)
        trade_repo.close_trade(trade.id, outcome="target_1", exit_price=140)

        service = ProfessionalInsightsService()
        review = service.daily_review(symbol="BANKNIFTY", review_date=datetime.now().date(), limit=100)
        journal = service.trade_journal(symbol="BANKNIFTY", limit=100)

        self.assertEqual(review["sample"]["accepted_opportunities"], 1)
        self.assertEqual(review["sample"]["trades"], 1)
        self.assertGreaterEqual(len(journal["timeline"]), 2)

    def test_daily_banknifty_summary_groups_rejections_and_warns_on_low_sample(self) -> None:
        trade_repo = TradeRepository()
        rejected_repo = RejectedOpportunityRepository()
        winner = trade_repo.create_trade(
            self._signal("BUY_CE", "BANKNIFTY26JUL58000CE", 100, 80, 140),
            mode="paper",
            status="filled",
            requested_quantity=15,
            placed_quantity=15,
        )
        loser = trade_repo.create_trade(
            self._signal("BUY_PE", "BANKNIFTY26JUL57000PE", 100, 80, 140),
            mode="paper",
            status="filled",
            requested_quantity=15,
            placed_quantity=15,
        )
        trade_repo.create_trade(
            self._signal("BUY_CE", "BANKNIFTY26JUL58100CE", 100, 80, 140),
            mode="paper",
            status="filled",
            requested_quantity=15,
            placed_quantity=15,
        )
        trade_repo.close_trade(winner.id, outcome="target_1", exit_price=140)
        trade_repo.close_trade(loser.id, outcome="stop_loss", exit_price=80)
        rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=72,
            reasons=[
                "premium_candles_stale_or_missing",
                "top banks are mixed against Bank Nifty direction",
                "selected_option_quote_invalid",
            ],
        )

        result = ProfessionalInsightsService().daily_banknifty_summary(summary_date=datetime.now().date())

        self.assertEqual(result["total_paper_trades"], 3)
        self.assertEqual(result["total_closed_paper_trades"], 2)
        self.assertEqual(result["winning_trades"], 1)
        self.assertEqual(result["losing_trades"], 1)
        self.assertEqual(result["open_trades"], 1)
        self.assertIsNotNone(result["gross_pnl"])
        self.assertIsNotNone(result["net_pnl"])
        self.assertGreater(result["average_win"], 0)
        self.assertGreater(result["average_loss"], 0)
        self.assertEqual(result["total_rejected_opportunities"], 1)
        self.assertEqual(result["rejection_reasons_count"]["premium_candles_stale_or_missing"], 1)
        self.assertEqual(result["rejection_reasons_count"]["top_banks_mixed"], 1)
        self.assertEqual(result["rejection_reasons_count"]["quote_invalid"], 1)
        self.assertTrue(result["low_sample_warning"])
        self.assertFalse(result["recommendation"]["safe_to_change_strategy"])
        self.assertFalse(result["recommendation"]["safe_to_enable_live"])
        self.assertIn("Premium confirmation candles were stale or missing.", result["data_health_warnings"])

    def test_professional_data_completeness_reports_stored_market_data(self) -> None:
        session = get_session()
        try:
            session.add(
                Candle(
                    symbol="BANKNIFTY",
                    timeframe="1minute",
                    timestamp=datetime(2026, 7, 3, 9, 20),
                    open_price=58000,
                    high_price=58100,
                    low_price=57950,
                    close_price=58050,
                    volume=1000,
                )
            )
            session.add(
                OptionQuoteSnapshot(
                    underlying="BANKNIFTY",
                    tradingsymbol="BANKNIFTY26JUL58000CE",
                    exchange="NFO",
                    timestamp=datetime(2026, 7, 3, 9, 20),
                    expiry="2026-07-26",
                    strike=58000,
                    option_type="CE",
                    last_price=100,
                    bid=99,
                    ask=101,
                    open_interest=10000,
                    volume=5000,
                )
            )
            session.commit()
        finally:
            session.close()

        result = ProfessionalInsightsService().data_completeness(symbol="BANKNIFTY")

        self.assertEqual(result["candles"]["rows"], 1)
        self.assertEqual(result["option_snapshots"]["rows"], 1)
        self.assertTrue(result["readiness"]["has_underlying_candles"])

    def _signal(
        self,
        action: str,
        tradingsymbol: str,
        entry: float,
        stop: float,
        target: float,
        factor_scores: dict[str, object] | None = None,
    ) -> Signal:
        return Signal(
            symbol="BANKNIFTY",
            action=action,
            side="BUY",
            tradingsymbol=tradingsymbol,
            exchange="NFO",
            strike=58000,
            expiry="2026-07-26",
            entry_price=entry,
            stop_loss=stop,
            target_1=target,
            quantity=15,
            lot_size=15,
            probability=0.78,
            risk_reward=1.5,
            score=86,
            setup_type="directional_option_buy",
            factor_scores={"score_breakdown": {"score": 86}, **(factor_scores or {})},
        )


if __name__ == "__main__":
    unittest.main()
