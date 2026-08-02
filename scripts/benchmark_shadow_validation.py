from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import fields
from datetime import datetime, timedelta

from app.config import settings
from app.models import Signal
from app.services.database import get_session, init_db
from app.services.episode_outcome_collector import (
    EpisodeOutcomeCollector,
    MarketPathEvent,
)
from app.services.evidence_persistence_queue import EvidencePersistenceQueue
from app.services.latency_metrics_service import LatencyMetricsService
from app.services.pre_order_risk_service import PreOrderRiskService
from app.services.trade_repository import TradeRepository


class PassingRisk:
    def evaluate_signal(self, symbol):  # type: ignore[no-untyped-def]
        return {
            "passed": True,
            "reasons": [],
            "summary": {"trades": 0, "pnl": 0.0, "stop_losses": 0},
            "open_exposure": {
                "open_trades": 0,
                "by_symbol": {},
                "premium_exposure": 0.0,
            },
            "limits": {"available_cash": 100000.0, "current_audited_equity": 100000.0},
            "risk_state": {
                "current_audited_equity": 100000.0,
                "realized_daily_pnl": 0.0,
                "unrealized_daily_pnl": 0.0,
                "current_drawdown_pct": 0.0,
                "consecutive_losses": 0,
                "planned_risk_today": 0.0,
                "total_open_risk": 0.0,
                "banknifty_open_risk": 0.0,
            },
        }


def signal(index: int) -> Signal:
    token = 700000 + index
    return Signal(
        symbol="BANKNIFTY",
        action="BUY_CE",
        side="BUY",
        tradingsymbol=f"BANKNIFTY99DEC{token}CE",
        exchange="NFO",
        instrument_token=token,
        strike=58000,
        expiry="2099-12-31",
        entry_price=100,
        stop_loss=95,
        target_1=110,
        quantity=15,
        lot_size=15,
        score=99,
        factor_scores={
            "strategy_metadata": {"strategy_version": settings.strategy_version},
            "risk_request": {"requested_tier": "TIER_1_BASE"},
            "contract": {"instrument_token": token, "expiry": "2099-12-31"},
        },
    )


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 4)


def summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "samples": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 4) if values else None,
    }


def benchmark_pre_order(
    *, asynchronous_evidence: bool, offset: int, samples: int
) -> tuple[dict[str, object], dict[str, object]]:
    metrics = LatencyMetricsService(sample_limit=max(100, samples))
    evidence_queue = (
        EvidencePersistenceQueue(max_size=samples + 10, start_worker=False)
        if asynchronous_evidence
        else None
    )
    service = PreOrderRiskService(
        risk_management_service=PassingRisk(),
        evidence_queue=evidence_queue,
        latency_metrics=metrics,
    )
    service._session_eligible = lambda: True  # type: ignore[method-assign]
    durations: list[float] = []
    for index in range(samples):
        started = time.perf_counter()
        result = service.evaluate_and_reserve(
            signal(offset + index),
            order_mode="paper",
            live_requested=False,
            execution_quality={
                "passed": True,
                "reasons": [],
                "best_ask": 100,
                "best_bid": 99,
                "entry_depth_quantity": 150,
                "exit_depth_quantity": 150,
            },
            metadata={"trigger_identifier": f"benchmark-{offset + index}"},
        )
        if not result["passed"]:
            raise RuntimeError(result)
        durations.append((time.perf_counter() - started) * 1000.0)
    return summary(durations), metrics.report()["metrics"]


def benchmark_sqlite_commit(samples: int = 100) -> dict[str, object]:
    durations: list[float] = []
    for _ in range(samples):
        session = get_session()
        try:
            started = time.perf_counter()
            session.commit()
            durations.append((time.perf_counter() - started) * 1000.0)
        finally:
            session.close()
    return summary(durations)


def benchmark_trade_persistence(samples: int = 100) -> dict[str, object]:
    repository = TradeRepository()
    durations: list[float] = []
    for index in range(samples):
        candidate = signal(5000 + index)
        started = time.perf_counter()
        repository.create_trade(
            candidate,
            mode="paper",
            status="filled",
            requested_quantity=15,
            placed_quantity=15,
            order_response={"benchmark": True},
        )
        durations.append((time.perf_counter() - started) * 1000.0)
    return summary(durations)


def benchmark_outcomes(samples: int = 500) -> dict[str, object]:
    collector = EpisodeOutcomeCollector(start_worker=False, persist_observations=False)
    started_at = datetime(2026, 7, 20, 10, 0, 0)
    collector.register_episode(
        "benchmark-episode",
        state="PREPARED",
        observed_at=started_at,
        context={"option_token": 800001, "target_1": 110, "stop_loss": 95},
    )
    enqueue_durations: list[float] = []
    process_durations: list[float] = []
    events: list[MarketPathEvent] = []
    for index in range(samples):
        timestamp = started_at + timedelta(milliseconds=index * 20)
        event = MarketPathEvent(
            instrument_token=800001,
            exchange_timestamp=timestamp,
            receive_timestamp=timestamp + timedelta(milliseconds=2),
            bid=100 + index / 1000,
            ask=101 + index / 1000,
            ltp=100.5 + index / 1000,
            bid_depth=150,
            ask_depth=150,
        )
        events.append(event)
        started = time.perf_counter()
        collector.enqueue(event)
        enqueue_durations.append((time.perf_counter() - started) * 1000.0)
    drain = EpisodeOutcomeCollector(start_worker=False, persist_observations=False)
    drain.register_episode(
        "benchmark-episode",
        state="PREPARED",
        observed_at=started_at,
        context={"option_token": 800001, "target_1": 110, "stop_loss": 95},
    )
    for event in events:
        started = time.perf_counter()
        drain.process_event(event)
        process_durations.append((time.perf_counter() - started) * 1000.0)
    return {
        "callback_enqueue": summary(enqueue_durations),
        "worker_processing": summary(process_durations),
    }


def main() -> None:
    snapshot = {
        item.name: getattr(settings, item.name) for item in fields(type(settings))
    }
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    try:
        init_db(f"sqlite:///{handle.name}")
        object.__setattr__(settings, "max_daily_planned_risk_percent", 10.0)
        object.__setattr__(settings, "max_realized_daily_loss_percent", 10.0)
        object.__setattr__(settings, "max_total_open_risk_percent", 10.0)
        object.__setattr__(settings, "max_banknifty_open_risk_percent", 10.0)
        asynchronous, components = benchmark_pre_order(
            asynchronous_evidence=True, offset=0, samples=200
        )
        synchronous, synchronous_components = benchmark_pre_order(
            asynchronous_evidence=False, offset=1000, samples=100
        )
        report = {
            "benchmark": "local_sqlite_shadow_validation_v1",
            "legacy_reported_baseline_ms": {"p50": 92, "p95": 111, "p99": 117},
            "safe_async_final_pre_order": asynchronous,
            "synchronous_evidence_comparison": synchronous,
            "component_metrics": components,
            "synchronous_component_metrics": synchronous_components,
            "sqlite_empty_commit": benchmark_sqlite_commit(),
            "trade_persistence": benchmark_trade_persistence(),
            "outcome_collector": benchmark_outcomes(),
            "target_p95_ms": 25.0,
            "target_met": float(asynchronous["p95_ms"] or 999999) <= 25.0,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        if not report["target_met"]:
            raise SystemExit("final synchronous pre-order p95 exceeded 25 ms")
    finally:
        for key, value in snapshot.items():
            object.__setattr__(settings, key, value)
        try:
            os.remove(handle.name)
        except (PermissionError, FileNotFoundError):
            pass


if __name__ == "__main__":
    main()
