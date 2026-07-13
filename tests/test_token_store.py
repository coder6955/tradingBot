import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.providers import token_store


class TokenStoreTests(unittest.TestCase):
    def test_load_access_token_strips_quotes_and_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text('KITE_ACCESS_TOKEN="  fresh-token  "\n')

            with patch.object(token_store, "ENV_FILE", env_file):
                self.assertEqual(token_store.load_access_token(), "fresh-token")

    def test_save_access_token_strips_quotes_and_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("KITE_ACCESS_TOKEN=old-token\n")

            with patch.object(token_store, "ENV_FILE", env_file):
                token_store.save_access_token(" 'fresh-token' ")
                self.assertEqual(token_store.load_access_token(), "fresh-token")
                self.assertIn("KITE_ACCESS_TOKEN=fresh-token", env_file.read_text())


if __name__ == "__main__":
    unittest.main()
