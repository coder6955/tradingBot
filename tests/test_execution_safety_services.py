import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.services.data_freshness_service import DataFreshnessService
from app.services.database import init_db
from app.services.decision_engine_service import DecisionEngineService
from app.services.mock_market_feed import MockMarketFeed
from app.services.realistic_pnl_service import RealisticPnlService
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.scanner_service import ScannerService
from app.services.trade_setup_service import OptionContract


class ExecutionSafetyServiceTests(unittest.TestCase):
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

    def test_live_freshness_rejects_stale_fallback_data(self) -> None:
        stale = (datetime.now() - timedelta(minutes=30)).isoformat(sep=" ")
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=1,
            name="BANKNIFTY",
            expiry="2099-07-26",
            strike=58000,
            option_type="CE",
            lot_size=15,
            last_price=100,
            bid=99,
            ask=100,
        )

        result = DataFreshnessService().validate_scan_inputs(
            order_mode="live",
            snapshot={"source": "stored_candles", "is_real_data": True, "quote_timestamp": stale, "candles": [{"date": stale}]},
            chain_quotes={"NFO:BANKNIFTY26JUL58000CE": {"last_price": 100, "quote_timestamp": stale}},
            contract=contract,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(any("stale" in reason for reason in result["reasons"]))
        self.assertTrue(any("canonical completed-candle analysis" in reason for reason in result["reasons"]))
        self.assertTrue(any("timestamp provenance" in reason for reason in result["reasons"]))

    def test_rejected_opportunity_repository_persists_reason_breakdown(self) -> None:
        repo = RejectedOpportunityRepository()
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000PE",
            exchange="NFO",
            instrument_token=1,
            name="BANKNIFTY",
            expiry="2099-07-26",
            strike=58000,
            option_type="PE",
            lot_size=15,
        )

        repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_PE",
            score=76,
            reasons=["option premium confirmation failed"],
            snapshot={"price": 58000},
            contract=contract,
            factor_scores={"option_quality": {"score": 80}, "option_premium_confirmation": {"score": 45}},
            score_breakdown={"score": 76},
        )
        report = repo.analyze(symbol="BANKNIFTY")

        self.assertEqual(report["sample"]["total_rejected"], 1)
        self.assertIn("option premium confirmation failed", report["top_reasons"])
        self.assertEqual(report["ce_vs_pe"]["PE"], 1)

    def test_realistic_pnl_deducts_costs_from_gross(self) -> None:
        result = RealisticPnlService().calculate(entry_price=100, exit_price=110, quantity=30, side="BUY")

        self.assertGreater(result.gross_pnl, 0)
        self.assertGreater(result.charges, 0)
        self.assertLess(result.net_pnl, result.gross_pnl)

    def test_scanner_and_decision_engine_score_parity(self) -> None:
        scanner = ScannerService(feed=MockMarketFeed(), rejected_opportunity_repository=RejectedOpportunityRepository())
        scanner_score = scanner._score_breakdown(  # noqa: SLF001
            technical_score=95,
            market_score=70,
            price_score=80,
            chain_score=60,
            liquidity_score=85,
            quality_score=90,
            banknifty_score=75,
        )
        engine_score = DecisionEngineService().score_breakdown(
            technical_score=95,
            market_score=70,
            price_score=80,
            chain_score=60,
            liquidity_score=85,
            quality_score=90,
            banknifty_score=75,
        )

        self.assertEqual(scanner_score, engine_score)
        self.assertEqual(scanner_score["caps"]["trend_momentum_capped_at"], 75)


if __name__ == "__main__":
    unittest.main()
