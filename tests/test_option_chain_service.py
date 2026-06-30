import unittest

from app.services.option_chain_service import OptionChainService
from app.services.trade_setup_service import OptionContract


class OptionChainServiceTests(unittest.TestCase):
    def test_bullish_chain_scores_support_and_resistance_room(self) -> None:
        service = OptionChainService()
        selected = OptionContract(
            tradingsymbol="NIFTY26JUN24000CE",
            exchange="NFO",
            instrument_token=1,
            name="NIFTY",
            expiry="2099-06-24",
            strike=24000,
            option_type="CE",
            lot_size=65,
            last_price=100,
            open_interest=20000,
            volume=5000,
            bid=99,
            ask=100,
        )
        contracts = [
            selected,
            OptionContract("NIFTY26JUN23900PE", "NFO", 2, "NIFTY", "2099-06-24", 23900, "PE", 65, 80, 50000, 7000, 79, 80),
            OptionContract("NIFTY26JUN24200CE", "NFO", 3, "NIFTY", "2099-06-24", 24200, "CE", 65, 60, 45000, 6000, 59, 60),
        ]

        result = service.analyze(24000, "bullish", "BUY", selected, contracts)

        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["score"], 55)
        self.assertEqual(result["details"]["support_strike"], 23900)


if __name__ == "__main__":
    unittest.main()
