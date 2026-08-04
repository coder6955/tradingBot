import unittest
from unittest.mock import Mock, patch

from app.config import settings
from app.services.notification_service import NotificationService


class NotificationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_bot_token = settings.telegram_bot_token
        self.original_chat_id = settings.telegram_chat_id

    def tearDown(self) -> None:
        object.__setattr__(settings, "telegram_bot_token", self.original_bot_token)
        object.__setattr__(settings, "telegram_chat_id", self.original_chat_id)

    def test_disabled_notification_is_skipped(self) -> None:
        object.__setattr__(settings, "telegram_bot_token", None)
        object.__setattr__(settings, "telegram_chat_id", None)

        service = NotificationService()
        self.assertEqual(
            service.send("done", kind="runtime_failure")["status"], "skipped"
        )
        status = service.status()
        self.assertFalse(status["configured"])
        self.assertEqual(status["last_attempt"]["kind"], "runtime_failure")
        self.assertEqual(status["last_attempt"]["status"], "skipped")

    @patch("app.services.notification_service.requests")
    def test_error_response_never_contains_bot_token(self, requests: Mock) -> None:
        secret = "private-bot-token"
        object.__setattr__(settings, "telegram_bot_token", secret)
        object.__setattr__(settings, "telegram_chat_id", "123")
        requests.post.side_effect = RuntimeError(f"failed URL contained {secret}")

        result = NotificationService().send("done")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn(secret, str(result))


if __name__ == "__main__":
    unittest.main()
