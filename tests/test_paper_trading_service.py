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
        service.close_trade("NIFTY", 115.0)
        summary = service.get_summary()
        self.assertEqual(summary["closed_trades"], 1)
        self.assertEqual(summary["pnl"], 15.0)


if __name__ == "__main__":
    unittest.main()
