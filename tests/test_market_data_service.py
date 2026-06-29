import os
import tempfile
import unittest

from app.services.database import Base, SessionLocal, init_db
from app.services.market_data_service import MarketDataService


class MarketDataServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.database_url = f"sqlite:///{self.temp_db.name}"
        init_db(self.database_url)
        self.session_factory = SessionLocal

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_save_candles_and_get_summary(self) -> None:
        service = MarketDataService(session_factory=self.session_factory)
        candles = [
            {"timestamp": "2024-01-01T09:15:00", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1200.0},
            {"timestamp": "2024-01-01T09:16:00", "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 1400.0},
        ]

        saved_count = service.save_candles("NIFTY", "1m", candles)
        self.assertEqual(saved_count, 2)

        summary = service.get_market_summary("NIFTY")
        self.assertEqual(summary["symbol"], "NIFTY")
        self.assertEqual(summary["count"], 2)
        self.assertGreaterEqual(summary["latest_close"], 100.0)


if __name__ == "__main__":
    unittest.main()
