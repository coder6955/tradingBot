import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.services.database import Candle, OptionQuoteSnapshot, get_session, init_db
from app.services.trade_setup_service import OptionContract
from app.services.volatility_edge_service import VolatilityEdgeService


class VolatilityEdgeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.contract = OptionContract(
            "BANKNIFTY26JUL58000CE",
            "NFO",
            580001,
            "BANKNIFTY",
            "2099-07-26",
            58000,
            "CE",
            15,
            100,
            100000,
            50000,
            99,
            101,
        )

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _insert_banknifty_candles(self, closes: list[float]) -> None:
        session = get_session()
        try:
            start = datetime.now() - timedelta(minutes=len(closes) * 5)
            for idx, close in enumerate(closes):
                session.add(
                    Candle(
                        symbol="BANKNIFTY",
                        timeframe="5minute",
                        timestamp=start + timedelta(minutes=idx * 5),
                        open_price=close,
                        high_price=close + 20,
                        low_price=close - 20,
                        close_price=close,
                        volume=1000,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _insert_option_iv_snapshots(self, values: list[float]) -> None:
        session = get_session()
        try:
            start = datetime.now() - timedelta(minutes=len(values))
            for idx, iv in enumerate(values):
                session.add(
                    OptionQuoteSnapshot(
                        underlying="BANKNIFTY",
                        tradingsymbol=self.contract.tradingsymbol,
                        exchange="NFO",
                        timestamp=start + timedelta(minutes=idx),
                        expiry=self.contract.expiry,
                        strike=self.contract.strike,
                        option_type=self.contract.option_type,
                        last_price=100,
                        bid=99,
                        ask=101,
                        implied_volatility=iv,
                        delta=0.5,
                        theta=-3,
                        open_interest=50000,
                        volume=100000,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _insert_option_candles(self, ranges: list[float]) -> None:
        session = get_session()
        try:
            start = datetime.now() - timedelta(minutes=len(ranges) * 5)
            price = 100.0
            for idx, candle_range in enumerate(ranges):
                price += 2
                session.add(
                    Candle(
                        symbol=self.contract.tradingsymbol,
                        timeframe="5minute",
                        timestamp=start + timedelta(minutes=idx * 5),
                        open_price=price,
                        high_price=price + candle_range,
                        low_price=price,
                        close_price=price + candle_range / 2,
                        volume=1000,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _quality(self, iv: float = 0.20, delta: float = 0.50, dte: int = 30) -> dict[str, object]:
        return {
            "score": 90,
            "passed": True,
            "reasons": [],
            "details": {
                "greeks": {
                    "implied_volatility": iv,
                    "delta": delta,
                    "theta": -3.0,
                    "days_to_expiry": dte,
                }
            },
        }

    def _evaluate(self, *, iv: float = 0.20, prices: dict[str, float] | None = None) -> dict[str, object]:
        return VolatilityEdgeService().evaluate(
            symbol="BANKNIFTY",
            contract=self.contract,
            option_quality=self._quality(iv=iv),
            premium_eval={"passed": True, "details": {"breakout": True}},
            market_snapshots={"BANKNIFTY": {"price": 58000}, "INDIAVIX": {"price": 15}},
            prices=prices or {"entry_price": 100, "target_1": 120},
            action="BUY_CE",
            side="BUY",
        )

    def test_insufficient_iv_history_returns_unknown_data_missing(self) -> None:
        self._insert_banknifty_candles([58000 + idx for idx in range(40)])

        result = self._evaluate()

        self.assertFalse(result["passed"])
        self.assertIn("UNKNOWN_DATA_MISSING: insufficient IV history", result["reasons"])
        self.assertEqual(result["details"]["iv_history_data_quality"], "UNKNOWN_DATA_MISSING")

    def test_realized_volatility_is_calculated_from_banknifty_candles(self) -> None:
        closes = [58000, 58100, 57950, 58200, 58050, 58300, 58100, 58400, 58200, 58500, 58300, 58600]
        self._insert_banknifty_candles(closes)
        self._insert_option_iv_snapshots([0.18 + idx * 0.001 for idx in range(35)])

        result = self._evaluate(iv=0.20)

        self.assertGreater(result["details"]["realized_volatility_intraday"], 0)
        self.assertGreater(result["details"]["realized_range_pct"], 0)
        self.assertEqual(result["details"]["candles_used"], len(closes))

    def test_iv_too_expensive_is_flagged(self) -> None:
        self._insert_banknifty_candles([58000 + (idx % 2) for idx in range(40)])
        self._insert_option_iv_snapshots([0.20 + idx * 0.001 for idx in range(35)])

        result = self._evaluate(iv=1.00)

        self.assertIn(result["classification"], {"expensive", "iv_crush_risk"})
        self.assertEqual(result["details"]["iv_vs_realized_label"], "IV_TOO_EXPENSIVE")
        self.assertEqual(result["volatility_edge_for_option_buying"], "unfavorable")

    def test_iv_cheap_relative_to_realized_is_identified(self) -> None:
        closes = [58000 + ((-1) ** idx) * 600 for idx in range(40)]
        self._insert_banknifty_candles(closes)
        self._insert_option_iv_snapshots([0.08 + idx * 0.001 for idx in range(35)])

        result = self._evaluate(iv=0.10)

        self.assertEqual(result["details"]["iv_vs_realized_label"], "IV_CHEAP_RELATIVE_TO_RV")
        self.assertIn(result["classification"], {"cheap_relative_to_realized", "iv_expansion_supported", "fair"})

    def test_iv_expansion_and_premium_range_support_option_buying(self) -> None:
        self._insert_banknifty_candles([58000 + idx * 20 for idx in range(40)])
        self._insert_option_iv_snapshots([0.15 + idx * 0.003 for idx in range(35)])
        self._insert_option_candles([2, 2, 2, 5, 6, 7])

        result = self._evaluate(iv=0.24)

        self.assertTrue(result["details"]["iv_expansion_supported"])
        self.assertIn("volatility expansion supports option buying", result["reasons"])

    def test_high_iv_without_expansion_warns_about_crush_risk(self) -> None:
        self._insert_banknifty_candles([58000 + idx for idx in range(40)])
        high_but_falling_iv = [0.10 + idx * 0.008 for idx in range(25)] + [0.30 - idx * 0.001 for idx in range(10)]
        self._insert_option_iv_snapshots(high_but_falling_iv)
        self._insert_option_candles([6, 6, 6, 2, 2, 2])

        result = self._evaluate(iv=0.30)

        self.assertTrue(result["details"]["iv_crush_risk"])
        self.assertEqual(result["classification"], "iv_crush_risk")

    def test_expected_move_coverage_strong_when_iv_can_cover_target(self) -> None:
        self._insert_banknifty_candles([58000 + idx * 10 for idx in range(40)])
        self._insert_option_iv_snapshots([0.18 + idx * 0.001 for idx in range(35)])

        result = self._evaluate(iv=0.20, prices={"entry_price": 100, "target_1": 120})

        self.assertEqual(result["details"]["expected_move_label"], "strong")
        self.assertGreater(result["details"]["best_expected_move_coverage"], 1.1)

    def test_expected_move_coverage_weak_penalizes_buying(self) -> None:
        self._insert_banknifty_candles([58000 + (idx % 2) for idx in range(40)])
        self._insert_option_iv_snapshots([0.05 for _ in range(35)])

        result = VolatilityEdgeService().evaluate(
            symbol="BANKNIFTY",
            contract=self.contract,
            option_quality=self._quality(iv=0.05, delta=0.35, dte=1),
            premium_eval={"passed": True, "details": {"breakout": True}},
            market_snapshots={"BANKNIFTY": {"price": 58000}, "INDIAVIX": {"price": 12}},
            prices={"entry_price": 100, "target_1": 600},
            action="BUY_CE",
            side="BUY",
        )

        self.assertEqual(result["details"]["expected_move_label"], "weak")
        self.assertIn("expected move coverage is weak for target 1", result["reasons"])


if __name__ == "__main__":
    unittest.main()
