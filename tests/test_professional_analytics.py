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
            reasons=["premium_candles_stale_or_missing"],
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
        )
        rejected_repo.mark_later_outcome(rejection.id, outcome="would_have_hit_target", exit_price=130)

        result = ProfessionalInsightsService().analyze(symbol="BANKNIFTY", limit=100)

        self.assertEqual(result["accepted_vs_rejected"]["accepted"]["wins"], 1)
        self.assertEqual(result["accepted_vs_rejected"]["rejected"]["missed_winners"], 1)
        self.assertIn("rejection_gate:premium_candles_stale_or_missing", result["factor_attribution"])
        self.assertIn("test_strategy", result["strategy_versions"]["versions"])

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

    def _signal(self, action: str, tradingsymbol: str, entry: float, stop: float, target: float) -> Signal:
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
            factor_scores={"score_breakdown": {"score": 86}},
        )


if __name__ == "__main__":
    unittest.main()
