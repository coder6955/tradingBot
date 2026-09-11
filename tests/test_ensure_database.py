import unittest
from unittest.mock import Mock, patch

from scripts.ensure_database import ensure_configured_database


class EnsureDatabaseTests(unittest.TestCase):
    def test_sqlite_needs_no_server_schema_bootstrap(self) -> None:
        self.assertEqual(ensure_configured_database("sqlite:///./app.db"), "not_required")

    @patch("scripts.ensure_database.create_engine")
    def test_mysql_database_is_created_idempotently(self, create_engine: Mock) -> None:
        connection = create_engine.return_value.connect.return_value.__enter__.return_value

        result = ensure_configured_database(
            "mysql+pymysql://trader:secret@localhost:3306/option_trading"
        )

        self.assertEqual(result, "option_trading")
        server_url = create_engine.call_args.args[0]
        self.assertIsNone(server_url.database)
        connection.exec_driver_sql.assert_called_once_with(
            "CREATE DATABASE IF NOT EXISTS `option_trading` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        create_engine.return_value.dispose.assert_called_once_with()

    def test_unsafe_mysql_database_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid MySQL database name"):
            ensure_configured_database(
                "mysql+pymysql://trader:secret@localhost:3306/unsafe-name"
            )


if __name__ == "__main__":
    unittest.main()
