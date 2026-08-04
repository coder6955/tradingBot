import asyncio
import unittest
from types import SimpleNamespace

from scripts.run_scheduled_app import (
    COMPLETION_REASON,
    RuntimeMonitorResult,
    StartupHealthResult,
    _after_market_completion_reached,
    _evaluate_runtime_health,
    _evaluate_startup_health,
    _failure_exit_code,
    _monitor_completion,
    _monitor_startup_health,
    _new_runtime_error_issues,
    _runtime_notification_message,
    _startup_notification_message,
)


class FakeServer:
    def __init__(self) -> None:
        self.should_exit = False


class FakeNotificationService:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, message: str, *, kind: str = "operational") -> dict[str, str]:
        self.messages.append((kind, message))
        return {"status": "sent"}


class ScheduledAppRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _healthy_checks(self) -> dict[str, dict[str, object]]:
        return {
            "api": {"status": "ok"},
            "database": {"status": "ok"},
            "kite": {"status": "ok"},
            "websocket": {
                "websocket_enabled": True,
                "websocket_connected": True,
                "subscription_owners": {"260105": ["core_market"]},
                "data_gap": {"active_gap": False},
            },
            "automation": {
                "running": True,
                "lifecycle": {"task_healthy": True},
            },
            "runtime": {
                "status": "ok",
                "session": {"should_run_live_modules": True},
                "instance_lock": {"acquired": True},
                "startup_tasks": {
                    "maintenance": {"completed": True, "errors": []},
                    "broker_sync": {"completed": True, "errors": []},
                },
            },
            "reconciliation": {
                "blocked": False,
                "last_reconciliation": {"status": "ok"},
            },
        }

    def test_startup_health_requires_all_operational_checks(self) -> None:
        result = _evaluate_startup_health(self._healthy_checks())

        self.assertTrue(result.ready)
        self.assertEqual(result.issues, ())

    def test_startup_health_reports_actionable_failures(self) -> None:
        checks = self._healthy_checks()
        checks["kite"] = {"status": "AUTH_FAILED", "message": "Token expired"}
        checks["websocket"] = {
            "websocket_enabled": True,
            "websocket_connected": False,
            "websocket_status": "AUTH_FAILED",
        }
        checks["automation"] = {
            "running": True,
            "lifecycle": {"task_healthy": False},
        }

        result = _evaluate_startup_health(checks)

        self.assertFalse(result.ready)
        self.assertIn("Kite login: Token expired", result.issues)
        self.assertIn("WebSocket: AUTH_FAILED", result.issues)
        self.assertIn("Automation: supervisor task is unhealthy", result.issues)

    def test_startup_messages_distinguish_success_and_failure(self) -> None:
        healthy = StartupHealthResult(True, (), self._healthy_checks())
        failed = StartupHealthResult(False, ("WebSocket: not connected",), {})

        success_message = _startup_notification_message(
            healthy, mode="paper", strategy_version="banknifty_option_buying_v7"
        )
        failure_message = _startup_notification_message(
            failed, mode="paper", strategy_version="banknifty_option_buying_v7"
        )

        self.assertIn("started successfully", success_message)
        self.assertIn("WebSocket: connected", success_message)
        self.assertIn("startup FAILED", failure_message)
        self.assertIn("WebSocket: not connected", failure_message)
        self.assertIn(
            "RUNTIME FAILURE",
            _runtime_notification_message(("Database: unavailable",)),
        )
        self.assertEqual(_failure_exit_code({"status": "sent"}), 10)
        self.assertEqual(_failure_exit_code({"status": "error"}), 11)

    async def test_startup_monitor_detects_server_exit(self) -> None:
        server_task = asyncio.create_task(asyncio.sleep(0))
        await server_task

        result = await _monitor_startup_health(
            server_task=server_task,
            base_url="http://127.0.0.1:1",
            timeout_seconds=1,
            poll_seconds=0.01,
            request_timeout_seconds=0.01,
        )

        self.assertFalse(result.ready)
        self.assertIn("stopped before startup", result.issues[0])

    def test_completion_requires_expected_stop_reason(self) -> None:
        self.assertTrue(
            _after_market_completion_reached(
                SimpleNamespace(running=False, last_stop_reason=COMPLETION_REASON)
            )
        )
        self.assertFalse(
            _after_market_completion_reached(
                SimpleNamespace(running=False, last_stop_reason="requested")
            )
        )
        self.assertFalse(
            _after_market_completion_reached(
                SimpleNamespace(running=True, last_stop_reason=COMPLETION_REASON)
            )
        )

    async def test_monitor_requests_graceful_server_exit(self) -> None:
        server = FakeServer()
        server_task = asyncio.create_task(asyncio.sleep(30))
        supervisor = SimpleNamespace(running=False, last_stop_reason=COMPLETION_REASON)
        try:
            result = await _monitor_completion(
                server=server,
                server_task=server_task,
                supervisor=supervisor,
                poll_seconds=0.01,
            )
        finally:
            server_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await server_task

        self.assertIsInstance(result, RuntimeMonitorResult)
        self.assertTrue(result.completed)
        self.assertTrue(server.should_exit)

    def test_runtime_health_allows_scheduled_market_closed_websocket_idle(
        self,
    ) -> None:
        checks = self._healthy_checks()
        checks["runtime"] = {
            "status": "ok",
            "session": {"should_run_live_modules": False},
            "instance_lock": {"acquired": True},
            "startup_tasks": {
                "maintenance": {"completed": True, "errors": []},
                "broker_sync": {"completed": True, "errors": []},
            },
        }
        checks["websocket"] = {
            "websocket_enabled": True,
            "websocket_connected": False,
            "websocket_status": "MARKET_CLOSED",
        }

        result = _evaluate_runtime_health(checks)

        self.assertTrue(result.ready)

    def test_runtime_health_requires_live_websocket_during_market(self) -> None:
        checks = self._healthy_checks()
        checks["websocket"] = {
            "websocket_enabled": True,
            "websocket_connected": False,
            "websocket_status": "DISCONNECTED",
        }

        result = _evaluate_runtime_health(checks)

        self.assertFalse(result.ready)
        self.assertIn("WebSocket: DISCONNECTED", result.issues)

    def test_runtime_health_fails_when_runtime_status_is_missing(self) -> None:
        checks = self._healthy_checks()
        checks["runtime"] = {"status": "unavailable", "error_type": "TimeoutError"}

        result = _evaluate_runtime_health(checks)

        self.assertFalse(result.ready)
        self.assertIn("Runtime status: TimeoutError", result.issues)

    def test_new_runtime_service_error_is_reported_once(self) -> None:
        status = {
            "error_count": 1,
            "recent_errors": [{"error": "research database failed"}],
            "services": {
                "collector": {"error_count": 0},
                "auto_trader": {"error_count": 2, "recent_errors": [{"error": "scan failed"}]},
                "outcome_monitor": {"error_count": 0},
            },
        }

        issues, current = _new_runtime_error_issues(
            status,
            {
                "automation_supervisor": 0,
                "collector": 0,
                "auto_trader": 1,
                "outcome_monitor": 0,
            },
        )
        repeated, _ = _new_runtime_error_issues(status, current)

        self.assertIn("Automation Supervisor: research database failed", issues)
        self.assertIn("Auto Trader: scan failed", issues)
        self.assertEqual(repeated, ())


if __name__ == "__main__":
    unittest.main()
