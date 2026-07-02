import os
import tempfile
import unittest

from app.models import Signal
from app.services.database import init_db
from app.services.execution_analytics_service import ExecutionAnalyticsService
from app.services.opportunity_analytics_service import OpportunityAnalyticsService
from app.services.opportunity_repository import OpportunityRepository
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
