import unittest
from datetime import datetime
from types import SimpleNamespace

from app.services.execution_realism_service import ExecutionRealismService


class ExecutionRealismServiceTests(unittest.TestCase):
    def test_buy_entry_uses_ask_or_adverse_slippage_not_ltp(self) -> None:
        fill = ExecutionRealismService().entry_fill(
            intended_price=100,
            side="BUY",
            bid=99.5,
            ask=101.0,
            timestamp=datetime(2026, 7, 6, 10, 0),
        )

        self.assertTrue(fill.filled)
        self.assertEqual(round(fill.fill_price, 2), 101.0)
        self.assertGreater(fill.fill_price, fill.intended_price)

    def test_target_barely_touched_is_no_fill(self) -> None:
        fill = ExecutionRealismService().exit_fill(
            intended_price=120,
            side="BUY",
            outcome="target_1",
            candle=SimpleNamespace(high_price=120.05),
            timestamp=datetime(2026, 7, 6, 10, 0),
        )

        self.assertFalse(fill.filled)
        self.assertEqual(fill.no_fill_reason, "target_barely_touched")

    def test_target_clearance_fills_below_target(self) -> None:
        fill = ExecutionRealismService().exit_fill(
            intended_price=120,
            side="BUY",
            outcome="target_1",
            candle=SimpleNamespace(high_price=122),
            bid=119.4,
            ask=120.2,
            timestamp=datetime(2026, 7, 6, 10, 0),
        )

        self.assertTrue(fill.filled)
        self.assertLess(fill.fill_price, 120)

    def test_stop_loss_uses_worse_than_stop_fill(self) -> None:
        fill = ExecutionRealismService().exit_fill(
            intended_price=85,
            side="BUY",
            outcome="stop_loss",
            candle=SimpleNamespace(low_price=84),
            timestamp=datetime(2026, 7, 6, 10, 0),
        )

        self.assertTrue(fill.filled)
        self.assertLess(fill.fill_price, 85)

    def test_opening_and_expiry_add_extra_friction(self) -> None:
        service = ExecutionRealismService()
        normal = service.exit_fill(
            intended_price=100,
            side="BUY",
            outcome="target_1",
            candle=SimpleNamespace(high_price=105),
            timestamp=datetime(2026, 7, 6, 10, 0),
            expiry="2026-07-09",
        )
        opening_expiry = service.exit_fill(
            intended_price=100,
            side="BUY",
            outcome="target_1",
            candle=SimpleNamespace(high_price=105),
            timestamp=datetime(2026, 7, 9, 9, 20),
            expiry="2026-07-09",
        )

        self.assertLess(opening_expiry.fill_price, normal.fill_price)


if __name__ == "__main__":
    unittest.main()
