import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.models import Signal
from app.services.active_price_feed import PriceTick
from app.services.backtest_service import BacktestService
from app.services.database import TradeRecord, get_session, init_db
from app.services.paper_trading_service import PaperTradingService
from app.services.professional_insights_service import ProfessionalInsightsService
from app.services.trade_exit_service import TradeExitService
from app.services.trade_repository import TradeRepository


class MfeMaeFeed:
    def __init__(self, price: float) -> None:
        self.price = price
        self.last_reason = None

    def latest_price(self, *, provider, exchange, tradingsymbol, instrument_token=None, mode="paper"):
        return PriceTick(
            instrument=f"{exchange}:{tradingsymbol}",
            price=self.price,
            timestamp=datetime(2026, 7, 6, 10, 1),
            source="test_websocket",
            instrument_token=instrument_token,
            bid=self.price,
            ask=self.price + 0.5,
            buy_depth=({"price": self.price, "quantity": 100},),
            sell_depth=({"price": self.price + 0.5, "quantity": 100},),
        )

    def subscribe(self, tokens):
        return {"subscribed": list(tokens)}

    def unsubscribe(self, tokens):
        return {"unsubscribed": list(tokens)}

    def status(self):
        return {"websocket_enabled": True, "websocket_connected": True}


class MfeMaeProvider:
    def instruments(self, exchange=None):
        if exchange == "NFO":
            return [{"tradingsymbol": "BANKNIFTY26JUL58000CE", "exchange": "NFO", "instrument_token": 123}]
        return [{"tradingsymbol": "NIFTY BANK", "name": "NIFTY BANK", "instrument_token": 260105}]

    def quote(self, instruments):
        return {instruments[0]: {"last_price": 112.0, "depth": {"buy": [{"price": 111.5}], "sell": [{"price": 112.0}]}}}


class MfeMaeTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_auto_squareoff": settings.enable_auto_squareoff,
            "enable_underlying_invalidation_exit": settings.enable_underlying_invalidation_exit,
            "enable_premium_invalidation_exit": settings.enable_premium_invalidation_exit,
            "enable_execution_realism": settings.enable_execution_realism,
            "backtest_slippage_pct": settings.backtest_slippage_pct,
            "paper_spread_impact_pct_per_side": settings.paper_spread_impact_pct_per_side,
            "backtest_option_stop_loss_pct": settings.backtest_option_stop_loss_pct,
            "backtest_option_target_pct": settings.backtest_option_target_pct,
        }
        object.__setattr__(settings, "enable_auto_squareoff", True)
        object.__setattr__(settings, "enable_underlying_invalidation_exit", False)
        object.__setattr__(settings, "enable_premium_invalidation_exit", False)
        object.__setattr__(settings, "enable_execution_realism", False)
        object.__setattr__(settings, "backtest_slippage_pct", 0.0)
        object.__setattr__(settings, "paper_spread_impact_pct_per_side", 0.0)
        object.__setattr__(settings, "backtest_option_stop_loss_pct", 20.0)
        object.__setattr__(settings, "backtest_option_target_pct", 25.0)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.repo = TradeRepository()

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_trade_mfe_mae_tracks_winner_multiple_updates(self) -> None:
        trade = self._create_trade()

        self.repo.update_mfe_mae(trade.id, price=94.0, price_timestamp=datetime(2026, 7, 6, 9, 16))
        self.repo.update_mfe_mae(trade.id, price=142.0, price_timestamp=datetime(2026, 7, 6, 9, 19))
        self.repo.update_mfe_mae(trade.id, price=130.0, price_timestamp=datetime(2026, 7, 6, 9, 20))
        updated = self.repo.get_trade(trade.id)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.highest_price_during_trade, 142.0)
        self.assertEqual(updated.lowest_price_during_trade, 94.0)
        self.assertEqual(updated.mfe_points, 42.0)
        self.assertEqual(updated.mae_points, 6.0)
        self.assertEqual(updated.mfe_percent, 42.0)
        self.assertEqual(updated.mae_percent, 6.0)

    def test_invalid_price_does_not_pollute_mfe_mae(self) -> None:
        trade = self._create_trade()

        self.repo.update_mfe_mae(trade.id, price=0.0)
        self.repo.update_mfe_mae(trade.id, price=-5.0)
        updated = self.repo.get_trade(trade.id)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.highest_price_during_trade, 100.0)
        self.assertEqual(updated.lowest_price_during_trade, 100.0)
        self.assertEqual(updated.mfe_points, 0.0)
        self.assertEqual(updated.mae_points, 0.0)

    def test_loser_tracks_mae_without_positive_mfe(self) -> None:
        trade = self._create_trade()

        self.repo.update_mfe_mae(trade.id, price=98.0)
        closed = self.repo.close_trade(trade.id, outcome="stop_loss", exit_price=90.0)

        self.assertEqual(closed.highest_price_during_trade, 100.0)
        self.assertEqual(closed.lowest_price_during_trade, 90.0)
        self.assertEqual(closed.mfe_points, 0.0)
        self.assertEqual(closed.mae_points, 10.0)

    def test_exit_service_updates_mfe_mae_from_active_price_tick(self) -> None:
        trade = self._create_trade(target_1=110.0)
        service = TradeExitService(
            trade_repository=self.repo,
            kite_provider_factory=lambda: MfeMaeProvider(),
            paper_trading_service=PaperTradingService(),
            active_price_feed=MfeMaeFeed(112.0),
        )

        result = service.evaluate_once()
        updated = self.repo.get_trade(trade.id)

        self.assertEqual(result["closed"], 1)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.highest_price_during_trade, 112.0)
        self.assertEqual(updated.mfe_points, 12.0)

    def test_backtest_calculates_mfe_mae_from_replayed_option_candles(self) -> None:
        start = datetime(2026, 7, 6, 10, 0)
        underlying = [
            SimpleNamespace(timestamp=start, close_price=58000),
            SimpleNamespace(timestamp=start + timedelta(minutes=5), close_price=58020),
            SimpleNamespace(timestamp=start + timedelta(minutes=10), close_price=58025),
        ]
        option_candles = {
            "BANKNIFTY26JUL58000CE": [
                SimpleNamespace(timestamp=start, open_price=100, high_price=101, low_price=99, close_price=100, volume=1000),
                SimpleNamespace(timestamp=start + timedelta(minutes=5), open_price=101, high_price=112, low_price=96, close_price=108, volume=1000),
                SimpleNamespace(timestamp=start + timedelta(minutes=10), open_price=108, high_price=118, low_price=91, close_price=110, volume=1000),
            ]
        }

        trade = BacktestService()._simulate_option_trade(
            underlying=underlying,
            option_candles=option_candles,
            idx=0,
            direction="CALL",
            horizon=2,
            reason="unit",
        )

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.highest_price_during_trade, 118.0)
        self.assertEqual(trade.lowest_price_during_trade, 91.0)
        self.assertEqual(trade.mfe_points, 18.0)
        self.assertEqual(trade.mae_points, 9.0)
        self.assertEqual(trade.time_to_mfe, 600.0)

    def test_research_summary_tolerates_old_null_mfe_mae_rows(self) -> None:
        trade = self._create_trade()
        self.repo.close_trade(trade.id, outcome="target_1", exit_price=112.0)
        session = get_session()
        try:
            row = session.get(TradeRecord, trade.id)
            row.highest_price_during_trade = None
            row.lowest_price_during_trade = None
            row.mfe_points = None
            row.mae_points = None
            row.mfe_percent = None
            row.mae_percent = None
            session.commit()
        finally:
            session.close()

        report = ProfessionalInsightsService().research_engine_report(symbol="BANKNIFTY", limit=10)

        self.assertEqual(report["mfe_mae"]["tracked_trades"], 0)
        self.assertEqual(report["mfe_mae"]["missing_trades"], 1)

    def _create_trade(self, *, target_1: float = 120.0):
        signal = Signal(
            symbol="BANKNIFTY",
            action="BUY_CE",
            side="BUY",
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=123,
            entry_price=100.0,
            stop_loss=90.0,
            target_1=target_1,
            target_2=130.0,
            target_3=140.0,
            quantity=15,
            lot_size=15,
            score=90,
            factor_scores={"setup_family": {"name": "unit_setup", "group": "unit"}},
        )
        return self.repo.create_trade(signal, mode="paper", status="filled", requested_quantity=15, placed_quantity=15)


if __name__ == "__main__":
    unittest.main()
