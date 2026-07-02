import unittest

from app.services.paper_trading_service import PaperTradingService


class PaperTradingServiceTests(unittest.TestCase):
    def test_execute_trade_updates_equity_and_pnl(self) -> None:
        service = PaperTradingService()
        service.execute_trade("NIFTY", 100.0, 1, 110.0, "BUY_CE")
        summary = service.get_summary()
        self.assertEqual(summary["open_positions"], 1)
        self.assertEqual(summary["equity"], 100.0)
        self.assertEqual(summary["pnl"], 0.0)

    def test_close_trade_updates_pnl(self) -> None:
        service = PaperTradingService()
        service.execute_trade("NIFTY", 100.0, 1, 110.0, "BUY_CE")
        trade = service.close_trade("NIFTY", 115.0)
        summary = service.get_summary()
        self.assertEqual(summary["closed_trades"], 1)
        self.assertEqual(trade["gross_pnl"], 15.0)
        self.assertGreater(trade["charges"], 0)
        self.assertEqual(summary["pnl"], trade["net_pnl"])
        self.assertLess(summary["pnl"], trade["gross_pnl"])


if __name__ == "__main__":
    unittest.main()
