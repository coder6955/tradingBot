import unittest
from datetime import timedelta

from app.config import settings
from app.services.banknifty_option_prewarm_service import BankNiftyOptionPrewarmService
from app.services.time_utils import ist_now_naive


class FakeWebSocket:
    def __init__(self) -> None:
        self.subscriptions: list[set[int]] = []

    def subscribe(self, tokens):
        clean = {int(token) for token in tokens}
        self.subscriptions.append(clean)
        return {"subscribed": sorted(clean)}


def _instrument(strike: int, option_type: str, token: int) -> dict[str, object]:
    return {
        "tradingsymbol": f"BANKNIFTY26JUL{strike}{option_type}",
        "exchange": "NFO",
        "instrument_token": token,
        "name": "BANKNIFTY",
        "expiry": "2099-07-26",
        "strike": strike,
        "instrument_type": option_type,
        "lot_size": 15,
    }


class BankNiftyOptionPrewarmServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "enable_banknifty_option_prewarm": settings.enable_banknifty_option_prewarm,
            "enable_kite_websocket": settings.enable_kite_websocket,
            "banknifty_prewarm_strike_depth": settings.banknifty_prewarm_strike_depth,
            "banknifty_prewarm_refresh_seconds": settings.banknifty_prewarm_refresh_seconds,
        }
        object.__setattr__(settings, "enable_banknifty_option_prewarm", True)
        object.__setattr__(settings, "enable_kite_websocket", True)
        object.__setattr__(settings, "banknifty_prewarm_strike_depth", 1)
        object.__setattr__(settings, "banknifty_prewarm_refresh_seconds", 60)

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)

    def _instruments(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        token = 1000
        for strike in [57800, 57900, 58000, 58100, 58200]:
            for option_type in ["CE", "PE"]:
                token += 1
                rows.append(_instrument(strike, option_type, token))
        return rows

    def test_prewarm_selects_atm_ce_pe_plus_one_itm_otm_each(self) -> None:
        ws = FakeWebSocket()
        service = BankNiftyOptionPrewarmService(ws)

        result = service.prewarm(
            spot_price=58020, option_instruments=self._instruments()
        )

        self.assertTrue(result["refreshed"])
        self.assertEqual(len(result["prewarm_tokens"]), 6)
        symbols = result["prewarm_tradingsymbols"]
        self.assertIn("BANKNIFTY26JUL57900CE", symbols)
        self.assertIn("BANKNIFTY26JUL58000CE", symbols)
        self.assertIn("BANKNIFTY26JUL58100CE", symbols)
        self.assertIn("BANKNIFTY26JUL57900PE", symbols)
        self.assertIn("BANKNIFTY26JUL58000PE", symbols)
        self.assertIn("BANKNIFTY26JUL58100PE", symbols)

    def test_prewarm_does_not_subscribe_full_option_chain(self) -> None:
        ws = FakeWebSocket()
        service = BankNiftyOptionPrewarmService(ws)

        service.prewarm(spot_price=58020, option_instruments=self._instruments())

        self.assertEqual(len(ws.subscriptions), 1)
        self.assertEqual(len(next(iter(ws.subscriptions))), 6)

    def test_prewarm_refreshes_when_atm_strike_changes(self) -> None:
        ws = FakeWebSocket()
        service = BankNiftyOptionPrewarmService(ws)
        service.prewarm(spot_price=58020, option_instruments=self._instruments())
        service.last_refresh_at = ist_now_naive() - timedelta(seconds=5)

        result = service.prewarm(
            spot_price=58110, option_instruments=self._instruments()
        )

        self.assertTrue(result["refreshed"])
        self.assertEqual(len(ws.subscriptions), 2)
        self.assertEqual(result["prewarm_atm_strike"], 58100)


if __name__ == "__main__":
    unittest.main()
