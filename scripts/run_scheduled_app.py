from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn

logger = logging.getLogger(__name__)

COMPLETION_REASON = "after_market_pipeline_completed"
COMPLETION_MESSAGE = (
    "Bank Nifty trading work is complete for today. After-market research "
    "finished and the app shut down safely. You may switch off the laptop."
)
IST = ZoneInfo("Asia/Kolkata")


def _configure_scheduled_profile() -> None:
    """Apply lifecycle defaults that belong to this dedicated scheduled runner."""
    os.environ.setdefault("AUTOMATION_STOP_AFTER_AFTER_MARKET_COMPLETE", "true")
    os.environ.setdefault("SCHEDULED_RUN_EXIT_AFTER_COMPLETE", "true")


@dataclass(frozen=True)
class StartupHealthResult:
    ready: bool
    issues: tuple[str, ...]
    checks: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class RuntimeMonitorResult:
    completed: bool
    terminal_issue: str | None = None


def _safe_detail(value: object, *, fallback: str) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:240] if text else fallback


def _evaluate_startup_health(
    checks: dict[str, dict[str, Any]],
) -> StartupHealthResult:
    issues: list[str] = []

    api = checks.get("api", {})
    if api.get("status") != "ok":
        issues.append(
            "API: "
            + _safe_detail(
                api.get("message") or api.get("error_type"),
                fallback="health endpoint unavailable",
            )
        )

    database = checks.get("database", {})
    if database.get("status") != "ok":
        issues.append(
            "Database: "
            + _safe_detail(
                database.get("error_type") or database.get("message"),
                fallback="connectivity check failed",
            )
        )

    kite = checks.get("kite", {})
    if kite.get("status") != "ok":
        auth = kite.get("auth") if isinstance(kite.get("auth"), dict) else {}
        issues.append(
            "Kite login: "
            + _safe_detail(
                kite.get("message")
                or kite.get("status")
                or auth.get("last_error_reason"),
                fallback="profile verification failed",
            )
        )

    websocket = checks.get("websocket", {})
    if not websocket.get("websocket_enabled", False):
        issues.append("WebSocket: disabled")
    elif not websocket.get("websocket_connected", False):
        issues.append(
            "WebSocket: "
            + _safe_detail(
                websocket.get("last_error_reason")
                or websocket.get("last_disconnect_reason")
                or websocket.get("websocket_status"),
                fallback="not connected",
            )
        )
    else:
        owners = websocket.get("subscription_owners")
        owner_names = {
            str(owner)
            for token_owners in (owners.values() if isinstance(owners, dict) else [])
            for owner in (token_owners if isinstance(token_owners, list) else [])
        }
        if "core_market" not in owner_names:
            issues.append("Bank Nifty feed: core market subscription is not ready")
        gap = websocket.get("data_gap")
        if isinstance(gap, dict) and gap.get("active_gap"):
            issues.append("Market data: an active WebSocket gap is blocking entry")

    automation = checks.get("automation", {})
    lifecycle = (
        automation.get("lifecycle")
        if isinstance(automation.get("lifecycle"), dict)
        else {}
    )
    if not automation.get("running", False):
        issues.append("Automation: supervisor is not running")
    elif not lifecycle.get("task_healthy", False):
        issues.append("Automation: supervisor task is unhealthy")

    runtime = checks.get("runtime", {})
    if runtime.get("status") != "ok":
        issues.append(
            "Runtime status: "
            + _safe_detail(
                runtime.get("message") or runtime.get("error_type"),
                fallback="unavailable",
            )
        )
    instance_lock = (
        runtime.get("instance_lock")
        if isinstance(runtime.get("instance_lock"), dict)
        else {}
    )
    if runtime.get("status") == "ok" and not instance_lock.get("acquired", False):
        issues.append("Runtime ownership: single-instance lock is not held")
    startup_tasks = (
        runtime.get("startup_tasks")
        if isinstance(runtime.get("startup_tasks"), dict)
        else {}
    )
    for task_name in ("maintenance", "broker_sync"):
        task_status = (
            startup_tasks.get(task_name)
            if isinstance(startup_tasks.get(task_name), dict)
            else {}
        )
        if not task_status.get("completed", False):
            task_errors = task_status.get("errors")
            detail = (
                ", ".join(str(item) for item in task_errors[:3])
                if isinstance(task_errors, list) and task_errors
                else "still running"
            )
            issues.append(
                f"Startup {task_name.replace('_', ' ')}: "
                + _safe_detail(detail, fallback="not ready")
            )

    reconciliation = checks.get("reconciliation", {})
    last_reconciliation = (
        reconciliation.get("last_reconciliation")
        if isinstance(reconciliation.get("last_reconciliation"), dict)
        else {}
    )
    if reconciliation.get("blocked"):
        issues.append(
            "Broker reconciliation: "
            + _safe_detail(reconciliation.get("reason"), fallback="blocked")
        )
    elif last_reconciliation.get("status") != "ok":
        issues.append("Broker reconciliation: startup reconciliation is not ready")

    return StartupHealthResult(
        ready=not issues,
        issues=tuple(issues),
        checks=checks,
    )


def _read_json(base_url: str, path: str, *, timeout_seconds: float) -> dict[str, Any]:
    try:
        with urlopen(
            f"{base_url}{path}", timeout=max(0.5, timeout_seconds)
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "status": "unavailable",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    return payload if isinstance(payload, dict) else {"status": "invalid_response"}


async def _collect_startup_checks(
    *, base_url: str, request_timeout_seconds: float
) -> dict[str, dict[str, Any]]:
    endpoints = {
        "api": "/health",
        "database": "/db/health",
        "kite": "/kite/health",
        "websocket": "/kite/websocket/status",
        "automation": "/automation/status",
        "runtime": "/runtime/status",
        "reconciliation": "/broker/reconciliation/status",
    }
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                _read_json,
                base_url,
                path,
                timeout_seconds=request_timeout_seconds,
            )
            for path in endpoints.values()
        )
    )
    return dict(zip(endpoints, results, strict=True))


def _server_stopped_issue(server_task: asyncio.Task[None]) -> str:
    if server_task.cancelled():
        return "API server task was cancelled during startup"
    try:
        error = server_task.exception()
    except asyncio.CancelledError:
        return "API server task was cancelled during startup"
    if error is None:
        return "API server stopped before startup health checks passed"
    return f"API server failed: {type(error).__name__}: {_safe_detail(error, fallback='unknown error')}"


async def _monitor_startup_health(
    *,
    server_task: asyncio.Task[None],
    base_url: str,
    timeout_seconds: float,
    poll_seconds: float,
    request_timeout_seconds: float = 5.0,
) -> StartupHealthResult:
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
    last_result = StartupHealthResult(
        ready=False,
        issues=("Startup health endpoints have not responded yet",),
        checks={},
    )
    while asyncio.get_running_loop().time() < deadline:
        if server_task.done():
            return StartupHealthResult(
                ready=False,
                issues=(_server_stopped_issue(server_task),),
                checks=last_result.checks,
            )
        checks = await _collect_startup_checks(
            base_url=base_url,
            request_timeout_seconds=request_timeout_seconds,
        )
        last_result = _evaluate_startup_health(checks)
        if server_task.done():
            return StartupHealthResult(
                ready=False,
                issues=(_server_stopped_issue(server_task),),
                checks=last_result.checks,
            )
        if last_result.ready:
            return last_result
        await asyncio.sleep(max(0.5, poll_seconds))
    return StartupHealthResult(
        ready=False,
        issues=("Startup health verification timed out", *last_result.issues),
        checks=last_result.checks,
    )


def _startup_notification_message(
    result: StartupHealthResult, *, mode: str, strategy_version: str
) -> str:
    timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    if result.ready:
        return (
            "Bank Nifty app started successfully.\n"
            f"Time: {timestamp}\n"
            f"Mode: {mode.upper()}\n"
            f"Strategy: {strategy_version}\n"
            "Kite login: OK\n"
            "WebSocket: connected\n"
            "Bank Nifty feed: subscribed\n"
            "Database: OK\n"
            "Automation: running"
        )
    issue_lines = "\n".join(f"- {issue}" for issue in result.issues[:8])
    return (
        "Bank Nifty app startup FAILED.\n"
        f"Time: {timestamp}\n"
        f"Mode: {mode.upper()}\n"
        f"Strategy: {strategy_version}\n"
        f"Issues:\n{issue_lines}\n"
        "Check the latest scheduled-app stderr/stdout logs before trading."
    )


def _runtime_notification_message(
    issues: tuple[str, ...], *, recovered: bool = False
) -> str:
    timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    if recovered:
        return (
            "Bank Nifty app runtime recovered.\n"
            f"Time: {timestamp}\n"
            "The scheduled health checks are passing again."
        )
    issue_lines = "\n".join(f"- {issue}" for issue in issues[:8])
    return (
        "Bank Nifty app RUNTIME FAILURE.\n"
        f"Time: {timestamp}\n"
        f"Issues:\n{issue_lines}\n"
        "The app will keep retrying when safe. Check the dashboard and the latest "
        "scheduled-app logs."
    )


def _evaluate_runtime_health(
    checks: dict[str, dict[str, Any]],
) -> StartupHealthResult:
    runtime = checks.get("runtime", {})
    if runtime.get("status") != "ok":
        return StartupHealthResult(
            False,
            (
                "Runtime status: "
                + _safe_detail(
                    runtime.get("message") or runtime.get("error_type"),
                    fallback="unavailable",
                ),
            ),
            checks,
        )
    session = runtime.get("session") if isinstance(runtime.get("session"), dict) else {}
    if session.get("should_run_live_modules"):
        return _evaluate_startup_health(checks)

    issues: list[str] = []
    instance_lock = (
        runtime.get("instance_lock")
        if isinstance(runtime.get("instance_lock"), dict)
        else {}
    )
    if not instance_lock.get("acquired", False):
        issues.append("Runtime ownership: single-instance lock is not held")
    api = checks.get("api", {})
    if api.get("status") != "ok":
        issues.append(
            "API: "
            + _safe_detail(
                api.get("message") or api.get("error_type"),
                fallback="health endpoint unavailable",
            )
        )
    database = checks.get("database", {})
    if database.get("status") != "ok":
        issues.append(
            "Database: "
            + _safe_detail(
                database.get("error_type") or database.get("message"),
                fallback="connectivity check failed",
            )
        )
    automation = checks.get("automation", {})
    lifecycle = (
        automation.get("lifecycle")
        if isinstance(automation.get("lifecycle"), dict)
        else {}
    )
    if not automation.get("running", False):
        issues.append("Automation: supervisor stopped before day completion")
    elif not lifecycle.get("task_healthy", False):
        issues.append("Automation: supervisor task is unhealthy")
    return StartupHealthResult(not issues, tuple(issues), checks)


def _failure_exit_code(notification_result: dict[str, Any]) -> int:
    # Reserve 10 for a handled failure whose Telegram alert was confirmed.
    # Generic interpreter/unhandled failures normally use 1, so the outer
    # launcher must not mistake them for already-notified failures.
    return 10 if notification_result.get("status") == "sent" else 11


def _runtime_error_counts(status: dict[str, Any]) -> dict[str, int]:
    counts = {"automation_supervisor": int(status.get("error_count") or 0)}
    services = status.get("services") if isinstance(status.get("services"), dict) else {}
    for name in ("collector", "auto_trader", "outcome_monitor"):
        service = services.get(name) if isinstance(services.get(name), dict) else {}
        counts[name] = int(service.get("error_count") or 0)
    return counts


def _new_runtime_error_issues(
    status: dict[str, Any], previous: dict[str, int]
) -> tuple[tuple[str, ...], dict[str, int]]:
    current = _runtime_error_counts(status)
    issues: list[str] = []
    services = status.get("services") if isinstance(status.get("services"), dict) else {}
    sources: dict[str, dict[str, Any]] = {
        "automation_supervisor": status,
        **{
            name: value
            for name, value in services.items()
            if name in {"collector", "auto_trader", "outcome_monitor"}
            and isinstance(value, dict)
        },
    }
    for name, count in current.items():
        if count <= int(previous.get(name) or 0):
            continue
        source = sources.get(name, {})
        recent = source.get("recent_errors")
        latest = recent[-1] if isinstance(recent, list) and recent else {}
        if not isinstance(latest, dict):
            latest = {"error": latest}
        detail = _safe_detail(
            latest.get("error") or latest.get("message"),
            fallback=f"error count increased to {count}",
        )
        issues.append(f"{name.replace('_', ' ').title()}: {detail}")
    return tuple(issues), current


def _after_market_completion_reached(supervisor: Any) -> bool:
    return bool(
        not supervisor.running and supervisor.last_stop_reason == COMPLETION_REASON
    )


async def _monitor_completion(
    *,
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
    supervisor: Any,
    poll_seconds: float,
    notification_service: Any | None = None,
    base_url: str | None = None,
    request_timeout_seconds: float = 5.0,
    health_poll_seconds: float = 15.0,
) -> RuntimeMonitorResult:
    try:
        supervisor_status = supervisor.status()
    except Exception:  # noqa: BLE001 - supervisor status boundary
        supervisor_status = {}
    previous_error_counts = _runtime_error_counts(supervisor_status)
    alerted_service_incidents: set[str] = set()
    active_health_fingerprint: tuple[str, ...] | None = None
    health_failure_streak = 0
    next_health_check_at = 0.0
    while not server_task.done():
        if _after_market_completion_reached(supervisor):
            logger.info("After-market pipeline complete; requesting graceful shutdown")
            server.should_exit = True
            return RuntimeMonitorResult(completed=True)
        try:
            supervisor_status = supervisor.status()
        except Exception as exc:  # noqa: BLE001 - supervisor status boundary
            server.should_exit = True
            return RuntimeMonitorResult(
                completed=False,
                terminal_issue=(
                    "Automation status failed: "
                    f"{type(exc).__name__}: {_safe_detail(exc, fallback='unknown error')}"
                ),
            )
        lifecycle = (
            supervisor_status.get("lifecycle")
            if isinstance(supervisor_status.get("lifecycle"), dict)
            else {}
        )
        if not supervisor_status.get("running", False):
            server.should_exit = True
            return RuntimeMonitorResult(
                completed=False,
                terminal_issue=(
                    "Automation supervisor stopped unexpectedly: "
                    + _safe_detail(
                        lifecycle.get("last_stop_reason"), fallback="unknown reason"
                    )
                ),
            )
        if not lifecycle.get("task_healthy", False):
            server.should_exit = True
            return RuntimeMonitorResult(
                completed=False,
                terminal_issue="Automation supervisor task became unhealthy",
            )

        new_issues, previous_error_counts = _new_runtime_error_issues(
            supervisor_status, previous_error_counts
        )
        unseen_issues = tuple(
            issue for issue in new_issues if issue not in alerted_service_incidents
        )
        if unseen_issues and notification_service is not None:
            result = await asyncio.to_thread(
                notification_service.send,
                _runtime_notification_message(unseen_issues),
                kind="runtime_failure",
            )
            if result.get("status") == "sent":
                alerted_service_incidents.update(unseen_issues)
            logger.error(
                "Runtime service error notification_status=%s issues=%s",
                result.get("status"),
                list(unseen_issues),
            )

        loop_time = asyncio.get_running_loop().time()
        if base_url and loop_time >= next_health_check_at:
            checks = await _collect_startup_checks(
                base_url=base_url,
                request_timeout_seconds=request_timeout_seconds,
            )
            runtime_health = _evaluate_runtime_health(checks)
            next_health_check_at = loop_time + max(5.0, health_poll_seconds)
            if runtime_health.ready:
                health_failure_streak = 0
                if active_health_fingerprint and notification_service is not None:
                    await asyncio.to_thread(
                        notification_service.send,
                        _runtime_notification_message((), recovered=True),
                        kind="runtime_recovery",
                    )
                active_health_fingerprint = None
            else:
                health_failure_streak += 1
                fingerprint = tuple(runtime_health.issues)
                if (
                    health_failure_streak >= 2
                    and fingerprint != active_health_fingerprint
                    and notification_service is not None
                ):
                    result = await asyncio.to_thread(
                        notification_service.send,
                        _runtime_notification_message(fingerprint),
                        kind="runtime_failure",
                    )
                    logger.error(
                        "Runtime health notification_status=%s issues=%s",
                        result.get("status"),
                        list(fingerprint),
                    )
                    active_health_fingerprint = fingerprint
        await asyncio.sleep(max(0.5, poll_seconds))
    return RuntimeMonitorResult(completed=False)


async def _serve(
    *,
    host: str,
    port: int,
    poll_seconds: float,
    startup_timeout_seconds: float | None = None,
    startup_poll_seconds: float | None = None,
) -> int:
    # This entrypoint is exclusively for finite Windows scheduled runs. Force
    # the lifecycle profile here as well as in the PowerShell wrapper so a
    # direct invocation cannot silently become an overnight continuous server.
    os.environ["AUTOMATION_STOP_AFTER_AFTER_MARKET_COMPLETE"] = "true"
    os.environ["SCHEDULED_RUN_EXIT_AFTER_COMPLETE"] = "true"
    from app.config import settings
    from app.services.notification_service import NotificationService

    notification_service = NotificationService()
    try:
        from app import api as api_module

        app = api_module.app
        automation_supervisor_service = api_module.automation_supervisor_service
        notification_service = api_module.notification_service
    except Exception as exc:
        result = StartupHealthResult(
            ready=False,
            issues=(
                (
                    f"Application import failed: {type(exc).__name__}: "
                    f"{_safe_detail(exc, fallback='unknown error')}"
                ),
            ),
            checks={},
        )
        import_failure_notification = await asyncio.to_thread(
            notification_service.send,
            _startup_notification_message(
                result,
                mode=settings.default_order_mode,
                strategy_version=settings.strategy_version,
            ),
            kind="startup_failure",
        )
        logger.exception("Scheduled app import failed")
        return _failure_exit_code(import_failure_notification)

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    server_task = asyncio.create_task(server.serve())
    startup_result = await _monitor_startup_health(
        server_task=server_task,
        base_url=f"http://{host}:{port}",
        timeout_seconds=(
            startup_timeout_seconds
            if startup_timeout_seconds is not None
            else settings.scheduled_startup_health_timeout_seconds
        ),
        poll_seconds=(
            startup_poll_seconds
            if startup_poll_seconds is not None
            else settings.scheduled_startup_health_poll_seconds
        ),
    )
    startup_notification = await asyncio.to_thread(
        notification_service.send,
        _startup_notification_message(
            startup_result,
            mode=settings.default_order_mode,
            strategy_version=settings.strategy_version,
        ),
        kind="startup_success" if startup_result.ready else "startup_failure",
    )
    logger.info(
        "Startup health ready=%s issues=%s notification_status=%s",
        startup_result.ready,
        list(startup_result.issues),
        startup_notification.get("status"),
    )

    if server_task.done():
        try:
            await server_task
        except BaseException:
            logger.exception("Scheduled API server stopped during startup")
        return _failure_exit_code(startup_notification)

    if not startup_result.ready:
        server.should_exit = True
        try:
            await server_task
        except BaseException:
            logger.exception("Scheduled API server failed during startup shutdown")
        return _failure_exit_code(startup_notification)

    monitor_task = asyncio.create_task(
        _monitor_completion(
            server=server,
            server_task=server_task,
            supervisor=automation_supervisor_service,
            poll_seconds=poll_seconds,
            notification_service=notification_service,
            base_url=f"http://{host}:{port}",
        )
    )
    monitor_result = RuntimeMonitorResult(completed=False)
    server_error: BaseException | None = None
    try:
        await server_task
        monitor_result = await monitor_task
    except BaseException as exc:
        server_error = exc
        logger.exception("Scheduled API server failed after startup")
    finally:
        if not monitor_task.done():
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

    if monitor_result.completed:
        shutdown_status = dict(api_module.application_shutdown_status)
        if not shutdown_status.get("completed"):
            shutdown_issue = (
                "Application shutdown hooks did not complete"
                + (
                    f": {shutdown_status.get('error_type')}"
                    if shutdown_status.get("error_type")
                    else ""
                )
            )
            failure_notification = await asyncio.to_thread(
                notification_service.send,
                _runtime_notification_message((shutdown_issue,)),
                kind="runtime_failure",
            )
            return _failure_exit_code(failure_notification)
        result = await asyncio.to_thread(
            notification_service.send,
            COMPLETION_MESSAGE,
            kind="day_complete",
        )
        logger.info("Completion notification status=%s", result.get("status"))
        return 0
    if startup_result.ready:
        issue = monitor_result.terminal_issue or (
            f"API server failed after startup: {type(server_error).__name__}: "
            f"{_safe_detail(server_error, fallback='unknown error')}"
            if server_error is not None
            else "API server stopped before after-market completion"
        )
        failure_notification = await asyncio.to_thread(
            notification_service.send,
            _runtime_notification_message((issue,)),
            kind="runtime_failure",
        )
        logger.error(
            "Unexpected scheduled shutdown notification_status=%s",
            failure_notification.get("status"),
        )
        return _failure_exit_code(failure_notification)
    return 11


def main() -> int:
    _configure_scheduled_profile()
    parser = argparse.ArgumentParser(description="Run the scheduled trading app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--startup-timeout-seconds", type=float, default=None)
    parser.add_argument("--startup-poll-seconds", type=float, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(
        _serve(
            host=args.host,
            port=args.port,
            poll_seconds=args.poll_seconds,
            startup_timeout_seconds=args.startup_timeout_seconds,
            startup_poll_seconds=args.startup_poll_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
