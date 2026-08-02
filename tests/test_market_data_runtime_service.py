import unittest
from unittest.mock import patch

from app.config import settings
from app.services.market_data_runtime_service import MarketDataRuntimeService


class FakeWebSocketFeed:
    access_token = "token"

    def __init__(self) -> None:
        self.refreshed: list[str | None] = []

    def refresh_credentials(self, *, access_token=None):
        self.access_token = access_token or self.access_token
        self.refreshed.append(access_token)


class FakeActivePriceFeed:
    def __init__(self, status):
        self._status = status
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        return {"started": True}

    def stop(self):
        self.stopped += 1
        return {"stopped": True}

    def status(self):
        return dict(self._status)


class FakePrewarmService:
    def status(self):
        return {"prewarm_enabled": True}


class MarketDataRuntimeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_enabled = settings.enable_kite_websocket
        self.original_key = settings.kite_api_key
        self.original_token = settings.kite_access_token
        object.__setattr__(settings, "enable_kite_websocket", True)
        object.__setattr__(settings, "kite_api_key", "api-key")
        object.__setattr__(settings, "kite_access_token", "settings-token")

    def tearDown(self) -> None:
        object.__setattr__(settings, "enable_kite_websocket", self.original_enabled)
        object.__setattr__(settings, "kite_api_key", self.original_key)
        object.__setattr__(settings, "kite_access_token", self.original_token)

    def service_for(self, websocket_status):
        websocket_feed = FakeWebSocketFeed()
        active_feed = FakeActivePriceFeed(websocket_status)
        service = MarketDataRuntimeService(
            websocket_feed=websocket_feed,
            active_price_feed=active_feed,
            prewarm_service=FakePrewarmService(),
        )
        return service, websocket_feed, active_feed

    def test_connected_state_is_explicit(self) -> None:
        service, _, _ = self.service_for(
            {
                "websocket_status": "CONNECTED",
                "websocket_connected": True,
                "running": True,
                "market_session": "REGULAR_MARKET",
                "max_tick_age": 1,
            }
        )

        with patch(
            "app.services.market_data_runtime_service.load_access_token",
            return_value=None,
        ):
            status = service.status()

        self.assertEqual(status["market_data_state"], "CONNECTED")
        self.assertTrue(status["websocket_connected"])
        self.assertEqual(status["banknifty_option_prewarm"], {"prewarm_enabled": True})

    def test_stale_state_requires_subscribed_token(self) -> None:
        service, _, _ = self.service_for(
            {
                "websocket_status": "CONNECTED",
                "websocket_connected": True,
                "running": True,
                "market_session": "REGULAR_MARKET",
                "max_tick_age": settings.websocket_price_stale_seconds + 1,
                "subscribed_tokens": [123],
            }
        )

        status = service.status()

        self.assertEqual(status["market_data_state"], "STALE")
        self.assertEqual(status["market_data_reason"], "websocket_tick_stale")

    def test_auth_required_when_token_missing(self) -> None:
        object.__setattr__(settings, "kite_access_token", None)
        service, websocket_feed, _ = self.service_for(
            {
                "websocket_status": "AUTH_FAILED",
                "websocket_connected": False,
                "running": False,
                "market_session": "REGULAR_MARKET",
                "last_error": "token expired",
            }
        )
        websocket_feed.access_token = None

        status = service.status()

        self.assertEqual(status["market_data_state"], "AUTH_REQUIRED")
        self.assertEqual(status["market_data_reason"], "token expired")

    def test_refresh_credentials_restarts_when_enabled(self) -> None:
        service, websocket_feed, active_feed = self.service_for(
            {
                "websocket_status": "DISCONNECTED",
                "websocket_connected": False,
                "running": False,
                "market_session": "REGULAR_MARKET",
            }
        )

        result = service.refresh_credentials(access_token="new-token")

        self.assertEqual(websocket_feed.access_token, "new-token")
        self.assertTrue(result["restart_requested"])
        self.assertEqual(active_feed.started, 1)
