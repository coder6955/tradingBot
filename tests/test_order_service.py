import unittest

from app.models import Signal
from app.services.order_service import OrderService
from app.services.paper_trading_service import PaperTradingService


class FailingKiteProvider:
    def place_order(self, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("live order should not be called")


class OrderServiceTests(unittest.TestCase):
    def test_place_signal_order_defaults_to_paper(self) -> None:
        service = OrderService(
            kite_provider=FailingKiteProvider(),  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
        )
        signal = Signal(
            symbol="NIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="NIFTY24JUN22000CE",
            entry_price=100,
            stop_loss=80,
            quantity=50,
            score=85,
        )

        result = service.place_signal_order(signal, confirm_live=False)

        self.assertEqual(result["status"], "paper")
        self.assertEqual(result["trade"]["symbol"], "NIFTY24JUN22000CE")


if __name__ == "__main__":
    unittest.main()
