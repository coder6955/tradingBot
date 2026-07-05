import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.services.backtest_service import BacktestService
from app.services.database import Candle, OptionQuoteSnapshot, RejectedOpportunityRecord, get_session, init_db


class BacktestScannerParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "min_signal_score": settings.min_signal_score,
            "min_market_regime_score": settings.min_market_regime_score,
            "min_price_action_score": settings.min_price_action_score,
            "min_option_chain_score": settings.min_option_chain_score,
            "min_option_liquidity_score": settings.min_option_liquidity_score,
            "min_option_quality_score": settings.min_option_quality_score,
            "min_option_premium_confirmation_score": settings.min_option_premium_confirmation_score,
            "min_option_oi": settings.min_option_oi,
            "min_option_volume": settings.min_option_volume,
            "min_risk_reward": settings.min_risk_reward,
            "min_remaining_risk_reward": settings.min_remaining_risk_reward,
            "min_target1_room_pct": settings.min_target1_room_pct,
            "max_premium_move_from_base_pct": settings.max_premium_move_from_base_pct,
            "max_entry_chase_pct": settings.max_entry_chase_pct,
            "min_entry_expected_move_coverage": settings.min_entry_expected_move_coverage,
            "min_entry_room_to_level_pct": settings.min_entry_room_to_level_pct,
            "enable_banknifty_intelligence": settings.enable_banknifty_intelligence,
            "enable_day_type_filter": settings.enable_day_type_filter,
            "enable_outcome_learning_guard": settings.enable_outcome_learning_guard,
            "enable_volatility_edge": settings.enable_volatility_edge,
            "enable_time_bucket_filter": settings.enable_time_bucket_filter,
            "enable_strategy_edge_guard": settings.enable_strategy_edge_guard,
            "block_expiry_day_option_buying": settings.block_expiry_day_option_buying,
            "max_bid_ask_spread_pct": settings.max_bid_ask_spread_pct,
            "min_option_buy_delta": settings.min_option_buy_delta,
            "max_option_buy_delta": settings.max_option_buy_delta,
            "max_option_buy_theta_pct": settings.max_option_buy_theta_pct,
            "min_option_buy_iv": settings.min_option_buy_iv,
            "max_option_buy_iv": settings.max_option_buy_iv,
        }
        object.__setattr__(settings, "min_signal_score", 1)
        object.__setattr__(settings, "min_market_regime_score", 0)
        object.__setattr__(settings, "min_price_action_score", 0)
        object.__setattr__(settings, "min_option_chain_score", 0)
        object.__setattr__(settings, "min_option_liquidity_score", 0)
        object.__setattr__(settings, "min_option_quality_score", 0)
        object.__setattr__(settings, "min_option_premium_confirmation_score", 1)
        object.__setattr__(settings, "min_option_oi", 0)
        object.__setattr__(settings, "min_option_volume", 0)
        object.__setattr__(settings, "min_risk_reward", 0.1)
        object.__setattr__(settings, "min_remaining_risk_reward", 0.1)
        object.__setattr__(settings, "min_target1_room_pct", 0.1)
        object.__setattr__(settings, "max_premium_move_from_base_pct", 50.0)
        object.__setattr__(settings, "max_entry_chase_pct", 5.0)
        object.__setattr__(settings, "min_entry_expected_move_coverage", 0.0)
        object.__setattr__(settings, "min_entry_room_to_level_pct", 0.0)
        object.__setattr__(settings, "enable_banknifty_intelligence", False)
        object.__setattr__(settings, "enable_day_type_filter", False)
        object.__setattr__(settings, "enable_outcome_learning_guard", False)
        object.__setattr__(settings, "enable_volatility_edge", False)
        object.__setattr__(settings, "enable_time_bucket_filter", False)
        object.__setattr__(settings, "enable_strategy_edge_guard", False)
        object.__setattr__(settings, "block_expiry_day_option_buying", False)
        object.__setattr__(settings, "max_bid_ask_spread_pct", 10.0)
        object.__setattr__(settings, "min_option_buy_delta", 0.0)
        object.__setattr__(settings, "max_option_buy_delta", 1.0)
        object.__setattr__(settings, "max_option_buy_theta_pct", 100.0)
        object.__setattr__(settings, "min_option_buy_iv", 0.0)
        object.__setattr__(settings, "max_option_buy_iv", 5.0)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self._seed_history()

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_option_backtest_uses_scanner_parity_by_default(self) -> None:
        result = BacktestService().run_option_premium(
            symbol="BANKNIFTY",
            timeframe="5minute",
            direction="CALL",
            horizon_candles=8,
            limit=90,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "scanner_parity_option_premium_replay")
        self.assertEqual(result["decision_engine"], "ScannerService")
        self.assertGreater(result["decisions_scanned"], 0)
        self.assertIn("TradeSetupService.build_prices", result["shared_live_components"])
        self.assertIn("legacy", result["legacy_mode_available"])

    def test_scanner_parity_backtest_does_not_persist_rejected_rows(self) -> None:
        result = BacktestService().run_option_premium(
            symbol="BANKNIFTY",
            timeframe="5minute",
            direction="PUT",
            horizon_candles=8,
            limit=90,
        )
        session = get_session()
        try:
            rejected_count = session.query(RejectedOpportunityRecord).count()
        finally:
            session.close()

        self.assertEqual(result["mode"], "scanner_parity_option_premium_replay")
        self.assertGreater(result["rejected_decisions"], 0)
        self.assertEqual(rejected_count, 0)

    def test_legacy_mode_remains_available_for_comparison(self) -> None:
        result = BacktestService().run_option_premium(
            symbol="BANKNIFTY",
            timeframe="5minute",
            direction="CALL",
            horizon_candles=8,
            limit=90,
            decision_mode="legacy",
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "option_premium_replay")
        self.assertEqual(result["decision_mode"], "legacy")

    def test_realistic_backtest_does_not_fill_target_on_bare_touch(self) -> None:
        service = BacktestService()
        start = datetime(2026, 7, 6, 10, 0)
        underlying = [
            SimpleNamespace(timestamp=start, close_price=58000),
            SimpleNamespace(timestamp=start + timedelta(minutes=5), close_price=58020),
            SimpleNamespace(timestamp=start + timedelta(minutes=10), close_price=58030),
        ]
        option_candles = {
            "BANKNIFTY26JUL58000CE": [
                SimpleNamespace(timestamp=start, open_price=100, high_price=101, low_price=99, close_price=100, volume=10000),
                SimpleNamespace(timestamp=start + timedelta(minutes=5), open_price=118, high_price=120.4, low_price=116, close_price=119, volume=10000),
                SimpleNamespace(timestamp=start + timedelta(minutes=10), open_price=110, high_price=112, low_price=108, close_price=110, volume=10000),
            ]
        }

        trade = service._simulate_option_trade(
            underlying=underlying,
            option_candles=option_candles,
            idx=0,
            direction="CALL",
            horizon=2,
            reason="test",
        )

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertNotEqual(trade.outcome, "target")
        self.assertIn("execution_realism", trade.to_dict())

    def _seed_history(self) -> None:
        start = datetime(2026, 7, 3, 9, 15)
        session = get_session()
        try:
            for idx in range(90):
                timestamp = start + timedelta(minutes=5 * idx)
                bank_close = 58000 + idx * 18
                nifty_close = 24000 + idx * 5
                self._add_candle(session, "BANKNIFTY", timestamp, bank_close, volume=100000 + idx * 1000)
                self._add_candle(session, "NIFTY", timestamp, nifty_close, volume=200000 + idx * 1000)
                ce_close = 100 + idx * 1.4
                pe_close = max(20.0, 180 - idx * 0.7)
                self._add_candle(session, "BANKNIFTY26JUL58000CE", timestamp, ce_close, volume=5000 + idx * 100)
                self._add_candle(session, "BANKNIFTY26JUL58000PE", timestamp, pe_close, volume=4500 + idx * 90)
                session.add(
                    OptionQuoteSnapshot(
                        underlying="BANKNIFTY",
                        tradingsymbol="BANKNIFTY26JUL58000CE",
                        exchange="NFO",
                        timestamp=timestamp,
                        expiry="2026-07-26",
                        strike=58000,
                        option_type="CE",
                        last_price=ce_close,
                        bid=ce_close - 0.5,
                        ask=ce_close + 0.5,
                        open_interest=100000 + idx * 100,
                        volume=5000 + idx * 100,
                    )
                )
                session.add(
                    OptionQuoteSnapshot(
                        underlying="BANKNIFTY",
                        tradingsymbol="BANKNIFTY26JUL58000PE",
                        exchange="NFO",
                        timestamp=timestamp,
                        expiry="2026-07-26",
                        strike=58000,
                        option_type="PE",
                        last_price=pe_close,
                        bid=max(0.05, pe_close - 0.5),
                        ask=pe_close + 0.5,
                        open_interest=90000 + idx * 100,
                        volume=4500 + idx * 90,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _add_candle(self, session, symbol: str, timestamp: datetime, close: float, *, volume: float) -> None:
        session.add(
            Candle(
                symbol=symbol,
                timeframe="5minute",
                timestamp=timestamp,
                open_price=close - 3,
                high_price=close + 6,
                low_price=close - 6,
                close_price=close,
                volume=volume,
            )
        )


if __name__ == "__main__":
    unittest.main()
