import unittest
import os
import tempfile
from datetime import date

from app.services.database import init_db
from app.services.market_data_service import MarketDataService
from app.services.trade_setup_service import OptionContract, TradeSetupService


class TradeSetupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_select_contract_uses_nearest_expiry_and_strike(self) -> None:
        service = TradeSetupService()
        instruments = [
            {
                "tradingsymbol": "NIFTY24JUN22000CE",
                "exchange": "NFO",
                "name": "NIFTY",
                "expiry": "2099-06-24",
                "strike": 22000,
                "instrument_type": "CE",
                "instrument_token": 1,
                "lot_size": 50,
            },
            {
                "tradingsymbol": "NIFTY24JUN22500CE",
                "exchange": "NFO",
                "name": "NIFTY",
                "expiry": "2099-06-24",
                "strike": 22500,
                "instrument_type": "CE",
                "instrument_token": 2,
                "lot_size": 50,
            },
        ]
        quotes = {
            "NFO:NIFTY24JUN22000CE": {
                "last_price": 120,
                "volume": 2000,
                "oi": 20000,
                "depth": {"buy": [{"price": 119}], "sell": [{"price": 121}]},
            }
        }

        contract = service.select_contract(instruments, "NIFTY", 22120, "bullish", quotes=quotes)

        self.assertIsNotNone(contract)
        self.assertEqual(contract.tradingsymbol, "NIFTY24JUN22000CE")
        self.assertGreaterEqual(service.liquidity_score(contract), 70)

    def test_sell_bullish_setup_uses_puts(self) -> None:
        service = TradeSetupService()
        self.assertEqual(service.option_type_for("bullish", "SELL"), "PE")
        self.assertEqual(service.option_type_for("bearish", "SELL"), "CE")

    def test_affordable_quantity_downsizes_to_available_funds(self) -> None:
        service = TradeSetupService()

        quantity = service.affordable_quantity(
            entry_price=100,
            lot_size=50,
            available_funds=10000,
            side="BUY",
        )

        self.assertEqual(quantity, 100)

    def test_risk_checks_block_expiry_day_low_premium_option_buy(self) -> None:
        service = TradeSetupService()
        contract = OptionContract(
            tradingsymbol="HDFCBANK26JUN800PE",
            exchange="NFO",
            instrument_token=1,
            name="HDFCBANK",
            expiry=date.today().isoformat(),
            strike=800,
            option_type="PE",
            lot_size=550,
            last_price=2.4,
            open_interest=3550800,
            volume=2090550,
            bid=2.3,
            ask=2.4,
        )

        failures = service.risk_checks(86, contract, 2.4, "BUY")

        self.assertIn("option premium is below minimum configured for buying", failures)
        self.assertIn("expiry-day option buying is blocked", failures)

    def test_banknifty_buy_prices_use_option_structure_not_static_percent(self) -> None:
        MarketDataService().save_candles(
            "BANKNIFTY26JUL58000CE",
            "5minute",
            [
                {"timestamp": "2026-07-02 10:00:00", "open": 95, "high": 105, "low": 92, "close": 100, "volume": 1000},
                {"timestamp": "2026-07-02 10:05:00", "open": 100, "high": 112, "low": 97, "close": 108, "volume": 1200},
                {"timestamp": "2026-07-02 10:10:00", "open": 108, "high": 118, "low": 104, "close": 115, "volume": 1300},
                {"timestamp": "2026-07-02 10:15:00", "open": 115, "high": 124, "low": 110, "close": 121, "volume": 1600},
                {"timestamp": "2026-07-02 10:20:00", "open": 121, "high": 130, "low": 116, "close": 126, "volume": 1800},
                {"timestamp": "2026-07-02 10:25:00", "open": 126, "high": 136, "low": 120, "close": 132, "volume": 2000},
            ],
        )
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=1,
            name="BANKNIFTY",
            expiry="2026-07-26",
            strike=58000,
            option_type="CE",
            lot_size=15,
            last_price=132,
            open_interest=100000,
            volume=2000,
            bid=131,
            ask=132,
        )

        prices = TradeSetupService().build_prices(
            entry_price=132,
            side="BUY",
            underlying="BANKNIFTY",
            snapshot={"adx": 26, "volume_confirmed": True, "price": 58000},
            contract=contract,
        )

        self.assertEqual(prices["risk_model"], "banknifty_structure_atr_premium")
        self.assertGreater(prices["premium_atr"], 0)
        self.assertEqual(prices["premium_swing_low"], 92)
        self.assertGreater(prices["target_1"], prices["entry_price"])
        self.assertLess(prices["stop_loss"], prices["entry_price"])
        self.assertNotEqual(prices["stop_loss"], round(132 * 0.78, 2))


if __name__ == "__main__":
    unittest.main()
