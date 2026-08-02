import os
import tempfile
import unittest

from app.config import settings
from app.services.database import init_db
from app.services.strategy_version_registry import StrategyVersionRegistry


class StrategyVersionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            "strategy_name": settings.strategy_name,
            "strategy_version": settings.strategy_version,
            "strategy_version_note": settings.strategy_version_note,
            "strategy_change_reason": settings.strategy_change_reason,
            "min_signal_score": settings.min_signal_score,
        }
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        object.__setattr__(settings, "strategy_name", "banknifty_option_buying")
        object.__setattr__(
            settings, "strategy_version", "banknifty_option_buying_test_v1"
        )
        object.__setattr__(settings, "strategy_version_note", "Test version note")
        object.__setattr__(settings, "strategy_change_reason", "Test change reason")
        object.__setattr__(settings, "min_signal_score", 80)

    def tearDown(self) -> None:
        for key, value in self.originals.items():
            object.__setattr__(settings, key, value)
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_current_version_is_registered_with_human_purpose_snapshot(self) -> None:
        registry = StrategyVersionRegistry()

        result = registry.ensure_current_version()

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["created"])
        version = result["version"]
        self.assertEqual(version["version"], "banknifty_option_buying_test_v1")
        self.assertEqual(version["human_note"], "Test version note")
        self.assertIn("entry_logic_summary", version)
        self.assertEqual(
            version["config_snapshot"]["entry_filters"]["min_signal_score"], 80
        )
        self.assertIn(
            "purpose", version["settings_purpose"]["entry_filters"]["min_signal_score"]
        )

    def test_same_version_with_changed_config_flags_drift_without_overwriting_original(
        self,
    ) -> None:
        registry = StrategyVersionRegistry()
        registry.ensure_current_version()

        object.__setattr__(settings, "min_signal_score", 85)
        result = registry.ensure_current_version()

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["created"])
        self.assertTrue(result["config_drift_detected"])
        version = result["version"]
        self.assertTrue(version["config_drift_detected"])
        self.assertEqual(
            version["config_snapshot"]["entry_filters"]["min_signal_score"], 80
        )
        self.assertEqual(
            version["latest_config_snapshot"]["entry_filters"]["min_signal_score"], 85
        )
        self.assertIn("Bump STRATEGY_VERSION", version["config_drift_warning"])

    def test_reactivating_a_previous_version_clears_retired_timestamp(self) -> None:
        registry = StrategyVersionRegistry()
        registry.ensure_current_version()
        object.__setattr__(
            settings, "strategy_version", "banknifty_option_buying_test_v2"
        )
        registry.ensure_current_version()
        object.__setattr__(
            settings, "strategy_version", "banknifty_option_buying_test_v1"
        )

        result = registry.ensure_current_version()

        self.assertEqual(result["version"]["status"], "active")
        self.assertIsNone(result["version"]["retired_at"])


if __name__ == "__main__":
    unittest.main()
