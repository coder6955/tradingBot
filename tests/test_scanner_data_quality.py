import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.config import settings
from app.services.banknifty_intelligence_service import BankNiftyIntelligenceService
from app.services.database import Candle, get_session, init_db
from app.services.market_regime_service import MarketRegimeService
from app.services.option_chain_service import OptionChainService
from app.services.option_premium_confirmation_service import (
    OptionPremiumConfirmationService,
)
from app.services.kite_websocket_price_feed import (
    KiteWebSocketPriceFeed,
    WebSocketPremiumCandle,
)
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
            "previous_day_close": 57900 if symbol == "BANKNIFTY" else 24900,
            "day_open": 57920 if symbol == "BANKNIFTY" else 24920,
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
        payload = dict(self.quote_payload)
        depth = (
            payload.get("depth", {}) if isinstance(payload.get("depth"), dict) else {}
        )
        payload["depth"] = {
            "buy": [
                {**row, "quantity": row.get("quantity", 1000)}
                for row in depth.get("buy", [])
            ],
            "sell": [
                {**row, "quantity": row.get("quantity", 1000)}
                for row in depth.get("sell", [])
            ],
        }
        return {
            instrument: {
                **payload,
                "quote_timestamp": datetime.now().isoformat(sep=" "),
            }
            for instrument in instruments
        }

    def call_counts(self):
        return {"quote": 1, "historical_data": 0, "instruments": 1}


class IncompleteCanonicalFeed(BankNiftyQualityFeed):
    def get_snapshot(self, symbol: str) -> dict[str, object]:
        return {
            **super().get_snapshot(symbol),
            "analysis_ready": False,
            "data_quality_reasons": [
                "insufficient_completed_5minute_structure_candles"
            ],
        }


class FakeWebSocketPremiumFeed:
    def __init__(
        self,
        candles: list[WebSocketPremiumCandle],
        *,
        subscribed: bool = True,
        ticks_seen: int | None = None,
    ) -> None:
        self.candles = candles
        self.subscribed = subscribed
        self.ticks_seen = len(candles) if ticks_seen is None else ticks_seen

    def get_recent_premium_candles(self, instrument_token: int, limit: int = 10):
        return self.candles[-limit:]

    def get_current_session_premium_candles(
        self, instrument_token: int, limit: int = 10
    ):
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


class FakeArmedEntryTimingService:
    def evaluate(self, **kwargs):
        return {
            "enabled": True,
            "state": "ARMED_FOR_ENTRY",
            "entry_timing_state": "ARMED_FOR_ENTRY",
            "passed": False,
            "reasons": ["waiting_for_entry_trigger", "premium_trigger_not_broken_yet"],
            "entry_timing_reason": "waiting_for_entry_trigger; premium_trigger_not_broken_yet",
            "entry_trigger_price": 1090.0,
            "current_premium": 1087.1,
            "premium_distance_to_trigger_pct": 0.266,
            "premium_move_from_base_pct": 2.0,
            "chase_risk": "normal",
            "remaining_risk_reward": 1.5,
            "target1_room_pct": 12.0,
            "entry_valid_until": datetime.now().isoformat(sep=" "),
            "entry_should_wait": True,
            "entry_should_reject_as_late": False,
            "spread_pct": 0.1,
        }


class FakeNoTradeEntryTimingService:
    def evaluate(self, **kwargs):
        return {
            "enabled": True,
            "state": "NO_TRADE",
            "entry_timing_state": "NO_TRADE",
            "passed": False,
            "reasons": ["entry_timing_price_inputs_missing"],
            "entry_timing_reason": "entry_timing_price_inputs_missing",
            "entry_trigger_price": None,
            "current_premium": kwargs["contract"].ask,
            "entry_should_wait": False,
            "entry_should_reject_as_late": False,
            "spread_pct": 0.1,
        }


class FakePendingPremiumConfirmationService:
    def evaluate(self, **kwargs):
        contract = kwargs["contract"]
        return {
            "enabled": True,
            "score": 0,
            "passed": False,
            "reasons": ["premium_candles_stale_or_missing"],
            "details": {
                "source": "unavailable",
                "premium_candle_source": "unavailable",
                "tradingsymbol": contract.tradingsymbol,
                "minimum_required_premium_candles": 3,
                "current_session_candle_count": 0,
                "premium_confirmation_ready": False,
                "premium_confirmation_block_reason": "premium_candles_stale_or_missing",
            },
        }


class FakeLiveGapBackfillService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def backfill_option_contract_live_gap(self, contract: OptionContract, **kwargs):
        self.calls.append({"contract": contract, "kwargs": kwargs})
        now = datetime.now().replace(second=0, microsecond=0)
        closes = [100, 102, 104, 106, 108, 110, 116]
        session = get_session()
        try:
            for idx, close in enumerate(closes):
                session.add(
                    Candle(
                        symbol=contract.tradingsymbol,
                        timeframe="1minute",
                        timestamp=now - timedelta(minutes=(len(closes) - idx)),
                        open_price=close - 1,
                        high_price=close + 2,
                        low_price=close - 2,
                        close_price=close,
                        volume=2000 if idx == len(closes) - 1 else 1000,
                    )
                )
            session.commit()
        finally:
            session.close()
        return {
            "status": "ok",
            "reason": kwargs.get("reason"),
            "historical_calls": 1,
            "inserted": len(closes),
        }


class FakeArmedEntryTracker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def register_from_scan(self, **kwargs):
        self.calls.append(kwargs)
        contract = kwargs["contract"]
        return {
            "registered": True,
            "setup_id": "armed-test-1",
            "latest_state": "ARMED_FOR_ENTRY",
            "latest_reason": "waiting_for_entry_trigger",
            "selected_option": {
                "tradingsymbol": contract.tradingsymbol,
                "instrument_token": contract.instrument_token,
                "option_type": contract.option_type,
                "strike": contract.strike,
                "expiry": contract.expiry,
            },
            "entry_trigger_price": kwargs["entry_timing"]["entry_trigger_price"],
            "current_premium": kwargs["entry_timing"]["current_premium"],
            "valid_until": kwargs["entry_timing"]["entry_valid_until"],
            "websocket_tracking_enabled": True,
            "paper_event_entry_enabled": True,
            "live_event_entry_blocked": False,
        }


class FakeBlockingRegimeFilterService:
    def evaluate(self, **kwargs):
        return {
            "enabled": True,
            "passed": False,
            "score": 40,
            "classification": "NO_BUY_REGIME",
            "hard_reasons": ["opening_trap_structure"],
            "soft_reasons": [],
            "details": {
                "verdict": "Do not buy Bank Nifty options in this regime: opening_trap_structure"
            },
        }


class ScannerDataQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "use_kite_market_data": settings.use_kite_market_data,
            "enable_day_type_filter": settings.enable_day_type_filter,
            "enable_outcome_learning_guard": settings.enable_outcome_learning_guard,
            "enable_strategy_edge_guard": settings.enable_strategy_edge_guard,
            "enable_volatility_edge": settings.enable_volatility_edge,
            "enable_volatility_edge_hard_gate": settings.enable_volatility_edge_hard_gate,
            "enable_banknifty_regime_filter": settings.enable_banknifty_regime_filter,
            "enable_kite_websocket": settings.enable_kite_websocket,
            "enable_early_armed_entry": settings.enable_early_armed_entry,
            "early_armed_entry_paper_only": settings.early_armed_entry_paper_only,
            "early_arm_min_score": settings.early_arm_min_score,
            "early_arm_trigger_buffer_pct": settings.early_arm_trigger_buffer_pct,
            "early_arm_allow_premium_pending": settings.early_arm_allow_premium_pending,
            "enable_live_option_candle_gap_backfill": settings.enable_live_option_candle_gap_backfill,
            "enable_on_demand_premium_candle_backfill": settings.enable_on_demand_premium_candle_backfill,
            "enable_websocket_candle_context_recovery": settings.enable_websocket_candle_context_recovery,
            "live_option_candle_backfill_timeframes": settings.live_option_candle_backfill_timeframes,
            "account_equity": settings.account_equity,
            "runtime_manual_override": settings.runtime_manual_override,
        }
        object.__setattr__(settings, "use_kite_market_data", True)
        object.__setattr__(settings, "enable_day_type_filter", False)
        object.__setattr__(settings, "enable_outcome_learning_guard", False)
        object.__setattr__(settings, "enable_strategy_edge_guard", False)
        object.__setattr__(settings, "enable_volatility_edge", True)
        object.__setattr__(settings, "enable_volatility_edge_hard_gate", False)
        object.__setattr__(settings, "enable_banknifty_regime_filter", False)
        object.__setattr__(settings, "enable_kite_websocket", False)
        object.__setattr__(settings, "enable_early_armed_entry", True)
        object.__setattr__(settings, "early_armed_entry_paper_only", True)
        object.__setattr__(settings, "early_arm_min_score", 75)
        object.__setattr__(settings, "early_arm_trigger_buffer_pct", 0.25)
        object.__setattr__(settings, "early_arm_allow_premium_pending", True)
        object.__setattr__(settings, "enable_live_option_candle_gap_backfill", True)
        object.__setattr__(settings, "enable_on_demand_premium_candle_backfill", True)
        object.__setattr__(settings, "enable_websocket_candle_context_recovery", True)
        object.__setattr__(
            settings, "live_option_candle_backfill_timeframes", "1minute"
        )
        object.__setattr__(settings, "account_equity", 1000000.0)
        object.__setattr__(settings, "runtime_manual_override", True)
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self._insert_underlying_candles()
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

    def _insert_underlying_candles(self) -> None:
        session = get_session()
        try:
            now = datetime.now().replace(second=0, microsecond=0)
            for timeframe, minutes in (("1minute", 1), ("5minute", 5)):
                for idx in range(24):
                    close = 57900.0 + idx * 8.0
                    session.add(
                        Candle(
                            symbol="BANKNIFTY",
                            timeframe=timeframe,
                            timestamp=now - timedelta(minutes=(24 - idx) * minutes),
                            open_price=close - 4,
                            high_price=close + 6,
                            low_price=close - 6,
                            close_price=close,
                            volume=1000 + idx * 10,
                        )
                    )
            session.commit()
        finally:
            session.close()

    def _replace_premium_candles(
        self,
        symbol: str,
        *,
        start: datetime,
        closes: list[float],
        volumes: list[float] | None = None,
    ) -> None:
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
        return scanner.scan_with_diagnostics(
            symbols=["BANKNIFTY"], side="BUY", order_mode="paper"
        )[0]

    def test_incomplete_canonical_candles_return_scored_diagnostic_without_crashing(
        self,
    ) -> None:
        scanner = ScannerService(
            feed=IncompleteCanonicalFeed({}),
            rejected_opportunity_repository=RejectedOpportunityRepository(),
        )

        result = scanner.scan_with_diagnostics(
            symbols=["BANKNIFTY"], side="BUY", order_mode="paper"
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 0)
        self.assertFalse(result[0]["passed"])
        self.assertIn(
            "canonical completed-candle analysis was not ready", result[0]["reasons"]
        )

    def test_zero_live_quote_with_valid_premium_candles_rejects_before_fake_prices(
        self,
    ) -> None:
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
        self.assertEqual(
            result["factor_scores"]["data_quality"]["data_quality"], "invalid"
        )
        self.assertEqual(
            result["factor_scores"]["option_premium_confirmation"]["details"][
                "last_close"
            ],
            1087.1,
        )
        self.assertNotEqual(result["factor_scores"]["prices"].get("entry_price"), 0.05)

    def test_scanner_diagnostics_exposes_entry_timing_fields(self) -> None:
        originals = {
            "max_premium_move_from_base_pct": settings.max_premium_move_from_base_pct,
            "min_target1_room_pct": settings.min_target1_room_pct,
        }
        try:
            object.__setattr__(settings, "max_premium_move_from_base_pct", 100.0)
            object.__setattr__(settings, "min_target1_room_pct", 1.0)
            result = self._scanner_result(
                {
                    "instrument_token": 580001,
                    "last_price": 1087.1,
                    "depth": {"buy": [{"price": 1086.0}], "sell": [{"price": 1087.1}]},
                    "volume": 100000,
                    "oi": 100000,
                }
            )
        finally:
            for key, value in originals.items():
                object.__setattr__(settings, key, value)

        self.assertIn("entry_timing_state", result)
        self.assertIn("entry_trigger_price", result)
        self.assertIn("current_premium", result)
        self.assertIn("premium_distance_to_trigger_pct", result)
        self.assertIn("premium_move_from_base_pct", result)
        self.assertIn("chase_risk", result)
        self.assertIn("remaining_risk_reward", result)
        self.assertIn("target1_room_pct", result)
        self.assertIn("entry_timing_reason", result)
        self.assertIn("entry_valid_until", result)
        self.assertIn("entry_should_wait", result)
        self.assertIn("entry_should_reject_as_late", result)
        self.assertIn("entry_timing", result["factor_scores"])

    def test_scanner_diagnostics_exposes_volatility_edge_fields(self) -> None:
        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 1087.1,
                "depth": {"buy": [{"price": 1086.0}], "sell": [{"price": 1087.1}]},
                "volume": 100000,
                "oi": 100000,
            }
        )

        self.assertIn("volatility_edge", result["factor_scores"])
        self.assertIn("volatility_edge_score", result)
        self.assertIn("volatility_edge_classification", result)
        self.assertIn("volatility_edge_for_option_buying", result)
        self.assertIn("iv_rank", result)
        self.assertIn("iv_percentile", result)
        self.assertIn("iv_to_rv_ratio", result)
        self.assertIn("expected_move_coverage_iv", result)
        self.assertIn("iv_expansion_supported", result)
        self.assertIn("iv_crush_risk", result)
        self.assertIn("volatility_edge_reasons", result)

    def test_volatility_edge_does_not_block_when_hard_gate_disabled(self) -> None:
        object.__setattr__(settings, "enable_volatility_edge_hard_gate", False)

        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 1087.1,
                "depth": {"buy": [{"price": 1086.0}], "sell": [{"price": 1087.1}]},
                "volume": 100000,
                "oi": 100000,
            }
        )

        self.assertIn(
            "volatility_edge_deferred_outside_live_decision_path",
            result["factor_scores"]["volatility_edge"]["reasons"],
        )
        self.assertNotIn(
            "volatility_edge_deferred_outside_live_decision_path", result["reasons"]
        )

    def test_volatility_edge_remains_shadow_when_legacy_hard_gate_is_enabled(
        self,
    ) -> None:
        object.__setattr__(settings, "enable_volatility_edge_hard_gate", True)

        result = self._scanner_result(
            {
                "instrument_token": 580001,
                "last_price": 1087.1,
                "depth": {"buy": [{"price": 1086.0}], "sell": [{"price": 1087.1}]},
                "volume": 100000,
                "oi": 100000,
            }
        )

        self.assertIn(
            "volatility_edge_deferred_outside_live_decision_path",
            result["factor_scores"]["volatility_edge"]["reasons"],
        )
        self.assertNotIn(
            "volatility_edge_deferred_outside_live_decision_path", result["reasons"]
        )

    def test_rejected_opportunity_metadata_includes_volatility_edge(self) -> None:
        repo = RejectedOpportunityRepository()
        scanner = ScannerService(
            feed=BankNiftyQualityFeed(
                {
                    "instrument_token": 580001,
                    "last_price": 0,
                    "depth": {"buy": [{"price": 0}], "sell": [{"price": 0}]},
                    "volume": 0,
                    "oi": 0,
                }
            ),
            rejected_opportunity_repository=repo,
        )

        scanner.scan_with_diagnostics(
            symbols=["BANKNIFTY"], side="BUY", order_mode="paper"
        )
        rows = repo.list_rejections(symbol="BANKNIFTY", limit=1)
        factors = json.loads(rows[0].factor_scores_json)

        self.assertIn("volatility_edge", factors)
        self.assertIn("reasons", factors["volatility_edge"])

    def test_duplicate_banknifty_regime_filter_is_shadow_only(self) -> None:
        object.__setattr__(settings, "enable_banknifty_regime_filter", True)
        scanner = ScannerService(
            feed=BankNiftyQualityFeed(
                {
                    "instrument_token": 580001,
                    "last_price": 1087.1,
                    "depth": {"buy": [{"price": 1086.0}], "sell": [{"price": 1087.1}]},
                    "volume": 100000,
                    "oi": 100000,
                }
            ),
            rejected_opportunity_repository=RejectedOpportunityRepository(),
            banknifty_regime_filter_service=FakeBlockingRegimeFilterService(),
        )

        result = scanner.scan_with_diagnostics(
            symbols=["BANKNIFTY"], side="BUY", order_mode="paper"
        )[0]

        self.assertFalse(result["passed"])
        self.assertNotIn("opening_trap_structure", result["reasons"])
        self.assertEqual(
            result["factor_scores"]["banknifty_regime_filter"]["classification"],
            "NO_BUY_REGIME",
        )

    def test_scanner_registers_armed_setup_when_entry_timing_is_armed(self) -> None:
        originals = {
            "enable_option_premium_confirmation": settings.enable_option_premium_confirmation,
            "enable_banknifty_intelligence": settings.enable_banknifty_intelligence,
            "enable_kite_websocket": settings.enable_kite_websocket,
            "min_signal_score": settings.min_signal_score,
            "min_market_regime_score": settings.min_market_regime_score,
            "min_price_action_score": settings.min_price_action_score,
            "min_option_chain_score": settings.min_option_chain_score,
            "min_option_quality_score": settings.min_option_quality_score,
            "min_risk_reward": settings.min_risk_reward,
            "min_directional_room_pct": settings.min_directional_room_pct,
        }
        tracker = FakeArmedEntryTracker()
        try:
            object.__setattr__(settings, "enable_option_premium_confirmation", False)
            object.__setattr__(settings, "enable_banknifty_intelligence", True)
            object.__setattr__(settings, "enable_kite_websocket", True)
            object.__setattr__(settings, "min_signal_score", 0)
            object.__setattr__(settings, "min_market_regime_score", 1)
            object.__setattr__(settings, "min_price_action_score", 1)
            object.__setattr__(settings, "min_option_chain_score", 1)
            object.__setattr__(settings, "min_option_quality_score", 1)
            object.__setattr__(settings, "min_risk_reward", 0.0)
            object.__setattr__(settings, "min_directional_room_pct", 0.0)
            scanner = ScannerService(
                feed=BankNiftyQualityFeed(
                    {
                        "instrument_token": 580001,
                        "last_price": 1087.1,
                        "depth": {
                            "buy": [{"price": 1086.0}],
                            "sell": [{"price": 1087.1}],
                        },
                        "volume": 100000,
                        "oi": 100000,
                    }
                ),
                rejected_opportunity_repository=RejectedOpportunityRepository(),
                entry_timing_service=FakeArmedEntryTimingService(),
                armed_entry_tracker=tracker,
            )

            result = scanner.scan_with_diagnostics(
                symbols=["BANKNIFTY"], side="BUY", order_mode="paper"
            )[0]
        finally:
            for key, value in originals.items():
                object.__setattr__(settings, key, value)

        self.assertFalse(result["passed"])
        self.assertEqual(result["entry_timing_state"], "ARMED_FOR_ENTRY")
        self.assertEqual(
            result["armed_setup_id"],
            "armed-test-1",
            result["factor_scores"].get("armed_entry"),
        )
        self.assertTrue(result["websocket_tracking_enabled"])
        self.assertEqual(len(tracker.calls), 1)
        self.assertEqual(tracker.calls[0]["contract"].instrument_token, 580001)

    def test_scanner_early_arms_when_only_premium_confirmation_is_pending(self) -> None:
        originals = {
            "enable_option_premium_confirmation": settings.enable_option_premium_confirmation,
            "enable_banknifty_intelligence": settings.enable_banknifty_intelligence,
            "enable_kite_websocket": settings.enable_kite_websocket,
            "min_signal_score": settings.min_signal_score,
            "early_arm_min_score": settings.early_arm_min_score,
            "min_market_regime_score": settings.min_market_regime_score,
            "min_price_action_score": settings.min_price_action_score,
            "min_option_chain_score": settings.min_option_chain_score,
            "min_option_quality_score": settings.min_option_quality_score,
            "min_risk_reward": settings.min_risk_reward,
            "min_directional_room_pct": settings.min_directional_room_pct,
        }
        tracker = FakeArmedEntryTracker()
        try:
            object.__setattr__(settings, "enable_option_premium_confirmation", True)
            object.__setattr__(settings, "enable_banknifty_intelligence", True)
            object.__setattr__(settings, "enable_kite_websocket", True)
            object.__setattr__(settings, "min_signal_score", 0)
            object.__setattr__(settings, "early_arm_min_score", 1)
            object.__setattr__(settings, "min_market_regime_score", 1)
            object.__setattr__(settings, "min_price_action_score", 1)
            object.__setattr__(settings, "min_option_chain_score", 1)
            object.__setattr__(settings, "min_option_quality_score", 1)
            object.__setattr__(settings, "min_risk_reward", 0.0)
            object.__setattr__(settings, "min_directional_room_pct", 0.0)
            scanner = ScannerService(
                feed=BankNiftyQualityFeed(
                    {
                        "instrument_token": 580001,
                        "last_price": 1087.1,
                        "depth": {
                            "buy": [{"price": 1086.0}],
                            "sell": [{"price": 1087.1}],
                        },
                        "volume": 100000,
                        "oi": 100000,
                    }
                ),
                rejected_opportunity_repository=RejectedOpportunityRepository(),
                option_premium_confirmation_service=FakePendingPremiumConfirmationService(),
                entry_timing_service=FakeNoTradeEntryTimingService(),
                armed_entry_tracker=tracker,
            )

            result = scanner.scan_with_diagnostics(
                symbols=["BANKNIFTY"], side="BUY", order_mode="paper"
            )[0]
        finally:
            for key, value in originals.items():
                object.__setattr__(settings, key, value)

        self.assertFalse(result["passed"])
        self.assertEqual(result["entry_timing_state"], "ARMED_FOR_ENTRY")
        self.assertEqual(
            result["armed_setup_id"],
            "armed-test-1",
            result["factor_scores"].get("armed_entry"),
        )
        self.assertTrue(result["factor_scores"]["armed_entry"]["early_arm"])
        self.assertTrue(tracker.calls[0]["entry_timing"]["early_arm"])
        self.assertGreater(
            tracker.calls[0]["entry_timing"]["entry_trigger_price"],
            tracker.calls[0]["entry_timing"]["current_premium"],
        )
        self.assertIn(
            "premium_confirmation_pending", tracker.calls[0]["entry_timing"]["reasons"]
        )

    def test_scanner_early_arm_ignores_shadow_regime_veto(self) -> None:
        originals = {
            "enable_option_premium_confirmation": settings.enable_option_premium_confirmation,
            "enable_banknifty_regime_filter": settings.enable_banknifty_regime_filter,
            "enable_banknifty_intelligence": settings.enable_banknifty_intelligence,
            "enable_kite_websocket": settings.enable_kite_websocket,
            "min_signal_score": settings.min_signal_score,
            "early_arm_min_score": settings.early_arm_min_score,
            "min_market_regime_score": settings.min_market_regime_score,
            "min_price_action_score": settings.min_price_action_score,
            "min_option_chain_score": settings.min_option_chain_score,
            "min_option_quality_score": settings.min_option_quality_score,
            "min_risk_reward": settings.min_risk_reward,
            "min_directional_room_pct": settings.min_directional_room_pct,
        }
        tracker = FakeArmedEntryTracker()
        try:
            object.__setattr__(settings, "enable_option_premium_confirmation", True)
            object.__setattr__(settings, "enable_banknifty_regime_filter", True)
            object.__setattr__(settings, "enable_banknifty_intelligence", True)
            object.__setattr__(settings, "enable_kite_websocket", True)
            object.__setattr__(settings, "min_signal_score", 0)
            object.__setattr__(settings, "early_arm_min_score", 1)
            object.__setattr__(settings, "min_market_regime_score", 1)
            object.__setattr__(settings, "min_price_action_score", 1)
            object.__setattr__(settings, "min_option_chain_score", 1)
            object.__setattr__(settings, "min_option_quality_score", 1)
            object.__setattr__(settings, "min_risk_reward", 0.0)
            object.__setattr__(settings, "min_directional_room_pct", 0.0)
            scanner = ScannerService(
                feed=BankNiftyQualityFeed(
                    {
                        "instrument_token": 580001,
                        "last_price": 1087.1,
                        "depth": {
                            "buy": [{"price": 1086.0}],
                            "sell": [{"price": 1087.1}],
                        },
                        "volume": 100000,
                        "oi": 100000,
                    }
                ),
                rejected_opportunity_repository=RejectedOpportunityRepository(),
                option_premium_confirmation_service=FakePendingPremiumConfirmationService(),
                entry_timing_service=FakeNoTradeEntryTimingService(),
                banknifty_regime_filter_service=FakeBlockingRegimeFilterService(),
                armed_entry_tracker=tracker,
            )

            result = scanner.scan_with_diagnostics(
                symbols=["BANKNIFTY"], side="BUY", order_mode="paper"
            )[0]
        finally:
            for key, value in originals.items():
                object.__setattr__(settings, key, value)

        self.assertFalse(result["passed"])
        self.assertNotIn("opening_trap_structure", result["reasons"])
        self.assertEqual(len(tracker.calls), 1)
        self.assertTrue(result["factor_scores"]["armed_entry"]["registered"])
        self.assertEqual(result["entry_timing_state"], "ARMED_FOR_ENTRY")

    def test_scanner_early_arm_is_paper_only_by_default(self) -> None:
        scanner = ScannerService(
            feed=BankNiftyQualityFeed({}),
            rejected_opportunity_repository=RejectedOpportunityRepository(),
        )
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            1087.1,
            100000,
            100000,
            1086.0,
            1087.1,
        )
        result = scanner._maybe_register_armed_entry(
            symbol="BANKNIFTY",
            side="BUY",
            trend="bullish",
            contract=contract,
            prices={
                "entry_price": 1087.1,
                "stop_loss": 1000.0,
                "target_1": 1250.0,
                "target_2": 1300.0,
                "target_3": 1350.0,
                "risk_reward": 1.8,
            },
            entry_timing_eval={
                "entry_timing_state": "NO_TRADE",
                "reasons": ["entry_timing_price_inputs_missing"],
            },
            score=90,
            probability=0.8,
            confidence=0.9,
            quantity=15,
            factor_scores={
                "data_quality": {"passed": True},
                "data_freshness": {"passed": True},
                "option_quality": {"passed": True},
                "market_regime": {"passed": True},
                "price_action": {"passed": True},
                "banknifty_intelligence": {"passed": True},
                "option_premium_confirmation": {
                    "passed": False,
                    "reasons": ["premium_candles_stale_or_missing"],
                    "details": {},
                },
            },
            order_mode="live",
            gate_failures=["premium_candles_stale_or_missing"],
        )

        self.assertEqual(result["reason"], "early_arming_paper_only")

    def test_early_arm_score_remains_diagnostic_without_risk_authority(self) -> None:
        tracker = FakeArmedEntryTracker()
        scanner = ScannerService(
            feed=BankNiftyQualityFeed({}),
            rejected_opportunity_repository=RejectedOpportunityRepository(),
            armed_entry_tracker=tracker,
        )
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            30,
            100,
            100000,
            100000,
            99.5,
            100,
            bid_quantity=300,
            ask_quantity=300,
        )
        originals = {
            "enable_kite_websocket": settings.enable_kite_websocket,
            "min_signal_score": settings.min_signal_score,
            "early_arm_min_score": settings.early_arm_min_score,
            "early_armed_entry_paper_only": settings.early_armed_entry_paper_only,
        }
        try:
            object.__setattr__(settings, "enable_kite_websocket", True)
            object.__setattr__(settings, "min_signal_score", 80)
            object.__setattr__(settings, "early_arm_min_score", 75)
            object.__setattr__(settings, "early_armed_entry_paper_only", True)
            result = scanner._maybe_register_armed_entry(
                symbol="BANKNIFTY",
                side="BUY",
                trend="bullish",
                contract=contract,
                prices={
                    "entry_price": 100,
                    "stop_loss": 90,
                    "target_1": 120,
                    "target_2": 130,
                    "target_3": 140,
                    "risk_reward": 2,
                },
                entry_timing_eval={
                    "entry_timing_state": "NO_TRADE",
                    "reasons": ["premium_candles_stale_or_missing"],
                },
                score=10,
                probability=0.7,
                confidence=0.76,
                quantity=30,
                factor_scores={
                    "data_quality": {"passed": True},
                    "data_freshness": {"passed": True},
                    "option_quality": {"passed": True},
                    "market_regime": {"passed": True},
                    "price_action": {"passed": True},
                    "banknifty_intelligence": {"passed": True},
                    "option_premium_confirmation": {"passed": False, "details": {}},
                },
                order_mode="paper",
                gate_failures=["premium_candles_stale_or_missing"],
            )
        finally:
            for key, value in originals.items():
                object.__setattr__(settings, key, value)

        self.assertTrue(result["registered"])
        self.assertTrue(result["early_arm"])
        self.assertEqual(len(tracker.calls), 1)

    def test_early_arm_downgrades_unvalidated_higher_risk_to_base(self) -> None:
        tracker = FakeArmedEntryTracker()
        scanner = ScannerService(
            feed=BankNiftyQualityFeed({}),
            rejected_opportunity_repository=RejectedOpportunityRepository(),
            armed_entry_tracker=tracker,
            session_eligibility_provider=lambda: True,
        )
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            30,
            100,
            100000,
            100000,
            99.5,
            100,
            bid_quantity=300,
            ask_quantity=300,
        )
        factors = {
            "data_quality": {"passed": True},
            "data_freshness": {"passed": True},
            "multi_timeframe": {"passed": True},
            "option_quality": {"passed": True},
            "option_premium_confirmation": {"passed": False, "details": {}},
            "risk_request": {"requested_tier": "TIER_4_EXCEPTIONAL"},
        }
        original_websocket = settings.enable_kite_websocket
        try:
            object.__setattr__(settings, "enable_kite_websocket", True)
            result = scanner._maybe_register_armed_entry(
                symbol="BANKNIFTY",
                side="BUY",
                trend="bullish",
                contract=contract,
                prices={
                    "entry_price": 100,
                    "stop_loss": 90,
                    "target_1": 120,
                    "target_2": 130,
                    "target_3": 140,
                    "risk_reward": 2,
                },
                entry_timing_eval={
                    "entry_timing_state": "NO_TRADE",
                    "reasons": ["premium_candles_stale_or_missing"],
                },
                score=99,
                probability=None,
                confidence=0.99,
                quantity=30,
                factor_scores=factors,
                order_mode="paper",
                gate_failures=["premium_candles_stale_or_missing"],
            )
        finally:
            object.__setattr__(settings, "enable_kite_websocket", original_websocket)
        self.assertTrue(result["registered"])
        self.assertEqual(factors["risk_request"]["requested_tier"], "TIER_1_BASE")
        self.assertEqual(
            factors["risk_request"]["shadow_requested_tier"], "TIER_4_EXCEPTIONAL"
        )
        self.assertFalse(
            factors["risk_request"]["higher_tier_has_active_order_authority"]
        )

    def test_previous_day_option_candles_fail_premium_confirmation_during_current_session(
        self,
    ) -> None:
        yesterday = datetime.now() - timedelta(days=1, hours=1)
        self._replace_premium_candles(
            "BANKNIFTY26JUL58000CE",
            start=yesterday,
            closes=[100, 104, 108, 112, 116, 121, 126],
            volumes=[1000, 1000, 1000, 1000, 1000, 1000, 2000],
        )
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            126,
            50000,
            10000,
            125,
            126,
        )

        result = OptionPremiumConfirmationService().evaluate(
            contract=contract, side="BUY"
        )

        self.assertFalse(result["passed"])
        self.assertIn("premium_candles_stale_or_missing", result["reasons"])
        self.assertFalse(result["details"]["premium_candle_freshness_passed"])
        self.assertNotEqual(
            result["details"]["premium_candle_session_date"],
            result["details"]["current_market_session_date"],
        )

    def test_current_session_option_candles_pass_freshness(self) -> None:
        now = datetime.now()
        self._replace_premium_candles(
            "BANKNIFTY26JUL58000CE",
            start=now - timedelta(minutes=30),
            closes=[100, 102, 104, 106, 108, 110, 116],
            volumes=[1000, 1000, 1000, 1000, 1000, 1000, 2000],
        )
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            116,
            50000,
            10000,
            115,
            116,
        )

        result = OptionPremiumConfirmationService().evaluate(
            contract=contract, side="BUY"
        )

        self.assertTrue(result["details"]["premium_candle_freshness_passed"])
        self.assertEqual(
            result["details"]["premium_candle_session_date"],
            result["details"]["current_market_session_date"],
        )
        self.assertIsNotNone(result["details"]["premium_candle_age_seconds"])

    def test_on_demand_live_gap_backfill_repairs_missing_premium_candles(self) -> None:
        session = get_session()
        try:
            session.query(Candle).filter(
                Candle.symbol == "BANKNIFTY26JUL58000CE"
            ).delete()
            session.commit()
        finally:
            session.close()
        backfill_service = FakeLiveGapBackfillService()
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            116,
            50000,
            10000,
            115,
            116,
        )

        result = OptionPremiumConfirmationService(
            live_gap_backfill_service=backfill_service
        ).evaluate(contract=contract, side="BUY")

        self.assertEqual(len(backfill_service.calls), 1)
        self.assertEqual(backfill_service.calls[0]["kwargs"]["timeframes"], ["1minute"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["details"]["premium_candle_source"], "stored_candles")
        self.assertEqual(
            result["details"]["live_candle_gap_backfill_timeframe"], "1minute"
        )
        self.assertEqual(result["details"]["live_candle_gap_backfill"]["inserted"], 7)

    def test_on_demand_backfill_restores_websocket_candle_context_without_fake_ticks(
        self,
    ) -> None:
        session = get_session()
        try:
            session.query(Candle).filter(
                Candle.symbol == "BANKNIFTY26JUL58000CE"
            ).delete()
            session.commit()
        finally:
            session.close()
        now = datetime.now().replace(second=0, microsecond=0)
        feed = KiteWebSocketPriceFeed(
            api_key="k", access_token="t", ticker_factory=None, clock=lambda: now
        )
        backfill_service = FakeLiveGapBackfillService()
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            116,
            50000,
            10000,
            115,
            116,
        )

        result = OptionPremiumConfirmationService(
            feed, live_gap_backfill_service=backfill_service
        ).evaluate(contract=contract, side="BUY")

        self.assertTrue(result["passed"])
        self.assertEqual(feed.tick_count(580001), 0)
        self.assertEqual(
            result["details"]["premium_candle_source"], "websocket_builder"
        )
        recovery = result["details"]["websocket_candle_context_recovery"]
        self.assertEqual(recovery["status"], "ok")
        self.assertEqual(recovery["inserted_candles"], 7)
        self.assertFalse(recovery["tick_replay"])
        candles = feed.get_current_session_premium_candles(580001)
        self.assertEqual(len(candles), 7)
        self.assertEqual(candles[-1].source, "kite_historical_context_recovered")

    def test_websocket_built_candle_is_accepted_if_fresh(self) -> None:
        session = get_session()
        try:
            session.query(Candle).filter(
                Candle.symbol == "BANKNIFTY26JUL58000CE"
            ).delete()
            session.commit()
        finally:
            session.close()
        now = datetime.now().replace(second=0, microsecond=0)
        candles = [
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=6),
                100,
                102,
                99,
                102,
                1000,
                3,
            ),
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=5),
                103,
                106,
                102,
                106,
                1100,
                3,
            ),
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=4),
                107,
                111,
                106,
                111,
                1200,
                3,
            ),
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=3),
                112,
                116,
                111,
                116,
                1300,
                3,
            ),
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=2),
                117,
                122,
                116,
                122,
                1400,
                3,
            ),
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=1),
                123,
                130,
                122,
                130,
                3000,
                3,
            ),
        ]
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            130,
            50000,
            10000,
            129,
            130,
        )

        result = OptionPremiumConfirmationService(
            FakeWebSocketPremiumFeed(candles)
        ).evaluate(contract=contract, side="BUY")

        self.assertEqual(
            result["details"]["premium_candle_source"], "websocket_builder"
        )
        self.assertTrue(result["details"]["premium_candle_freshness_passed"])
        self.assertEqual(
            result["details"]["premium_candle_session_date"],
            result["details"]["current_market_session_date"],
        )

    def test_fresh_websocket_candles_are_preferred_over_stale_stored_candles(
        self,
    ) -> None:
        yesterday = datetime.now() - timedelta(days=1, hours=1)
        self._replace_premium_candles(
            "BANKNIFTY26JUL58000CE",
            start=yesterday,
            closes=[100, 104, 108, 112, 116, 121, 126],
            volumes=[1000, 1000, 1000, 1000, 1000, 1000, 2000],
        )
        now = datetime.now().replace(second=0, microsecond=0)
        candles = [
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=3),
                100,
                104,
                99,
                104,
                1000,
                3,
            ),
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=2),
                105,
                110,
                104,
                110,
                1200,
                3,
            ),
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=1),
                111,
                118,
                110,
                118,
                2500,
                3,
            ),
        ]
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            118,
            50000,
            10000,
            117,
            118,
        )

        result = OptionPremiumConfirmationService(
            FakeWebSocketPremiumFeed(candles)
        ).evaluate(contract=contract, side="BUY")

        self.assertEqual(
            result["details"]["premium_candle_source"], "websocket_builder"
        )
        self.assertTrue(result["details"]["premium_candle_freshness_passed"])

    def test_insufficient_current_session_websocket_candles_rejects_with_specific_reason(
        self,
    ) -> None:
        now = datetime.now().replace(second=0, microsecond=0)
        candles = [
            WebSocketPremiumCandle(
                580001,
                "1minute",
                now - timedelta(minutes=1),
                100,
                101,
                99,
                101,
                1000,
                2,
            )
        ]
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            101,
            50000,
            10000,
            100,
            101,
        )
        session = get_session()
        try:
            session.query(Candle).filter(
                Candle.symbol == "BANKNIFTY26JUL58000CE"
            ).delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(
            FakeWebSocketPremiumFeed(candles)
        ).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("premium_candle_builder_warming_up", result["reasons"])
        self.assertEqual(
            result["details"]["premium_confirmation_block_reason"],
            "premium_candle_builder_warming_up",
        )

    def test_selected_option_not_subscribed_returns_specific_reason(self) -> None:
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            101,
            50000,
            10000,
            100,
            101,
        )
        session = get_session()
        try:
            session.query(Candle).filter(
                Candle.symbol == "BANKNIFTY26JUL58000CE"
            ).delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(
            FakeWebSocketPremiumFeed([], subscribed=False)
        ).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("selected_option_not_subscribed_for_candles", result["reasons"])

    def test_no_current_session_candles_rejects_as_insufficient(self) -> None:
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            101,
            50000,
            10000,
            100,
            101,
        )
        session = get_session()
        try:
            session.query(Candle).filter(
                Candle.symbol == "BANKNIFTY26JUL58000CE"
            ).delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(
            FakeWebSocketPremiumFeed([], subscribed=True, ticks_seen=0)
        ).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn("insufficient_current_session_premium_candles", result["reasons"])

    def test_ticks_seen_without_candles_returns_specific_reason(self) -> None:
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            101,
            50000,
            10000,
            100,
            101,
        )
        session = get_session()
        try:
            session.query(Candle).filter(
                Candle.symbol == "BANKNIFTY26JUL58000CE"
            ).delete()
            session.commit()
        finally:
            session.close()

        result = OptionPremiumConfirmationService(
            FakeWebSocketPremiumFeed([], subscribed=True, ticks_seen=2)
        ).evaluate(contract=contract, side="BUY")

        self.assertFalse(result["passed"])
        self.assertIn(
            "websocket_ticks_available_but_no_candles_built", result["reasons"]
        )

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
        self.assertEqual(
            details["premium_candle_rejection_reason"],
            "premium_candles_stale_or_missing",
        )

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
        self.assertEqual(
            quality["selected_option"]["quote_key_used"], "NFO:BANKNIFTY26JUL58000CE"
        )
        self.assertEqual(
            quality["selected_option"]["token_validation_status"], "matched"
        )
        self.assertGreater(
            quality["mismatch"]["mismatch_pct"],
            settings.option_quote_premium_mismatch_tolerance_pct,
        )

    def test_incomplete_oi_marks_pcr_and_max_pain_unavailable(self) -> None:
        selected = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            1,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            100,
            0,
            0,
            99,
            101,
        )
        contracts = [
            selected,
            OptionContract(
                "BANKNIFTY26JUL58000PE",
                "NFO",
                2,
                "BANKNIFTY",
                "2099-07-26",
                58000,
                "PE",
                15,
                100,
                0,
                0,
                99,
                101,
            ),
        ]

        result = OptionChainService().analyze(
            spot_price=58091,
            trend="bullish",
            side="BUY",
            selected=selected,
            contracts=contracts,
        )

        self.assertIsNone(result["details"]["pcr_oi"])
        self.assertIsNone(result["details"]["max_pain"])
        self.assertFalse(result["details"]["oi_data_complete"])
        self.assertIn(
            "PCR/max pain unavailable because OI data is incomplete", result["reasons"]
        )

    def test_vix_unavailable_reduces_regime_without_fake_confidence(self) -> None:
        result = MarketRegimeService().evaluate(
            symbol="BANKNIFTY",
            trend="bullish",
            side="BUY",
            nifty={"trend_bullish": True},
            banknifty={"trend_bullish": True},
            vix=None,
        )

        self.assertIn(
            "India VIX was unavailable; regime score reduced", result["reasons"]
        )
        self.assertEqual(result["details"]["vix"], 0.0)

    def test_top_bank_unavailable_is_soft_context_not_hard_confidence(self) -> None:
        contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            1,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            100,
            50000,
            10000,
            99,
            101,
        )
        result = BankNiftyIntelligenceService().evaluate(
            trend="bullish",
            snapshot={
                "price": 58091,
                "day_high": 58200,
                "day_low": 57800,
                "vwap": 58020,
            },
            market_snapshots={"BANKNIFTY": {"price": 58091}, "NIFTY": {"price": 25000}},
            contract=contract,
            chain_contracts=[contract],
            prices={"entry_price": 100, "target_1": 120},
            premium_eval={
                "passed": True,
                "details": {"last_close": 100, "option_vwap": 95},
            },
            day_type_eval={"passed": True, "details": {}},
        )

        self.assertIn(
            "top bank constituent live data is incomplete", result["soft_reasons"]
        )
        self.assertEqual(result["details"]["topBankAlignment"]["available"], 0)


if __name__ == "__main__":
    unittest.main()
