import unittest

from app.models import Signal
from app.services.order_service import OrderService
from app.services.paper_trading_service import PaperTradingService


class FailingKiteProvider:
    def quote(self, instruments):  # type: ignore[no-untyped-def]
        return {
            instruments[0]: {
                "last_price": 100,
                "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100}]},
            }
        }

    def place_order(self, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("live order should not be called")


class LiveKiteProvider:
    def __init__(self) -> None:
        self.order = None

    def margins(self):  # type: ignore[no-untyped-def]
        return {"equity": {"available": {"cash": 10000}}}

    def quote(self, instruments):  # type: ignore[no-untyped-def]
        return {
            instruments[0]: {
                "last_price": 100,
                "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100}]},
            }
        }

    def place_order(self, **kwargs):  # type: ignore[no-untyped-def]
        self.order = kwargs
        return {"order_id": "test-order"}


class PassingRiskService:
    def evaluate_signal(self, symbol):  # type: ignore[no-untyped-def]
        return {"passed": True, "reasons": []}


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

    def test_live_order_downsizes_to_available_cash(self) -> None:
        provider = LiveKiteProvider()
        service = OrderService(
            kite_provider=provider,  # type: ignore[arg-type]
            paper_trading_service=PaperTradingService(),
            risk_management_service=PassingRiskService(),  # type: ignore[arg-type]
        )
        signal = Signal(
            symbol="NIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="NIFTY24JUN22000CE",
            entry_price=100,
            stop_loss=80,
            quantity=500,
            lot_size=50,
            score=85,
        )

        from app.services import order_service

        original_live = order_service.settings.live_trading_mode
        original_paper = order_service.settings.paper_trading_mode
        try:
            object.__setattr__(order_service.settings, "live_trading_mode", True)
            object.__setattr__(order_service.settings, "paper_trading_mode", False)
            result = service.place_signal_order(signal, confirm_live=True, order_mode="live")
        finally:
            object.__setattr__(order_service.settings, "live_trading_mode", original_live)
            object.__setattr__(order_service.settings, "paper_trading_mode", original_paper)

        self.assertEqual(result["status"], "live")
        self.assertEqual(result["requested_quantity"], 500)
        self.assertEqual(result["placed_quantity"], 100)
        self.assertEqual(provider.order["quantity"], 100)


if __name__ == "__main__":
    unittest.main()
