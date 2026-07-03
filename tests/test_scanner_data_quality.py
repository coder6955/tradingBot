import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.config import settings
from app.services.banknifty_intelligence_service import BankNiftyIntelligenceService
from app.services.database import Candle, get_session, init_db
from app.services.market_regime_service import MarketRegimeService
from app.services.option_chain_service import OptionChainService
from app.services.option_premium_confirmation_service import OptionPremiumConfirmationService
from app.services.kite_websocket_price_feed import WebSocketPremiumCandle
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.scanner_service import ScannerService
from app.services.trade_setup_service import OptionContract


class BankNiftyQualityFeed:
    def __init__(self, quote_payload: dict[str, object]) -> None:
        self.quote_payload = quote_payload

    def get_snapshot(self, symbol: str) -> dict[str, object]:
        base = {
            "symbol": symbol,
            "price": 58091.0 if symbol == "BANKNIFTY" else 25000.0,
            "source": "kite_quote",
            "is_real_data": True,
            "quote_timestamp": datetime.now().isoformat(sep=" "),
            "trend_bullish": True,
            "market_context": "strong",
            "rsi": 58,
            "adx": 26,
            "macd_positive": True,
            "ema_alignment": True,
            "vwap_above_price": True,
            "volume_confirmed": True,
            "vwap": 58020,
            "ema_9": 58080,
            "ema_21": 57980,
            "day_high": 58200,
            "day_low": 57800,
            "room_to_level_pct": 1.0,
        }
        if symbol == "INDIAVIX":
            return {**base, "symbol": symbol, "price": 14.5}
        return base

    def get_instruments(self, exchange: str):
        return [
            {
                "tradingsymbol": "BANKNIFTY26JUL58000CE",
                "exchange": "NFO",
                "instrument_token": 580001,
                "name": "BANKNIFTY",
                "expiry": "2099-07-26",
                "strike": 58000,
                "instrument_type": "CE",
                "lot_size": 15,
            },
            {
                "tradingsymbol": "BANKNIFTY26JUL58000PE",
                "exchange": "NFO",
                "instrument_token": 580002,
                "name": "BANKNIFTY",
                "expiry": "2099-07-26",
                "strike": 58000,
                "instrument_type": "PE",
                "lot_size": 15,
            },
        ]

    def get_quotes(self, instruments):
        return {instrument: {**self.quote_payload, "quote_timestamp": datetime.now().isoformat(sep=" ")} for instrument in instruments}

    def call_counts(self):
        return {"quote": 1, "historical_data": 0, "instruments": 1}


class FakeWebSocketPremiumFeed:
    def __init__(self, candles: list[WebSocketPremiumCandle], *, subscribed: bool = True, ticks_seen: int | None = None) -> None:
        self.candles = candles
        self.subscribed = subscribed
        self.ticks_seen = len(candles) if ticks_seen is None else ticks_seen

    def get_recent_premium_candles(self, instrument_token: int, limit: int = 10):
        return self.candles[-limit:]

    def get_current_session_premium_candles(self, instrument_token: int, limit: int = 10):
        return self.candles[-limit:]

    def premium_candle_status(self, instrument_token: int):
        return {
            "subscribed": self.subscribed,
            "ticks_seen": self.ticks_seen,
            "current_session_candle_count": len(self.candles),
            "current_building_candle": None,
            "last_completed_candle": None,
            "last_candle_age_seconds": 1 if self.candles else None,
        }


class ScannerDataQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "use_kite_market_data": settings.use_kite_market_data,
            "enable_day_type_filter": settings.enable_day_type_filter,
            "enable_outcome_learning_guard": settings.enable_outcome_learning_guard,
            "enable_strategy_edge_guard": settings.enable_strategy_edge_guard,
        }
        object.__setattr__(settings, "use_kite_market_data", True)
        object.__setattr__(settings, "enable_day_type_filter", False)
        object.__setattr__(settings, "enable_outcome_learning_guard", False)
        object.__setattr__(settings, "enable_strategy_edge_guard", False)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self._insert_premium_candles("BANKNIFTY26JUL58000CE")

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _insert_premium_candles(self, symbol: str) -> None:
        session = get_session()
        try:
            now = datetime.now()
            closes = [1083.0, 1089.0, 1117.15, 1087.1]
            for idx, close in enumerate(closes):
                session.add(
                    Candle(
                        symbol=symbol,
                        timeframe="5minute",
                        timestamp=now - timedelta(minutes=(len(closes) - idx)),
                        open_price=close - 5,
                        high_price=max(close + 2, 1117.15 if idx == 2 else close + 2),
                        low_price=close - 8,
                        close_price=close,
                        volume=1000,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _replace_premium_candles(self, symbol: str, *, start: datetime, closes: list[float], volumes: list[float] | None = None) -> None:
        session = get_session()
        try:
            session.query(Candle).filter(Candle.symbol == symbol).delete()
            volumes = volumes or [1000.0 for _ in closes]
            for idx, close in enumerate(closes):
                session.add(
                    Candle(
                        symbol=symbol,
                        timeframe="5minute",
                        timestamp=start + timedelta(minutes=idx * 5),
                        open_price=close - 2,
                        high_price=close + 2,
                        low_price=close - 3,
                        close_price=close,
                        volume=volumes[idx],
                    )
                )
            session.commit()
        finally:
            session.close()

    def _scanner_result(self, quote_payload: dict[str, object]) -> dict[str, object]:
        scanner = ScannerService(
            feed=BankNiftyQualityFeed(quote_payload),
            rejected_opportunity_repository=RejectedOpportunityRepository(),
        )
        return scanner.scan_with_diagnostics(symbols=["BANKNIFTY"], side="BUY", order_mode="paper")[0]

    def test_zero_live_quote_with_valid_premium_candles_rejects_before_fake_prices(self) -> None:
        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 0,
                "depth": {"buy": [{"price": 0}], "sell": [{"price": 0}]},
                "volume": 0,
                "oi": 0,
            }
        )

        self.assertFalse(result["passed"])
        self.assertIn("selected_option_quote_invalid", result["reasons"])
        self.assertEqual(result["factor_scores"]["prices"], {})
        self.assertEqual(result["factor_scores"]["data_quality"]["data_quality"], "invalid")
        self.assertEqual(result["factor_scores"]["option_premium_confirmation"]["details"]["last_close"], 1087.1)
        self.assertNotEqual(result["factor_scores"]["prices"].get("entry_price"), 0.05)

    def test_previous_day_option_candles_fail_premium_confirmation_during_current_session(self) -> None:
        yesterday = datetime.now() - timedelta(days=1, hours=1)
        self._replace_premium_candles(
            "BANKNIFTY26JUL58000CE",
            start=yesterday,
            closes=[100, 104, 108, 112, 116, 121, 126],
            volumes=[1000, 1000, 1000, 1000, 1000, 1000, 2000],
        )
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 126, 50000, 10000, 125, 126)

        result = OptionPremiumConfirmationService().evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("premium_candles_stale_or_missing", result["reasons"])
        self.assertFalse(result["details"]["premium_candle_freshness_passed"])
        self.assertNotEqual(result["details"]["premium_candle_session_date"], result["details"]["current_market_session_date"])

    def test_current_session_option_candles_pass_freshness(self) -> None:
        now = datetime.now()
        self._replace_premium_candles(
            "BANKNIFTY26JUL58000CE",
            start=now - timedelta(minutes=30),
            closes=[100, 102, 104, 106, 108, 110, 116],
            volumes=[1000, 1000, 1000, 1000, 1000, 1000, 2000],
        )
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 116, 50000, 10000, 115, 116)

        result = OptionPremiumConfirmationService().evaluate(contract=contract, side="BUY")

        self.assertTrue(result["details"]["premium_candle_freshness_passed"])
        self.assertEqual(result["details"]["premium_candle_session_date"], result["details"]["current_market_session_date"])
        self.assertIsNotNone(result["details"]["premium_candle_age_seconds"])

    def test_websocket_built_candle_is_accepted_if_fresh(self) -> None:
        session = get_session()
        try:
            session.query(Candle).filter(Candle.symbol == "BANKNIFTY26JUL58000CE").delete()
            session.commit()
        finally:
            session.close()
        now = datetime.now().replace(second=0, microsecond=0)
        candles = [
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=6), 100, 102, 99, 102, 1000, 3),
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=5), 103, 106, 102, 106, 1100, 3),
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=4), 107, 111, 106, 111, 1200, 3),
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=3), 112, 116, 111, 116, 1300, 3),
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=2), 117, 122, 116, 122, 1400, 3),
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=1), 123, 130, 122, 130, 3000, 3),
        ]
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 130, 50000, 10000, 129, 130)

        result = OptionPremiumConfirmationService(FakeWebSocketPremiumFeed(candles)).evaluate(contract=contract, side="BUY")

        self.assertEqual(result["details"]["premium_candle_source"], "websocket_builder")
        self.assertTrue(result["details"]["premium_candle_freshness_passed"])
        self.assertEqual(result["details"]["premium_candle_session_date"], result["details"]["current_market_session_date"])

    def test_fresh_websocket_candles_are_preferred_over_stale_stored_candles(self) -> None:
        yesterday = datetime.now() - timedelta(days=1, hours=1)
        self._replace_premium_candles(
            "BANKNIFTY26JUL58000CE",
            start=yesterday,
            closes=[100, 104, 108, 112, 116, 121, 126],
            volumes=[1000, 1000, 1000, 1000, 1000, 1000, 2000],
        )
        now = datetime.now().replace(second=0, microsecond=0)
        candles = [
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=3), 100, 104, 99, 104, 1000, 3),
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=2), 105, 110, 104, 110, 1200, 3),
            WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=1), 111, 118, 110, 118, 2500, 3),
        ]
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 118, 50000, 10000, 117, 118)

        result = OptionPremiumConfirmationService(FakeWebSocketPremiumFeed(candles)).evaluate(contract=contract, side="BUY")

        self.assertEqual(result["details"]["premium_candle_source"], "websocket_builder")
        self.assertTrue(result["details"]["premium_candle_freshness_passed"])

    def test_insufficient_current_session_websocket_candles_rejects_with_specific_reason(self) -> None:
        now = datetime.now().replace(second=0, microsecond=0)
        candles = [WebSocketPremiumCandle(580001, "1minute", now - timedelta(minutes=1), 100, 101, 99, 101, 1000, 2)]
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 101, 50000, 10000, 100, 101)
        session = get_session()
        try:
            session.query(Candle).filter(Candle.symbol == "BANKNIFTY26JUL58000CE").delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(FakeWebSocketPremiumFeed(candles)).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("premium_candle_builder_warming_up", result["reasons"])
        self.assertEqual(result["details"]["premium_confirmation_block_reason"], "premium_candle_builder_warming_up")

    def test_selected_option_not_subscribed_returns_specific_reason(self) -> None:
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 101, 50000, 10000, 100, 101)
        session = get_session()
        try:
            session.query(Candle).filter(Candle.symbol == "BANKNIFTY26JUL58000CE").delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(FakeWebSocketPremiumFeed([], subscribed=False)).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("selected_option_not_subscribed_for_candles", result["reasons"])

    def test_no_current_session_candles_rejects_as_insufficient(self) -> None:
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 101, 50000, 10000, 100, 101)
        session = get_session()
        try:
            session.query(Candle).filter(Candle.symbol == "BANKNIFTY26JUL58000CE").delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(FakeWebSocketPremiumFeed([], subscribed=True, ticks_seen=0)).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("insufficient_current_session_premium_candles", result["reasons"])

    def test_ticks_seen_without_candles_returns_specific_reason(self) -> None:
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 580001, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 101, 50000, 10000, 100, 101)
        session = get_session()
        try:
            session.query(Candle).filter(Candle.symbol == "BANKNIFTY26JUL58000CE").delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(FakeWebSocketPremiumFeed([], subscribed=True, ticks_seen=2)).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("websocket_ticks_available_but_no_candles_built", result["reasons"])

    def test_stale_premium_candles_cannot_create_signal(self) -> None:
        yesterday = datetime.now() - timedelta(days=1, hours=1)
        self._replace_premium_candles(
            "BANKNIFTY26JUL58000CE",
            start=yesterday,
            closes=[100, 104, 108, 112, 116, 121, 126],
            volumes=[1000, 1000, 1000, 1000, 1000, 1000, 2000],
        )

        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 126,
                "depth": {"buy": [{"price": 125}], "sell": [{"price": 126}]},
                "volume": 10000,
                "oi": 50000,
            }
        )

        self.assertFalse(result["passed"])
        self.assertIsNone(result["signal"])
        self.assertIn("premium_candles_stale_or_missing", result["reasons"])
        details = result["factor_scores"]["option_premium_confirmation"]["details"]
        self.assertFalse(details["premium_candle_freshness_passed"])
        self.assertEqual(details["premium_candle_rejection_reason"], "premium_candles_stale_or_missing")

    def test_premium_diagnostics_expose_age_and_session_date(self) -> None:
        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 1087.1,
                "depth": {"buy": [{"price": 1086}], "sell": [{"price": 1088}]},
                "volume": 10000,
                "oi": 50000,
            }
        )
        details = result["factor_scores"]["option_premium_confirmation"]["details"]

        self.assertIn("premium_candle_age_seconds", details)
        self.assertIn("premium_candle_session_date", details)
        self.assertIn("current_market_session_date", details)
        self.assertIn("premium_candle_freshness_passed", details)
        self.assertIn("premium_candle_rejection_reason", details)

    def test_invalid_bid_ask_oi_volume_blocks_before_price_calculation(self) -> None:
        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 1087.1,
                "depth": {"buy": [{"price": 0}], "sell": [{"price": 0}]},
                "volume": 0,
                "oi": 0,
            }
        )

        self.assertFalse(result["passed"])
        self.assertIn("selected_option_quote_invalid", result["reasons"])
        self.assertEqual(result["factor_scores"]["prices"], {})

    def test_quote_premium_mismatch_rejects_with_diagnostics(self) -> None:
        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 100,
                "depth": {"buy": [{"price": 99}], "sell": [{"price": 101}]},
                "volume": 10000,
                "oi": 50000,
            }
        )

        quality = result["factor_scores"]["data_quality"]
        self.assertFalse(result["passed"])
        self.assertIn("option_quote_premium_mismatch", result["reasons"])
        self.assertEqual(quality["selected_option"]["quote_key_used"], "NFO:BANKNIFTY26JUL58000CE")
        self.assertEqual(quality["selected_option"]["token_validation_status"], "matched")
        self.assertGreater(quality["mismatch"]["mismatch_pct"], settings.option_quote_premium_mismatch_tolerance_pct)

    def test_incomplete_oi_marks_pcr_and_max_pain_unavailable(self) -> None:
        selected = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 1, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 100, 0, 0, 99, 101)
        contracts = [
            selected,
            OptionContract("BANKNIFTY26JUL58000PE", "NFO", 2, "BANKNIFTY", "2099-07-26", 58000, "PE", 15, 100, 0, 0, 99, 101),
        ]

        result = OptionChainService().analyze(spot_price=58091, trend="bullish", side="BUY", selected=selected, contracts=contracts)

        self.assertIsNone(result["details"]["pcr_oi"])
        self.assertIsNone(result["details"]["max_pain"])
        self.assertFalse(result["details"]["oi_data_complete"])
        self.assertIn("PCR/max pain unavailable because OI data is incomplete", result["reasons"])

    def test_vix_unavailable_reduces_regime_without_fake_confidence(self) -> None:
        result = MarketRegimeService().evaluate(
            symbol="BANKNIFTY",
            trend="bullish",
            side="BUY",
            nifty={"trend_bullish": True},
            banknifty={"trend_bullish": True},
            vix=None,
        )

        self.assertIn("India VIX was unavailable; regime score reduced", result["reasons"])
        self.assertEqual(result["details"]["vix"], 0.0)

    def test_top_bank_unavailable_is_soft_context_not_hard_confidence(self) -> None:
        contract = OptionContract("BANKNIFTY26JUL58000CE", "NFO", 1, "BANKNIFTY", "2099-07-26", 58000, "CE", 15, 100, 50000, 10000, 99, 101)
        result = BankNiftyIntelligenceService().evaluate(
            trend="bullish",
            snapshot={"price": 58091, "day_high": 58200, "day_low": 57800, "vwap": 58020},
            market_snapshots={"BANKNIFTY": {"price": 58091}, "NIFTY": {"price": 25000}},
            contract=contract,
            chain_contracts=[contract],
            prices={"entry_price": 100, "target_1": 120},
            premium_eval={"passed": True, "details": {"last_close": 100, "option_vwap": 95}},
            day_type_eval={"passed": True, "details": {}},
        )

        self.assertIn("top bank constituent live data is incomplete", result["soft_reasons"])
        self.assertEqual(result["details"]["topBankAlignment"]["available"], 0)


if __name__ == "__main__":
    unittest.main()
