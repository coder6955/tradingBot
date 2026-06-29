import os
import tempfile
import unittest

from app.services.database import init_db
from app.services.signal_repository import SignalRepository


class SignalRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.database_url = f"sqlite:///{self.temp_db.name}"
        init_db(self.database_url)

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_save_and_list_signals(self) -> None:
        repo = SignalRepository(self.database_url)
        repo.save_signal("NIFTY", "BUY_CE", 88, 0.86, "bullish")
        signals = repo.list_signals()
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "NIFTY")


if __name__ == "__main__":
    unittest.main()
