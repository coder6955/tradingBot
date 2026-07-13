import unittest

from app.config import settings
from app.providers.kite_auth_state import kite_auth_state
from app.providers.kite_provider import KiteProvider


class TokenException(Exception):
    pass


class FailingKiteClient:
    def quote(self, instruments):
        raise TokenException("invalid access token")

    def profile(self):
        raise TokenException("invalid access token")

    def margins(self):
        return {"ok": True}


class KiteAuthStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._api_key = settings.kite_api_key
        kite_auth_state.clear()
        object.__setattr__(settings, "kite_api_key", "kite-key")

    def tearDown(self) -> None:
        object.__setattr__(settings, "kite_api_key", self._api_key)
        kite_auth_state.clear()

    def _provider(self) -> KiteProvider:
        provider = KiteProvider()
        provider.client = FailingKiteClient()
        provider.access_token = "token"
        return provider

    def test_token_exception_marks_relogin_required_and_blocks_followup_calls(self) -> None:
        provider = self._provider()

        with self.assertRaises(TokenException):
            provider.quote(["NFO:BANKNIFTY26JUL58000CE"])

        self.assertTrue(kite_auth_state.status()["relogin_required"])
        with self.assertRaisesRegex(RuntimeError, "relogin required"):
            provider.margins()

    def test_profile_returns_controlled_auth_failed_payload(self) -> None:
        payload = self._provider().profile()

        self.assertEqual(payload["status"], "AUTH_FAILED")
        self.assertTrue(payload["relogin_required"])


if __name__ == "__main__":
    unittest.main()
