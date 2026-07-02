import os
import tempfile
import unittest

from app.services.banknifty_intelligence_service import BankNiftyIntelligenceService
from app.services.database import init_db
from app.services.market_data_service import MarketDataService
from app.services.time_utils import ist_today
from app.services.trade_setup_service import OptionContract


class BankNiftyIntelligenceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        from app.services import banknifty_intelligence_service

        self._original_first_trade_time = banknifty_intelligence_service.settings.banknifty_first_trade_time
        object.__setattr__(banknifty_intelligence_service.settings, "banknifty_first_trade_time", "00:00")
        self._seed_banknifty_candles(last_close=58220)

    def tearDown(self) -> None:
        from app.services import banknifty_intelligence_service

        object.__setattr__(banknifty_intelligence_service.settings, "banknifty_first_trade_time", self._original_first_trade_time)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_ce_allowed_when_banks_relative_strength_premium_and_day_align(self) -> None:
        result = self._evaluate(
            trend="bullish",
            market_snapshots=self._market_snapshots(bank_move=0.7, nifty_move=0.2, bank_constituent_move=0.8),
        )

        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["score"], 70)
        self.assertIn(result["details"]["tradeQuality"], {"A", "B"})

    def test_no_trade_when_top_banks_are_mixed(self) -> None:
        snapshots = self._market_snapshots(bank_move=0.6, nifty_move=0.2, bank_constituent_move=0.7)
        snapshots["HDFCBANK"] = self._snapshot(-0.4)
        snapshots["ICICIBANK"] = self._snapshot(-0.3)
        snapshots["SBIN"] = self._snapshot(0.1)

        result = self._evaluate(trend="bullish", market_snapshots=snapshots)

        self.assertFalse(result["passed"])
        self.assertIn("top banks are mixed against Bank Nifty direction", result["hard_reasons"])

    def test_no_trade_when_premium_does_not_confirm(self) -> None:
        result = self._evaluate(
            trend="bullish",
            market_snapshots=self._market_snapshots(bank_move=0.7, nifty_move=0.2, bank_constituent_move=0.8),
            premium_eval={"passed": False, "score": 35, "reasons": ["option premium is not expanding"], "details": {}},
        )

        self.assertFalse(result["passed"])
        self.assertIn("option premium is not expanding", result["hard_reasons"])

    def test_no_trade_inside_opening_range(self) -> None:
        self._seed_banknifty_candles(last_close=58040)

        result = self._evaluate(
            trend="bullish",
            snapshot={"price": 58040, "vwap": 58035},
            market_snapshots=self._market_snapshots(bank_move=0.7, nifty_move=0.2, bank_constituent_move=0.8),
        )

        self.assertFalse(result["passed"])
        self.assertIn("Bank Nifty is inside opening range", result["hard_reasons"])

    def test_no_trade_when_expected_move_is_too_small(self) -> None:
        result = self._evaluate(
            trend="bullish",
            market_snapshots=self._market_snapshots(bank_move=0.7, nifty_move=0.2, bank_constituent_move=0.8),
            prices={"entry_price": 500, "stop_loss": 420, "target_1": 900, "target_2": 1000, "target_3": 1100, "risk_reward": 5},
        )

        self.assertFalse(result["passed"])
        self.assertIn("expected move is smaller than option premium target requirement", result["hard_reasons"])

    def test_event_day_downgrades_confidence(self) -> None:
        from app.services import banknifty_intelligence_service

        original = banknifty_intelligence_service.settings.banknifty_event_dates
        try:
            object.__setattr__(banknifty_intelligence_service.settings, "banknifty_event_dates", ist_today().isoformat())
            result = self._evaluate(
                trend="bullish",
                market_snapshots=self._market_snapshots(bank_move=0.7, nifty_move=0.2, bank_constituent_move=0.8),
            )
        finally:
            object.__setattr__(banknifty_intelligence_service.settings, "banknifty_event_dates", original)

        self.assertIn("eventDayMode", result["details"])
        self.assertLess(result["score"], 100)

    def test_range_day_blocks_option_buying(self) -> None:
        result = self._evaluate(
            trend="bullish",
            market_snapshots=self._market_snapshots(bank_move=0.7, nifty_move=0.2, bank_constituent_move=0.8),
            day_type_eval={"passed": False, "score": 35, "reasons": ["range"], "details": {"day_type": "rotation_range"}},
        )

        self.assertFalse(result["passed"])
        self.assertIn("range day blocks option buying", result["hard_reasons"])

    def _evaluate(self, **overrides):  # type: ignore[no-untyped-def]
        contract = overrides.get("contract") or OptionContract(
            tradingsymbol="BANKNIFTY26JUL58200CE",
            exchange="NFO",
            instrument_token=1,
            name="BANKNIFTY",
            expiry="2026-07-26",
            strike=58200,
            option_type="CE",
            lot_size=15,
            last_price=180,
            open_interest=100000,
            volume=5000,
            bid=179,
            ask=180,
        )
        prices = overrides.get("prices") or {"entry_price": 180, "stop_loss": 145, "target_1": 240, "target_2": 270, "target_3": 320, "risk_reward": 1.7}
        premium_eval = overrides.get("premium_eval") or {
            "passed": True,
            "score": 80,
            "reasons": [],
            "details": {"last_close": 182, "option_vwap": 175, "participation_confirmed": True},
        }
        day_type_eval = overrides.get("day_type_eval") or {"passed": True, "score": 90, "reasons": [], "details": {"day_type": "trend_expansion"}}
        snapshot = {"price": 58220, "vwap": 58120, **overrides.get("snapshot", {})}
        return BankNiftyIntelligenceService().evaluate(
            trend=overrides.get("trend", "bullish"),
            snapshot=snapshot,
            market_snapshots=overrides.get("market_snapshots") or self._market_snapshots(0.6, 0.2, 0.7),
            contract=contract,
            chain_contracts=self._chain_contracts(),
            prices=prices,
            premium_eval=premium_eval,
            day_type_eval=day_type_eval,
        )

    def _market_snapshots(self, bank_move: float, nifty_move: float, bank_constituent_move: float) -> dict[str, dict[str, float]]:
        snapshots = {
            "BANKNIFTY": self._snapshot(bank_move),
            "NIFTY": self._snapshot(nifty_move),
        }
        for symbol in ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"]:
            snapshots[symbol] = self._snapshot(bank_constituent_move)
        return snapshots

    def _snapshot(self, move_pct: float) -> dict[str, float]:
        previous = 100.0
        return {"price": previous * (1 + move_pct / 100), "previous_day_close": previous, "day_open": previous}

    def _seed_banknifty_candles(self, last_close: float) -> None:
        today = ist_today().isoformat()
        MarketDataService().save_candles(
            "BANKNIFTY",
            "5minute",
            [
                {"timestamp": f"{today} 09:15:00", "open": 58000, "high": 58100, "low": 57900, "close": 58060, "volume": 1000},
                {"timestamp": f"{today} 09:20:00", "open": 58060, "high": 58120, "low": 58020, "close": 58080, "volume": 1200},
                {"timestamp": f"{today} 09:25:00", "open": 58080, "high": 58140, "low": 58000, "close": 58100, "volume": 1300},
                {"timestamp": f"{today} 09:30:00", "open": 58100, "high": 58160, "low": 58060, "close": 58150, "volume": 1400},
                {"timestamp": f"{today} 09:35:00", "open": 58150, "high": 58280, "low": 58120, "close": last_close, "volume": 2000},
            ],
        )

    def _chain_contracts(self) -> list[OptionContract]:
        rows = []
        for strike in [58000, 58100, 58200, 58300, 58400]:
            rows.append(OptionContract("BNCE", "NFO", 1, "BANKNIFTY", "2026-07-26", strike, "CE", 15, 100, 10000, 2000, 99, 100))
            rows.append(OptionContract("BNPE", "NFO", 1, "BANKNIFTY", "2026-07-26", strike, "PE", 15, 100, 16000, 2500, 99, 100))
        return rows


if __name__ == "__main__":
    unittest.main()
