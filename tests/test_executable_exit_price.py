import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace

from app.config import settings
from app.models import Signal
from app.services.active_price_feed import PriceTick
from app.services.database import init_db
from app.services.executable_price_service import ExecutablePriceService
from app.services.paper_trading_service import PaperTradingService
from app.services.time_utils import ist_now_naive
from app.services.trade_exit_service import TradeExitService
from app.services.trade_repository import TradeRepository


class _Provider:
    def instruments(self, exchange=None):
        if exchange == "NFO":
            return [
                {
                    "tradingsymbol": "BANKNIFTY26JUL58000CE",
                    "exchange": "NFO",
                    "instrument_token": 123,
                }
            ]
        return [
            {
                "tradingsymbol": "NIFTY BANK",
                "name": "NIFTY BANK",
                "instrument_token": 260105,
            }
        ]


class _Feed:
    def __init__(self, tick: PriceTick | None, reason: str | None = None) -> None:
        self.tick = tick
        self.last_reason = reason

    def latest_price(self, **kwargs):
        return self.tick

    def subscribe(self, tokens):
        return {"subscribed": sorted(tokens)}

    def unsubscribe(self, tokens):
        return {"unsubscribed": sorted(tokens)}

    def status(self):
        return {}


def _tick(
    *, ltp: float, bid: float | None, buy_depth=(), ask: float | None = None
) -> PriceTick:
    return PriceTick(
        instrument="NFO:BANKNIFTY26JUL58000CE",
        instrument_token=123,
        price=ltp,
        bid=bid,
        ask=ask,
        buy_depth=tuple(buy_depth),
        timestamp=ist_now_naive(),
        source="kite_websocket",
        age_seconds=0.0,
        timestamp_source="exchange_timestamp",
    )


class ExecutablePriceServiceTests(unittest.TestCase):
    def test_depth_weighted_price_covers_position(self) -> None:
        result = ExecutablePriceService().for_long_exit(
            _tick(
                ltp=112,
                bid=111,
                ask=112,
                buy_depth=(
                    {"price": 111, "quantity": 10},
                    {"price": 110, "quantity": 10},
                ),
            ),
            quantity=15,
        )

        self.assertAlmostEqual(result.executable_price or 0, 110.666666, places=5)
        self.assertEqual(result.depth_coverage, 1.0)
        self.assertTrue(result.live_safe)

    def test_ltp_only_spike_is_not_executable(self) -> None:
        result = ExecutablePriceService().for_long_exit(
            _tick(ltp=130, bid=None), quantity=15
        )

        self.assertIsNone(result.executable_price)
        self.assertFalse(result.target_supported)
        self.assertEqual(result.price_source, "ltp_diagnostic_only")

    def test_thin_partial_depth_uses_worst_visible_bid_and_blocks_target(self) -> None:
        result = ExecutablePriceService().for_long_exit(
            _tick(
                ltp=112,
                bid=110,
                ask=112,
                buy_depth=(
                    {"price": 110, "quantity": 5},
                    {"price": 107, "quantity": 3},
                ),
            ),
            quantity=15,
        )

        self.assertEqual(result.executable_price, 107)
        self.assertAlmostEqual(result.depth_coverage or 0, 8 / 15, places=6)
        self.assertFalse(result.target_supported)
        self.assertFalse(result.live_safe)


class ExecutableTradeExitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_underlying_invalidation_exit": settings.enable_underlying_invalidation_exit,
            "enable_premium_invalidation_exit": settings.enable_premium_invalidation_exit,
            "option_time_stop_minutes": settings.option_time_stop_minutes,
            "exit_open_trades_before_close_minutes": settings.exit_open_trades_before_close_minutes,
        }
        object.__setattr__(settings, "enable_underlying_invalidation_exit", False)
        object.__setattr__(settings, "enable_premium_invalidation_exit", False)
        object.__setattr__(settings, "option_time_stop_minutes", 0)
        object.__setattr__(settings, "exit_open_trades_before_close_minutes", 0)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.repo = TradeRepository()

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        try:
            os.remove(self.temp_db.name)
        except (FileNotFoundError, PermissionError):
            pass

    def _trade(self):
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=123,
            entry_price=100,
            stop_loss=90,
            target_1=110,
            target_2=120,
            target_3=130,
            quantity=15,
            score=90,
        )
        return self.repo.create_trade(
            signal,
            mode="paper",
            status="filled",
            requested_quantity=15,
            placed_quantity=15,
        )

    def _service(self, tick: PriceTick) -> TradeExitService:
        return TradeExitService(
            trade_repository=self.repo,
            kite_provider_factory=_Provider,
            paper_trading_service=PaperTradingService(),
            active_price_feed=_Feed(tick),
        )

    def test_ltp_only_target_spike_does_not_close_trade(self) -> None:
        self._trade()

        result = self._service(_tick(ltp=130, bid=None)).evaluate_once()

        self.assertFalse(result["results"][0]["closed"])
        self.assertEqual(result["results"][0]["reason"], "executable_bid_unavailable")

    def test_stop_gap_uses_conservative_partial_depth_price(self) -> None:
        trade = self._trade()
        tick = _tick(
            ltp=88,
            bid=86,
            ask=89,
            buy_depth=({"price": 86, "quantity": 5}, {"price": 84, "quantity": 5}),
        )

        result = self._service(tick).evaluate_once()
        updated = self.repo.get_trade(int(trade.id))

        self.assertTrue(result["results"][0]["closed"])
        self.assertEqual(result["results"][0]["outcome"], "stop_loss")
        self.assertEqual(updated.exit_price, 84)
        self.assertEqual(
            updated.exit_execution_source, "partial_depth_conservative_bid"
        )

    def test_simultaneous_stop_and_time_exit_attributes_stop_first(self) -> None:
        trade = SimpleNamespace(
            side="BUY",
            stop_loss=90,
            target_1=110,
            target_2=120,
            target_3=130,
            average_price=100,
            entry_price=100,
            created_at=ist_now_naive() - timedelta(minutes=60),
            tradingsymbol="BANKNIFTY26JUL58000CE",
        )
        object.__setattr__(settings, "option_time_stop_minutes", 15)

        decision = self._service(_tick(ltp=85, bid=85))._outcome_for_price(
            _Provider(), trade, 85
        )

        self.assertEqual(decision["first_triggered"], "stop_loss")
        self.assertEqual(decision["triggered_rules"][:2], ["stop_loss", "time_exit"])


if __name__ == "__main__":
    unittest.main()
