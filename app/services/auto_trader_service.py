from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from threading import RLock, Thread
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import Signal
from app.services.notification_service import NotificationService
from app.services.opportunity_repository import OpportunityRepository
from app.services.order_service import OrderService
from app.services.risk_management_service import RiskManagementService
from app.services.scanner_service import ScannerService
from app.services.io_call_metrics_service import io_call_metrics


ScannerFactory = Callable[[], ScannerService]
OrderServiceFactory = Callable[[], OrderService]


class AutoTraderService:
    """Continuously scan for opportunities and optionally route them to orders."""

    def __init__(
        self,
        scanner_factory: ScannerFactory,
        order_service_factory: OrderServiceFactory,
        opportunity_repository: OpportunityRepository | None = None,
        risk_management_service: RiskManagementService | None = None,
        notification_service: NotificationService | None = None,
        latency_metrics: Any | None = None,
        fast_scan_context_service: Any | None = None,
        fast_candidate_promoter: Any | None = None,
        decision_evidence_repository: Any | None = None,
    ) -> None:
        self.scanner_factory = scanner_factory
        self.order_service_factory = order_service_factory
        self.opportunity_repository = opportunity_repository
        self.risk_management_service = (
            risk_management_service or RiskManagementService()
        )
        self.notification_service = notification_service or NotificationService()
        self.latency_metrics = latency_metrics
        self.fast_scan_context_service = fast_scan_context_service
        self.fast_candidate_promoter = fast_candidate_promoter
        self.decision_evidence_repository = decision_evidence_repository
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.config: dict[str, Any] = {}
        self.latest_opportunities: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.decision_events: list[dict[str, Any]] = []
        self.seen_order_keys: set[str] = set()
        self.last_scan_at: str | None = None
        self.last_scan_result: dict[str, Any] | None = None
        self._decision_event_lock = RLock()
        self._fast_rescan_lock = RLock()
        self._scan_lock = RLock()
        self._fast_rescan_running = False
        self._last_fast_rescan_at: datetime | None = None
        self.last_fast_rally_event: dict[str, Any] | None = None
        self.last_fast_dispatch: dict[str, Any] | None = None
        self.last_fast_candidate_decision: dict[str, Any] | None = None
        self.fast_rally_request_count = 0
        self.fast_validation_scheduled_count = 0
        self.fast_validation_suppressed_count = 0
        self.fast_validation_completed_count = 0
        self.fast_validation_passed_count = 0
        self.fast_validation_rejected_count = 0
        self.fast_validation_error_count = 0
        self.fast_suppressed_reasons: dict[str, int] = {}

    def _now_ist(self) -> str:
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
            "%d %b %Y, %I:%M:%S %p IST"
        )

    def _append_decision_event(self, event: dict[str, Any]) -> None:
        with self._decision_event_lock:
            self.decision_events.append(dict(event))
            self.decision_events = self.decision_events[-100:]

    def recent_decision_events(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._decision_event_lock:
            return [dict(item) for item in self.decision_events[-max(1, int(limit)) :]]

    def start(
        self,
        *,
        side: str = "BUY",
        symbols: list[str] | None = None,
        interval_seconds: int | None = None,
        limit: int = 5,
        place_orders: bool = False,
        confirm_live: bool = False,
        order_mode: str | None = None,
    ) -> dict[str, Any]:
        if self.running:
            return self.status()

        interval = interval_seconds or settings.scanner_interval_seconds
        self.config = {
            "side": side.upper(),
            "symbols": symbols,
            "interval_seconds": max(1, int(interval)),
            "limit": max(1, int(limit)),
            "place_orders": bool(place_orders),
            "confirm_live": bool(confirm_live),
            "order_mode": (
                order_mode or settings.default_order_mode or "paper"
            ).lower(),
        }
        self.running = True
        self.task = asyncio.create_task(self._run())
        if not settings.scheduled_run_exit_after_complete:
            self.notification_service.send(
                f"Auto trader started: side={side.upper()}, symbols={symbols or 'default'}, interval={max(1, int(interval))}s"
            )
        return self.status()

    async def stop(self) -> dict[str, Any]:
        self.running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        return self.status()

    def status(self) -> dict[str, Any]:
        order_mode = str(
            self.config.get("order_mode") or settings.default_order_mode or "paper"
        ).lower()
        return {
            "running": self.running,
            "config": self.config,
            "last_scan_at": self.last_scan_at,
            "latest_count": len(self.latest_opportunities),
            "execution_count": len(self.executions),
            "error_count": len(self.errors),
            "recent_errors": self.errors[-5:],
            "last_scan_result": dict(self.last_scan_result)
            if self.last_scan_result
            else None,
            "recent_decisions": self.recent_decision_events(limit=5),
            "mode": order_mode,
            "live_ordering_requires_confirm_live": True,
            "risk_limits_apply_to_order_mode": order_mode == "live",
            "risk": {
                "status": "deferred",
                "reason": "status_is_lightweight",
                "detail": "Use /risk/status for an explicit broker/risk evaluation.",
            },
            "io_call_budgets": io_call_metrics.report(),
            "last_fast_candidate_decision": self.last_fast_candidate_decision,
            "fast_rally": {
                "request_count": self.fast_rally_request_count,
                "validation_scheduled_count": self.fast_validation_scheduled_count,
                "validation_suppressed_count": self.fast_validation_suppressed_count,
                "validation_completed_count": self.fast_validation_completed_count,
                "validation_passed_count": self.fast_validation_passed_count,
                "validation_rejected_count": self.fast_validation_rejected_count,
                "validation_error_count": self.fast_validation_error_count,
                "validation_running": self._fast_rescan_running,
                "suppressed_reasons": dict(self.fast_suppressed_reasons),
                "last_event": dict(self.last_fast_rally_event)
                if self.last_fast_rally_event
                else None,
                "last_dispatch": dict(self.last_fast_dispatch)
                if self.last_fast_dispatch
                else None,
            },
        }

    async def _run(self) -> None:
        while self.running:
            try:
                await asyncio.to_thread(self.scan_once)
            except Exception as exc:
                self.errors.append(
                    {
                        "time": self._now_ist(),
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "source": "scheduled_scan",
                    }
                )
            await asyncio.sleep(
                float(
                    self.config.get(
                        "interval_seconds", settings.scanner_interval_seconds
                    )
                )
            )

    def scan_once(self) -> dict[str, Any]:
        if not self._scan_lock.acquire(blocking=False):
            return {
                "skipped": True,
                "reason": "scan_already_running",
                "last_scan_at": self.last_scan_at,
            }
        try:
            return self._scan_once_impl()
        finally:
            self._scan_lock.release()

    def _scan_once_impl(self) -> dict[str, Any]:
        scan_started = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        with io_call_metrics.measure("scheduled_scan") as calls:
            scanner = self.scanner_factory()
            symbols = self.config.get("symbols")
            opportunities = scanner.scan_symbols(
                symbols=symbols,
                side=str(self.config.get("side", "BUY")),
                order_mode=str(self.config.get("order_mode") or "paper"),
                rejection_source="automation_scan",
            )
            budget_exceeded = calls.rest_calls > settings.scheduled_scan_max_rest_calls
            if budget_exceeded:
                self.errors.append(
                    {
                        "time": self._now_ist(),
                        "error": "scheduled_scan_rest_call_budget_exceeded",
                        "error_type": "RestCallBudgetExceeded",
                        "source": "scheduled_scan",
                        "rest_calls": calls.rest_calls,
                        "budget": settings.scheduled_scan_max_rest_calls,
                    }
                )
                opportunities = []
            result = self._finalize_opportunities(
                opportunities, source="automation_scan"
            )
            result["io_calls"] = {
                "rest_calls": calls.rest_calls,
                "database_queries": calls.database_queries,
                "rest_call_budget": settings.scheduled_scan_max_rest_calls,
                "budget_exceeded": budget_exceeded,
            }
        scan_completed = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        if self.latency_metrics is not None:
            self.latency_metrics.record_between(
                "scheduled_scan_duration",
                scan_started,
                scan_completed,
                detail={
                    "accepted_candidates": len(opportunities),
                    "scan_source": "scheduled",
                    **result["io_calls"],
                },
            )
        self.last_scan_result = {
            "time": self.last_scan_at,
            "source": "scheduled_scan",
            "status": "completed",
            "opportunity_count": int(result.get("count") or 0),
            "placed_count": len(result.get("placed") or []),
            "io_calls": dict(result.get("io_calls") or {}),
        }
        return result

    def _finalize_opportunities(
        self, opportunities: list[Signal], *, source: str
    ) -> dict[str, Any]:
        limited = opportunities[: int(self.config.get("limit", 5))]
        self.latest_opportunities = [asdict(signal) for signal in limited]
        saved_ids: list[int] = []
        saved_id_by_order_key: dict[str, int] = {}
        if self.opportunity_repository is not None:
            for signal in limited:
                saved = self.opportunity_repository.save_opportunity(signal)
                saved_ids.append(saved.id)
                saved_id_by_order_key[self._order_key(signal)] = saved.id
        self.last_scan_at = self._now_ist()

        placed: list[dict[str, Any]] = []
        if self.config.get("place_orders"):
            order_mode = str(self.config.get("order_mode") or "paper").lower()
            for signal in limited:
                try:
                    decision_at = datetime.now(ZoneInfo("Asia/Kolkata")).replace(
                        tzinfo=None
                    )
                    result = self._place_once(
                        signal,
                        opportunity_id=saved_id_by_order_key.get(
                            self._order_key(signal)
                        ),
                    )
                    if self.latency_metrics is not None:
                        self.latency_metrics.record_between(
                            "decision_to_order_completion",
                            decision_at,
                            detail={
                                "symbol": signal.symbol,
                                "source": source,
                                "order_mode": self.config.get("order_mode"),
                            },
                        )
                    if result is not None:
                        placed.append(result)
                except Exception as exc:
                    message = str(exc)
                    if order_mode == "live" and message.startswith(
                        "risk guard blocked order:"
                    ):
                        try:
                            shadow = self._place_shadow_paper(
                                signal,
                                opportunity_id=saved_id_by_order_key.get(
                                    self._order_key(signal)
                                ),
                                reason=message,
                            )
                            placed.append(shadow)
                        except Exception as shadow_exc:
                            self.errors.append(
                                {
                                    "time": self._now_ist(),
                                    "error": str(shadow_exc),
                                    "shadow_reason": message,
                                    "order_key": self._order_key(signal),
                                }
                            )
                    else:
                        self.errors.append(
                            {
                                "time": self._now_ist(),
                                "error": message,
                                "order_key": self._order_key(signal),
                            }
                        )

        return {
            "last_scan_at": self.last_scan_at,
            "count": len(self.latest_opportunities),
            "saved_ids": saved_ids,
            "opportunities": self.latest_opportunities,
            "placed": placed,
            "source": source,
        }

    def request_fast_rescan(self, event: dict[str, Any]) -> dict[str, Any]:
        """Schedule one non-blocking scan when Bank Nifty accelerates."""
        self.last_fast_rally_event = dict(event)
        self.fast_rally_request_count += 1
        if not self.running:
            return self._record_fast_dispatch(
                event,
                {
                    "scheduled": False,
                    "reason": "auto_trader_not_running",
                    "stage": "suppressed",
                },
            )
        now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        with self._fast_rescan_lock:
            cooldown = max(0.1, float(settings.fast_rally_rescan_cooldown_seconds))
            if self._fast_rescan_running:
                return self._record_fast_dispatch(
                    event,
                    {
                        "scheduled": False,
                        "reason": "fast_rescan_already_running",
                        "stage": "suppressed",
                    },
                )
            if (
                self._last_fast_rescan_at
                and (now - self._last_fast_rescan_at).total_seconds() < cooldown
            ):
                return self._record_fast_dispatch(
                    event,
                    {
                        "scheduled": False,
                        "reason": "fast_rescan_cooldown",
                        "stage": "suppressed",
                        "cooldown_seconds": cooldown,
                    },
                )
            self._fast_rescan_running = True
            self._last_fast_rescan_at = now
        result = {
            "scheduled": True,
            "stage": "candidate_promoted_for_cached_validation",
        }
        try:
            Thread(
                target=self._run_fast_candidate_validation,
                args=(dict(event),),
                name="banknifty-fast-candidate",
                daemon=True,
            ).start()
        except Exception as exc:
            with self._fast_rescan_lock:
                self._fast_rescan_running = False
            result = {
                "scheduled": False,
                "stage": "dispatch_failed",
                "reason": "fast_validation_thread_start_failed",
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        return self._record_fast_dispatch(event, result)

    def _run_fast_candidate_validation(self, event: dict[str, Any]) -> None:
        try:
            direction = str(event.get("direction") or "").lower()
            if direction not in {"bullish", "bearish"}:
                raise ValueError("fast candidate direction is invalid")
            scan_started = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            rally_at = self._parse_event_time(event.get("timestamp"))
            with io_call_metrics.measure("fast_candidate_validation") as calls:
                decision = (
                    self.fast_scan_context_service.validate_candidate(event)
                    if self.fast_scan_context_service is not None
                    else {
                        "passed": False,
                        "reason": "fast_scan_context_service_missing",
                    }
                )
                decision["io_calls"] = {
                    "rest_calls": calls.rest_calls,
                    "database_queries": calls.database_queries,
                }
                if calls.rest_calls or calls.database_queries:
                    original_reason = decision.get("reason")
                    decision.update(
                        {
                            "passed": False,
                            "reason": "fast_candidate_io_budget_exceeded",
                            "underlying_reason": original_reason,
                        }
                    )
                decision["rally_event"] = dict(event)
                self.last_fast_candidate_decision = decision
            if (
                bool(decision.get("passed"))
                and decision.get("action") == "promote_precomputed_plan_to_armed_entry"
            ):
                if self.fast_candidate_promoter is None:
                    decision.update(
                        {"passed": False, "reason": "fast_candidate_promoter_missing"}
                    )
                else:
                    promotion = self.fast_candidate_promoter.register_from_fast_plan(
                        dict(decision.get("plan") or {})
                    )
                    decision["promotion"] = promotion
                    if promotion.get("registered"):
                        decision["stage"] = "fast_candidate_promoted_to_armed_entry"
                        decision["reason"] = (
                            "fast_candidate_armed_waiting_for_tick_quality"
                        )
                    else:
                        decision.update(
                            {
                                "passed": False,
                                "reason": str(
                                    promotion.get("reason")
                                    or "fast_candidate_promotion_failed"
                                ),
                            }
                        )
                self.last_fast_candidate_decision = decision
            scan_completed = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
            if self.latency_metrics is not None:
                self.latency_metrics.record_between(
                    "rally_detection_to_scan_start",
                    rally_at,
                    scan_started,
                    detail=decision,
                )
                self.latency_metrics.record_between(
                    "fast_rally_detection_to_scan_start",
                    rally_at,
                    scan_started,
                    detail=decision,
                )
                self.latency_metrics.record_between(
                    "cached_candidate_decision_duration",
                    scan_started,
                    scan_completed,
                    detail=decision,
                )
                self.latency_metrics.record_between(
                    "fast_rally_scan_duration",
                    scan_started,
                    scan_completed,
                    detail=decision,
                )
            self._append_decision_event(
                {
                    "time": self._now_ist(),
                    "event_type": "fast_rally_candidate_validation",
                    "source": "fast_rally_candidate_validation",
                    "passed": bool(decision.get("passed")),
                    "reason": decision.get("reason"),
                    "direction": direction,
                    "move_pct": event.get("move_pct"),
                    "instrument_token": event.get("instrument_token"),
                }
            )
            if self.decision_evidence_repository is not None:
                plan = (
                    decision.get("plan")
                    if isinstance(decision.get("plan"), dict)
                    else {}
                )
                promotion = (
                    decision.get("promotion")
                    if isinstance(decision.get("promotion"), dict)
                    else {}
                )
                self.decision_evidence_repository.record_decision(
                    decision_type="fast_rally",
                    final_state="ARMED" if promotion.get("registered") else "OBSERVE",
                    symbol="BANKNIFTY",
                    tradingsymbol=str(plan.get("tradingsymbol"))
                    if plan.get("tradingsymbol")
                    else None,
                    context={"rally_event": event, "cached_decision": decision},
                    gate_results={
                        "passed": bool(decision.get("passed")),
                        "reason": decision.get("reason"),
                    },
                    transition_timestamps={
                        "rally_at": event.get("timestamp"),
                        "validation_started_at": scan_started.isoformat(sep=" "),
                        "validation_completed_at": scan_completed.isoformat(sep=" "),
                    },
                )
            self.fast_validation_completed_count += 1
            if bool(decision.get("passed")):
                self.fast_validation_passed_count += 1
            else:
                self.fast_validation_rejected_count += 1
            if self.latency_metrics is not None:
                event_time = self._parse_event_time(
                    event.get("receive_timestamp") or event.get("timestamp")
                )
                self.latency_metrics.record_between(
                    "tick_to_candidate_decision",
                    event_time,
                    detail={
                        "direction": direction,
                        "candidate_passed": bool(decision.get("passed")),
                    },
                )
        except Exception as exc:
            failure = {
                "time": self._now_ist(),
                "error": str(exc),
                "error_type": type(exc).__name__,
                "source": "fast_rally_candidate_validation",
            }
            self.errors.append(failure)
            self.fast_validation_error_count += 1
            self.last_fast_candidate_decision = {
                "passed": False,
                "reason": "fast_candidate_validation_error",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "rally_event": dict(event),
            }
            self._append_decision_event(
                {
                    "time": failure["time"],
                    "event_type": "fast_rally_candidate_validation",
                    "source": "fast_rally_candidate_validation",
                    "passed": False,
                    "reason": "fast_candidate_validation_error",
                    "direction": str(event.get("direction") or "").lower() or None,
                    "move_pct": event.get("move_pct"),
                    "instrument_token": event.get("instrument_token"),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            with self._fast_rescan_lock:
                self._fast_rescan_running = False

    def _record_fast_dispatch(
        self, event: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            **dict(result),
            "time": self._now_ist(),
            "direction": str(event.get("direction") or "").lower() or None,
            "move_pct": event.get("move_pct"),
            "instrument_token": event.get("instrument_token"),
        }
        self.last_fast_dispatch = dict(payload)
        scheduled = bool(payload.get("scheduled"))
        if scheduled:
            self.fast_validation_scheduled_count += 1
        else:
            self.fast_validation_suppressed_count += 1
            reason = str(payload.get("reason") or "fast_validation_not_scheduled")
            self.fast_suppressed_reasons[reason] = (
                self.fast_suppressed_reasons.get(reason, 0) + 1
            )
        self._append_decision_event(
            {
                **payload,
                "event_type": "fast_rally_dispatch",
                "source": "banknifty_fast_rally",
                "passed": scheduled,
            }
        )
        return dict(result)

    def _parse_event_time(self, value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Asia/Kolkata")).replace(
                    tzinfo=None
                )
            return parsed
        except (TypeError, ValueError):
            return None

    def _order_key(self, signal: Signal) -> str:
        return "|".join(
            [
                str(signal.tradingsymbol or signal.symbol),
                signal.side.upper(),
                signal.action,
                str(signal.expiry or ""),
                str(signal.strike or ""),
            ]
        )

    def _place_once(
        self, signal: Signal, opportunity_id: int | None = None
    ) -> dict[str, Any] | None:
        order_key = self._order_key(signal)
        service = self.order_service_factory()
        result = service.place_signal_order(
            signal,
            confirm_live=bool(self.config.get("confirm_live", False)),
            opportunity_id=opportunity_id,
            order_mode=str(self.config.get("order_mode") or "paper"),
        )
        execution = {
            "time": self._now_ist(),
            "order_key": order_key,
            "signal": asdict(signal),
            "result": result,
        }
        self.executions.append(execution)
        self.notification_service.send(
            f"Auto trader placed {result.get('status')} order for {signal.tradingsymbol or signal.symbol}"
        )
        return execution

    def _place_shadow_paper(
        self, signal: Signal, opportunity_id: int | None = None, reason: str = ""
    ) -> dict[str, Any]:
        service = self.order_service_factory()
        result = service.place_signal_order(
            signal,
            confirm_live=False,
            opportunity_id=opportunity_id,
            order_mode="paper",
        )
        result = {**result, "shadow_for_live": True, "shadow_reason": reason}
        execution = {
            "time": self._now_ist(),
            "order_key": self._order_key(signal),
            "signal": asdict(signal),
            "result": result,
        }
        self.executions.append(execution)
        self.notification_service.send(
            f"Auto trader recorded paper shadow for {signal.tradingsymbol or signal.symbol}"
        )
        return execution
