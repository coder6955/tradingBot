import unittest

from app.services.trade_setup_service import TradeSetupService


class TradeSetupServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
