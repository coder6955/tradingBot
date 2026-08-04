import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.providers import token_store


class TokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 4, 8, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_save_and_load_current_dated_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / "access_token.txt"
            with patch.object(token_store, "TOKEN_FILE", token_file):
                token_store.save_access_token(
                    " 'fresh-token' ", created_at=self.now, source="test"
                )

                self.assertEqual(
                    token_store.load_access_token(now=self.now), "fresh-token"
                )
                payload = json.loads(token_file.read_text(encoding="utf-8"))
                self.assertEqual(payload["trading_date"], "2026-08-04")
                self.assertEqual(payload["source"], "test")

    def test_stale_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / "access_token.txt"
            with patch.object(token_store, "TOKEN_FILE", token_file):
                token_store.save_access_token("old-token", created_at=self.now)

                tomorrow = datetime(2026, 8, 5, 8, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
                self.assertIsNone(token_store.load_access_token(now=tomorrow))
                self.assertEqual(
                    token_store.load_access_token(require_today=False), "old-token"
                )

    def test_malformed_token_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / "access_token.txt"
            token_file.write_text("not-json", encoding="utf-8")
            with patch.object(token_store, "TOKEN_FILE", token_file):
                self.assertIsNone(token_store.load_access_token(now=self.now))
                self.assertFalse(
                    token_store.token_status(now=self.now)["token_present"]
                )

    def test_legacy_env_token_is_available_only_for_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text('KITE_ACCESS_TOKEN="  legacy-token  "\n')
            with patch.object(token_store, "ENV_FILE", env_file):
                self.assertEqual(
                    token_store.load_legacy_env_access_token(), "legacy-token"
                )


if __name__ == "__main__":
    unittest.main()
