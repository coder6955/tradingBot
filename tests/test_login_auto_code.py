import unittest
from dataclasses import fields
from unittest.mock import patch

from app import loginAutoCode
from app.config import settings


class FakeKite:
    def __init__(self) -> None:
        self.access_token = None
        self.generated_request_token = None

    def set_access_token(self, token):  # type: ignore[no-untyped-def]
        self.access_token = token

    def profile(self):  # type: ignore[no-untyped-def]
        if self.access_token in {"valid-file", "valid-legacy", "generated-token"}:
            return {"user_id": "masked"}
        raise RuntimeError("invalid token")

    def generate_session(self, request_token, api_secret):  # type: ignore[no-untyped-def]
        self.generated_request_token = request_token
        return {"access_token": "generated-token"}


class LoginAutoCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings_snapshot = {
            item.name: getattr(settings, item.name) for item in fields(type(settings))
        }
        for key, value in {
            "kite_api_key": "api-key",
            "kite_api_secret": "api-secret",
            "kite_user_id": "user-id",
            "kite_password": "password",
            "kite_totp_secret": "JBSWY3DPEHPK3PXP",
            "kite_auto_login_enabled": True,
        }.items():
            object.__setattr__(settings, key, value)

    def tearDown(self) -> None:
        for key, value in self.settings_snapshot.items():
            object.__setattr__(settings, key, value)

    @patch.object(loginAutoCode, "token_status", return_value={"valid_for_today": True})
    @patch.object(loginAutoCode, "save_access_token")
    @patch.object(loginAutoCode, "load_legacy_env_access_token", return_value=None)
    @patch.object(loginAutoCode, "load_access_token", return_value="valid-file")
    @patch.object(loginAutoCode, "_kite_client", return_value=FakeKite())
    def test_reuses_valid_token_file_without_browser(
        self, _client, _load, _legacy, save, _status
    ) -> None:  # type: ignore[no-untyped-def]
        with patch.object(loginAutoCode, "_headless_request_token") as browser:
            result = loginAutoCode.ensure_access_token_for_today()

        self.assertTrue(result["ready"])
        self.assertEqual(result["source"], "today_token_file")
        browser.assert_not_called()
        save.assert_not_called()

    @patch.object(loginAutoCode, "token_status", return_value={"valid_for_today": True})
    @patch.object(loginAutoCode, "save_access_token")
    @patch.object(
        loginAutoCode, "load_legacy_env_access_token", return_value="valid-legacy"
    )
    @patch.object(loginAutoCode, "load_access_token", return_value=None)
    @patch.object(loginAutoCode, "_kite_client", return_value=FakeKite())
    def test_migrates_valid_legacy_env_token(
        self, _client, _load, _legacy, save, _status
    ) -> None:  # type: ignore[no-untyped-def]
        result = loginAutoCode.ensure_access_token_for_today()

        self.assertTrue(result["ready"])
        self.assertEqual(result["source"], "legacy_env_migrated")
        save.assert_called_once_with("valid-legacy", source="legacy_env_migration")

    @patch.object(loginAutoCode, "token_status", return_value={"valid_for_today": True})
    @patch.object(loginAutoCode, "save_access_token")
    @patch.object(
        loginAutoCode, "_headless_request_token", return_value="request-token"
    )
    @patch.object(loginAutoCode, "load_legacy_env_access_token", return_value=None)
    @patch.object(loginAutoCode, "load_access_token", return_value=None)
    @patch.object(loginAutoCode, "_kite_client", return_value=FakeKite())
    def test_generates_valid_token_and_saves_it(
        self, _client, _load, _legacy, browser, save, _status
    ) -> None:  # type: ignore[no-untyped-def]
        result = loginAutoCode.ensure_access_token_for_today()

        self.assertTrue(result["ready"])
        self.assertEqual(result["source"], "automatic_headless_login")
        browser.assert_called_once()
        save.assert_called_once_with(
            "generated-token", source="automatic_headless_login"
        )

    @patch.object(
        loginAutoCode, "token_status", return_value={"valid_for_today": False}
    )
    @patch.object(loginAutoCode, "load_legacy_env_access_token", return_value=None)
    @patch.object(loginAutoCode, "load_access_token", return_value=None)
    @patch.object(loginAutoCode, "_kite_client", return_value=FakeKite())
    def test_missing_credentials_fail_before_browser(
        self, _client, _load, _legacy, _status
    ) -> None:  # type: ignore[no-untyped-def]
        object.__setattr__(settings, "kite_totp_secret", None)
        with patch.object(loginAutoCode, "_headless_request_token") as browser:
            result = loginAutoCode.ensure_access_token_for_today()

        self.assertFalse(result["ready"])
        self.assertEqual(result["source"], "automatic_login_credentials_missing")
        self.assertIn("KITE_TOTP_SECRET", result["missing_configuration"])
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
