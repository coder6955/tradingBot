import asyncio
import json
import logging
import secrets
import threading
import time as time_module
from dataclasses import asdict
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy import text

from app.application_context import ApplicationContext
from app.config import settings
from app.dashboard_page import render_command_center_dashboard
from app.loginAutoCode import ensure_access_token_for_today
from app.services.market_data_service import MarketDataService
from app.services.scanner_service import ScannerService
from app.services.signal_repository import SignalRepository
from app.services.order_service import OrderService
from app.services.paper_trading_service import PaperTradingService
from app.services.auto_trader_service import AutoTraderService
from app.services.automation_supervisor_service import AutomationSupervisorService
from app.services.opportunity_repository import OpportunityRepository
from app.services.opportunity_outcome_service import OpportunityOutcomeService
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.rejected_opportunity_outcome_service import (
    RejectedOpportunityOutcomeService,
)
from app.services.broker_sync_service import BrokerSyncService
from app.services.backtest_service import BacktestService
from app.services.data_ingestion_service import DataIngestionService
from app.services.day_type_service import DayTypeService
from app.services.decision_evidence_repository import DecisionEvidenceRepository
from app.services.evidence_persistence_queue import EvidencePersistenceQueue
from app.services.episode_outcome_collector import EpisodeOutcomeCollector
from app.services.entry_policy_shadow_service import ShadowEntryPolicyComparisonService
from app.services.database import get_session
from app.services.greeks_service import GreeksService
from app.services.notification_service import NotificationService
from app.services.execution_analytics_service import ExecutionAnalyticsService
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.after_market_research_service import AfterMarketResearchService
from app.services.armed_entry_tracker_service import ArmedEntryTrackerService
from app.services.armed_entry_repository import ArmedEntryRepository
from app.services.evidence_matrix_service import EvidenceMatrixService
from app.services.strategy_promotion_service import StrategyPromotionService
from app.services.banknifty_option_prewarm_service import BankNiftyOptionPrewarmService
from app.services.banknifty_fast_rally_service import BankNiftyFastRallyService
from app.services.banknifty_intelligence_service import BankNiftyIntelligenceService
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed
from app.services.latency_metrics_service import LatencyMetricsService
from app.services.io_call_metrics_service import io_call_metrics
from app.services.raw_tick_capture_service import RawTickCaptureService
from app.services.underlying_candle_service import UnderlyingCandleService
from app.services.volatility_edge_service import VolatilityEdgeService
from app.services.fast_scan_context_service import FastScanContextService
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.market_data_runtime_service import MarketDataRuntimeService
from app.services.market_session_service import MarketSessionService
from app.services.opportunity_analytics_service import OpportunityAnalyticsService
from app.services.option_history_repository import OptionHistoryRepository
from app.services.option_premium_confirmation_service import (
    OptionPremiumConfirmationService,
)
from app.services.option_quality_service import OptionQualityService
from app.services.option_snapshot_collector_service import (
    OptionSnapshotCollectorService,
)
from app.services.outcome_learning_service import OutcomeLearningService
from app.services.professional_readiness_service import ProfessionalReadinessService
from app.services.professional_insights_service import ProfessionalInsightsService
from app.services.risk_management_service import RiskManagementService
from app.services.pre_order_risk_service import PreOrderRiskService
from app.services.risk_policy_service import RiskPolicyService
from app.services.research_reporting_service import (
    ResearchDatasetAuditService,
    ShadowPolicyResearchReportService,
)
from app.services.runtime_trading_config_service import RuntimeTradingConfigService
from app.services.runtime_job_repository import RuntimeJobRepository
from app.services.runtime_instance_lock import RuntimeInstanceLock
from app.services.strategy_edge_service import StrategyEdgeService
from app.services.strategy_validation_repository import StrategyValidationRepository
from app.services.strategy_version_registry import StrategyVersionRegistry
from app.services.time_bucket_edge_service import TimeBucketEdgeService
from app.services.trade_exit_service import TradeExitService
from app.services.trade_repository import TradeRepository
from app.services.trade_setup_service import OptionContract, TradeSetupService
from app.services.time_utils import format_ist, ist_now_naive
from app.providers.kite_provider import KiteProvider
from app.providers.kite_auth_state import kite_auth_state
from app.providers.kite_feed import KiteFeed
from app.providers.token_store import load_access_token, save_access_token, token_status
from fastapi.responses import RedirectResponse, HTMLResponse

API_DESCRIPTION = """
AI Option Trader API workflow.

Recommended sequence:

1. System checks: `GET /health`, `GET /db/health`
2. Kite login: `GET /kite/auth`, then verify with `GET /kite/health`
3. Account checks: `GET /kite/margins`, `GET /kite/positions`
4. Start automation: `POST /automation/start`
5. Watch automation: `GET /automation/status`, `GET /dashboard`
6. Manual override if needed: `GET /scanner/diagnostics?side=BUY&symbols=BANKNIFTY`
7. Research quality: `GET /research/after-market/status`, `GET /research/professional-readiness`, `GET /research/opportunity-analytics`, `GET /research/execution-analytics`, `GET /research/outcome-learning`, `POST /research/backtest/options`, `POST /research/walk-forward`
8. Watch saved opportunities: `GET /opportunities`, `GET /opportunities/performance`
9. Study failures: `POST /opportunities/evaluate-open`, `GET /opportunities/failure-analysis`
10. Live orders only after validation: set `LIVE_TRADING_MODE=true`, `PAPER_TRADING_MODE=false`, `AUTOMATION_PLACE_ORDERS=true`, and `AUTOMATION_CONFIRM_LIVE=true`

Safety note: signals are score-ranked trade setups; probability remains null until calibrated out-of-sample evidence is sufficient.
"""

logger = logging.getLogger(__name__)
_research_report_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_research_report_cache_lock = threading.Lock()

OPENAPI_TAGS = [
    {
        "name": "01 System",
        "description": "Start here: app health, DB health, and workflow overview.",
    },
    {
        "name": "02 Kite Login",
        "description": "Authenticate with Zerodha Kite and verify account connectivity.",
    },
    {
        "name": "03 Scanner",
        "description": "Find and diagnose option opportunities. Emitted opportunities are saved to DB.",
    },
    {
        "name": "04 Orders",
        "description": "Place paper orders by default; live orders require explicit live config and confirmation.",
    },
    {
        "name": "05 Auto Trader",
        "description": "Continuously scan for opportunities and optionally place orders.",
    },
    {
        "name": "06 Opportunity Journal",
        "description": "Review saved opportunities, mark outcomes, and study failures.",
    },
    {
        "name": "07 Outcome Monitor",
        "description": "Automatically evaluate open opportunities against stop/target prices.",
    },
    {
        "name": "08 Paper Trading",
        "description": "Inspect and close simulated in-memory paper trades.",
    },
    {"name": "09 Market Data", "description": "Read stored market summaries."},
    {
        "name": "10 Research",
        "description": "Market insight filters, Greeks, IV, option-quality filters, and historical rule replay.",
    },
    {
        "name": "11 Data Ingestion",
        "description": "Pull historical candles and option-chain snapshots into MySQL.",
    },
    {
        "name": "12 Automation",
        "description": "One-switch supervisor for daily ingestion, collectors, scanners, and monitors.",
    },
]

app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
)


@app.middleware("http")
async def log_request_timing(request: Request, call_next):
    started = time_module.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = time_module.perf_counter() - started
        logger.exception(
            "request failed after %.3fs path=%s", elapsed, request.url.path
        )
        raise
    elapsed = time_module.perf_counter() - started
    response.headers["X-Process-Time-ms"] = str(round(elapsed * 1000, 3))
    if elapsed >= float(settings.slow_api_log_threshold_seconds):
        logger.warning(
            "slow request duration_ms=%.3f method=%s path=%s status_code=%s",
            elapsed * 1000,
            request.method,
            request.url.path,
            response.status_code,
        )
    return response


def _extract_api_auth_token(
    authorization: str | None, x_api_key: str | None
) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
        return authorization.strip()
    return None


def require_api_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    configured_token = settings.api_auth_token
    if not configured_token:
        if settings.api_auth_required:
            raise HTTPException(
                status_code=503, detail="API auth token is not configured"
            )
        return
    supplied_token = _extract_api_auth_token(authorization, x_api_key)
    if not supplied_token or not secrets.compare_digest(
        str(configured_token), supplied_token
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


PROTECTED_ROUTE = [Depends(require_api_auth)]


def _bounded_int(
    value: object, default: int, *, minimum: int = 1, maximum: int = 3000
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _cached_research_report(
    key: tuple[Any, ...],
    *,
    cache_seconds: int,
    builder: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    ttl = max(0, min(int(cache_seconds or 0), 900))
    if ttl <= 0:
        result = builder()
        result["cache_hit"] = False
        return result
    now = time_module.monotonic()
    with _research_report_cache_lock:
        cached = _research_report_cache.get(key)
        if cached and now - cached[0] <= ttl:
            result = dict(cached[1])
            result["cache_hit"] = True
            return result
    result = builder()
    result["cache_hit"] = False
    with _research_report_cache_lock:
        _research_report_cache[key] = (now, dict(result))
    return result


market_data_service = MarketDataService()
signal_repository = SignalRepository()
paper_trading_service = PaperTradingService()
opportunity_repository = OpportunityRepository()
rejected_opportunity_repository = RejectedOpportunityRepository()
trade_repository = TradeRepository()
decision_evidence_repository = DecisionEvidenceRepository()
episode_outcome_collector = EpisodeOutcomeCollector(
    repository=decision_evidence_repository,
    start_worker=False,
)
episode_outcome_collector.shadow_policy_service = ShadowEntryPolicyComparisonService()
evidence_persistence_queue = EvidencePersistenceQueue(
    repository=decision_evidence_repository,
    start_worker=False,
)
research_dataset_audit_service = ResearchDatasetAuditService()
shadow_policy_research_report_service = ShadowPolicyResearchReportService()
risk_management_service = RiskManagementService(trade_repository)
runtime_trading_config_service = RuntimeTradingConfigService()
notification_service = NotificationService()
runtime_instance_lock = RuntimeInstanceLock(
    Path(__file__).resolve().parents[1] / "logs" / "banknifty-app.lock"
)
greeks_service = GreeksService()
option_quality_service = OptionQualityService(greeks_service)
backtest_service = BacktestService()
option_history_repository = OptionHistoryRepository()
shared_kite_feed = KiteFeed() if settings.use_kite_market_data else None
market_session_service = MarketSessionService()
latency_metrics_service = LatencyMetricsService()
episode_outcome_collector.latency_metrics = latency_metrics_service
raw_tick_capture_service = RawTickCaptureService()
underlying_candle_service = UnderlyingCandleService(
    market_session_service=market_session_service
)
kite_websocket_price_feed = KiteWebSocketPriceFeed(
    market_session_service=market_session_service,
    latency_metrics=latency_metrics_service,
    underlying_tick_handler=underlying_candle_service.on_tick,
    raw_tick_handler=lambda tick, context: raw_tick_capture_service.capture(
        tick,
        symbol=str(context.get("symbol") or "") or None,
        owners=context.get("owners") or [],
    ),
)
active_trade_price_feed = ActiveTradePriceFeed(kite_websocket_price_feed)


def _cached_executable_bid_for_equity(trade: Any) -> float | None:
    token = getattr(trade, "instrument_token", None)
    if not token:
        return None
    tick = kite_websocket_price_feed.get_latest_tick(int(token))
    if tick is None or tick.bid is None or tick.bid <= 0:
        return None
    observed_at = tick.receive_timestamp or tick.timestamp
    age = (ist_now_naive() - observed_at.replace(tzinfo=None)).total_seconds()
    return (
        float(tick.bid)
        if age <= float(settings.websocket_price_stale_seconds)
        else None
    )


risk_management_service.account_equity_state_service.executable_bid_provider = (
    _cached_executable_bid_for_equity
)
banknifty_option_prewarm_service = BankNiftyOptionPrewarmService(
    kite_websocket_price_feed
)
shared_trade_setup_service = TradeSetupService()
market_data_runtime_service = MarketDataRuntimeService(
    websocket_feed=kite_websocket_price_feed,
    active_price_feed=active_trade_price_feed,
    prewarm_service=banknifty_option_prewarm_service,
)
strategy_validation_repository = StrategyValidationRepository()
strategy_version_registry = StrategyVersionRegistry()
banknifty_intelligence_service = BankNiftyIntelligenceService()
strategy_edge_service = StrategyEdgeService(
    backtest_service=backtest_service, repository=strategy_validation_repository
)
day_type_service = DayTypeService()
option_premium_confirmation_service = OptionPremiumConfirmationService()
time_bucket_edge_service = TimeBucketEdgeService(backtest_service=backtest_service)
volatility_edge_service = VolatilityEdgeService()
fast_scan_context_service = FastScanContextService(kite_websocket_price_feed)
outcome_learning_service = OutcomeLearningService()
opportunity_analytics_service = OpportunityAnalyticsService()
execution_analytics_service = ExecutionAnalyticsService()
professional_insights_service = ProfessionalInsightsService()
professional_readiness_service = ProfessionalReadinessService(
    backtest_service=backtest_service,
    opportunity_analytics_service=opportunity_analytics_service,
    execution_analytics_service=execution_analytics_service,
    option_history_repository=option_history_repository,
)
after_market_research_service = AfterMarketResearchService(
    backtest_service=backtest_service,
    professional_insights_service=professional_insights_service,
    data_ingestion_service=None,
    outcome_learning_service=outcome_learning_service,
    rejected_outcome_service=None,
    opportunity_analytics_service=opportunity_analytics_service,
    execution_analytics_service=execution_analytics_service,
    professional_readiness_service=professional_readiness_service,
    time_bucket_edge_service=time_bucket_edge_service,
    strategy_edge_service=strategy_edge_service,
    market_session_service=market_session_service,
    job_repository=RuntimeJobRepository(),
)
armed_entry_tracker_service: ArmedEntryTrackerService | None = None
dashboard_broker_cache: dict[str, tuple[datetime, dict[str, object]]] = {}
scanner_response_cache: dict[str, dict[str, object]] = {}
scanner_response_cache_lock = threading.RLock()
kite_provider_lock = threading.RLock()
shared_kite_provider: KiteProvider | None = None
kite_token_bootstrap_status: dict[str, Any] = {
    "ready": False,
    "source": "startup_not_run",
}


def get_kite_provider() -> KiteProvider:
    global shared_kite_provider
    with kite_provider_lock:
        if shared_kite_provider is None:
            shared_kite_provider = KiteProvider()
        existing_token = load_access_token()
        if existing_token and existing_token != shared_kite_provider.access_token:
            shared_kite_provider.set_access_token(existing_token)
        return shared_kite_provider


market_data_coordinator = MarketDataCoordinator(get_kite_provider)
if shared_kite_feed is not None:
    shared_kite_feed.market_data_coordinator = market_data_coordinator
active_trade_price_feed.market_data_coordinator = market_data_coordinator
application_context = ApplicationContext(
    market_session_service=market_session_service,
    kite_websocket_price_feed=kite_websocket_price_feed,
    active_trade_price_feed=active_trade_price_feed,
    banknifty_option_prewarm_service=banknifty_option_prewarm_service,
    market_data_runtime_service=market_data_runtime_service,
    kite_provider_factory=get_kite_provider,
)
rejected_opportunity_outcome_service = RejectedOpportunityOutcomeService(
    rejected_opportunity_repository,
    kite_provider_factory=get_kite_provider,
    market_data_coordinator=market_data_coordinator,
)
after_market_research_service.rejected_outcome_service = (
    rejected_opportunity_outcome_service
)


data_ingestion_service = DataIngestionService(
    kite_provider_factory=get_kite_provider,
    market_data_service=market_data_service,
    option_history_repository=option_history_repository,
    greeks_service=greeks_service,
    market_data_coordinator=market_data_coordinator,
)
after_market_research_service.data_ingestion_service = data_ingestion_service
option_premium_confirmation_service.live_gap_backfill_service = data_ingestion_service
option_snapshot_collector_service = OptionSnapshotCollectorService(
    data_ingestion_service
)


def get_scanner_service() -> ScannerService:
    return ScannerService(
        strategy_edge_service=strategy_edge_service,
        feed=shared_kite_feed,
        trade_setup_service=shared_trade_setup_service,
        option_premium_confirmation_service=OptionPremiumConfirmationService(
            kite_websocket_price_feed, live_gap_backfill_service=data_ingestion_service
        ),
        rejected_opportunity_repository=rejected_opportunity_repository,
        banknifty_intelligence_service=banknifty_intelligence_service,
        banknifty_option_prewarm_service=banknifty_option_prewarm_service,
        armed_entry_tracker=armed_entry_tracker_service,
        time_bucket_edge_service=time_bucket_edge_service,
        volatility_edge_service=volatility_edge_service,
        outcome_learning_service=outcome_learning_service,
        fast_scan_context_service=fast_scan_context_service,
        session_eligibility_provider=market_session_service.should_run_live_modules,
        decision_evidence_repository=decision_evidence_repository,
        outcome_collector=episode_outcome_collector,
    )


def get_order_service() -> OrderService:
    pre_order_risk_service = PreOrderRiskService(
        risk_management_service=risk_management_service,
        evidence_repository=decision_evidence_repository,
        evidence_queue=evidence_persistence_queue,
        outcome_collector=episode_outcome_collector,
        latency_metrics=latency_metrics_service,
    )
    return OrderService(
        kite_provider=get_kite_provider(),
        paper_trading_service=paper_trading_service,
        trade_repository=trade_repository,
        risk_management_service=risk_management_service,
        active_price_feed=active_trade_price_feed,
        live_safety_checker=_combined_live_safety_status,
        market_data_coordinator=market_data_coordinator,
        latency_metrics=latency_metrics_service,
        pre_order_risk_service=pre_order_risk_service,
    )


def _combined_live_safety_status() -> dict[str, Any]:
    broker = broker_sync_service.live_block_status()
    if broker.get("blocked"):
        return {**broker, "source": "broker_reconciliation"}
    strategy = strategy_version_registry.current_version()
    version = strategy.get("version", {}) if isinstance(strategy, dict) else {}
    if bool(version.get("config_drift_detected")):
        return {
            "blocked": True,
            "reason": "strategy_config_drift_requires_new_strategy_version",
            "source": "strategy_lineage",
            "strategy": version,
        }
    return {
        "blocked": False,
        "source": "combined",
        "broker": broker,
        "strategy": version,
    }


armed_entry_tracker_service = ArmedEntryTrackerService(
    order_service_factory=get_order_service,
    websocket_price_feed=kite_websocket_price_feed,
    rejected_opportunity_repository=rejected_opportunity_repository,
    risk_management_service=risk_management_service,
    market_session_provider=kite_websocket_price_feed.market_session,
    latency_metrics=latency_metrics_service,
    armed_entry_repository=ArmedEntryRepository(),
)
evidence_matrix_service = EvidenceMatrixService()
strategy_promotion_service = StrategyPromotionService(evidence=evidence_matrix_service)
kite_websocket_price_feed.tick_handler = armed_entry_tracker_service.on_tick
kite_websocket_price_feed.gap_handler = armed_entry_tracker_service.cancel_for_data_gap


auto_trader_service = AutoTraderService(
    scanner_factory=get_scanner_service,
    order_service_factory=get_order_service,
    opportunity_repository=opportunity_repository,
    risk_management_service=risk_management_service,
    notification_service=notification_service,
    latency_metrics=latency_metrics_service,
    fast_scan_context_service=fast_scan_context_service,
    fast_candidate_promoter=armed_entry_tracker_service,
    decision_evidence_repository=decision_evidence_repository,
)
banknifty_fast_rally_service = BankNiftyFastRallyService(
    auto_trader_service.request_fast_rescan, latency_metrics=latency_metrics_service
)


def _dispatch_strategy_tick(tick: object) -> None:
    episode_outcome_collector.on_tick(tick)
    if armed_entry_tracker_service is not None:
        armed_entry_tracker_service.on_tick(tick)
    banknifty_fast_rally_service.on_tick(tick)


kite_websocket_price_feed.tick_handler = _dispatch_strategy_tick
trade_exit_service = TradeExitService(
    trade_repository=trade_repository,
    kite_provider_factory=get_kite_provider,
    paper_trading_service=paper_trading_service,
    active_price_feed=active_trade_price_feed,
    notification_service=notification_service,
    market_data_coordinator=market_data_coordinator,
    latency_metrics=latency_metrics_service,
)
opportunity_outcome_service = OpportunityOutcomeService(
    repository=opportunity_repository,
    kite_provider_factory=get_kite_provider,
    trade_exit_service=trade_exit_service,
    rejected_outcome_service=rejected_opportunity_outcome_service,
    market_data_coordinator=market_data_coordinator,
    market_session_provider=kite_websocket_price_feed.market_session,
)
broker_sync_service = BrokerSyncService(
    trade_repository=trade_repository,
    kite_provider_factory=get_kite_provider,
    notification_service=notification_service,
    latency_metrics=latency_metrics_service,
)
broker_sync_service.set_exit_confirmation_callback(
    trade_exit_service.confirm_live_exit_for_trade
)
kite_websocket_price_feed.order_update_handler = (
    broker_sync_service.handle_order_postback
)
automation_supervisor_service = AutomationSupervisorService(
    data_ingestion_service=data_ingestion_service,
    snapshot_collector_service=option_snapshot_collector_service,
    auto_trader_service=auto_trader_service,
    outcome_service=opportunity_outcome_service,
    risk_management_service=risk_management_service,
    notification_service=notification_service,
    after_market_research_service=after_market_research_service,
    market_session_service=market_session_service,
)
application_shutdown_status: dict[str, object] = {
    "started": False,
    "completed": False,
    "error_type": None,
}
startup_maintenance_status: dict[str, object] = {
    "running": False,
    "completed": False,
    "errors": [],
}
startup_broker_sync_status: dict[str, object] = {
    "running": False,
    "completed": False,
    "errors": [],
}


@app.on_event("startup")
async def startup_automation() -> None:
    await asyncio.to_thread(runtime_instance_lock.assert_canonical_port_free)
    runtime_instance_lock.acquire()
    try:
        await _bootstrap_kite_access_token()
        evidence_persistence_queue.start()
        evidence_persistence_queue.recover_pending_order_evidence()
        episode_outcome_collector.start()
        raw_tick_capture_service.start()
        underlying_candle_service.start()
        startup_maintenance_status.update(
            {"running": True, "completed": False, "errors": []}
        )
        startup_broker_sync_status.update(
            {"running": True, "completed": False, "errors": []}
        )
        threading.Thread(
            target=_run_startup_maintenance, name="startup-maintenance", daemon=True
        ).start()
        threading.Thread(
            target=_run_startup_broker_sync, name="startup-broker-sync", daemon=True
        ).start()
        if settings.automation_enabled:
            automation_supervisor_service.start(trigger="application_startup")
    except BaseException:
        runtime_instance_lock.release()
        raise


async def _bootstrap_kite_access_token() -> None:
    global kite_token_bootstrap_status
    result = await asyncio.to_thread(ensure_access_token_for_today)
    kite_token_bootstrap_status = dict(result)
    if not result.get("ready"):
        kite_auth_state.mark_auth_failed("startup_token_bootstrap_failed")
        logger.warning(
            "Kite token bootstrap unavailable source=%s missing=%s error_type=%s",
            result.get("source"),
            result.get("missing_configuration"),
            result.get("error_type"),
        )
        return
    token = load_access_token()
    if not token:
        kite_token_bootstrap_status = {
            **kite_token_bootstrap_status,
            "ready": False,
            "source": "token_file_missing_after_bootstrap",
        }
        logger.error("Kite token bootstrap reported ready without a current token file")
        return
    get_kite_provider().set_access_token(token)
    kite_auth_state.clear()
    if shared_kite_feed is not None:
        shared_kite_feed.refresh_credentials(token)
    application_context.market_data_runtime_service.refresh_credentials(
        access_token=token, restart_if_enabled=False
    )
    logger.info("Kite token bootstrap ready source=%s", result.get("source"))


def _run_startup_maintenance() -> None:
    errors: list[str] = []
    try:
        strategy_version_registry.ensure_current_version()
    except Exception as exc:
        errors.append(f"strategy_version:{type(exc).__name__}")
        logger.exception("startup strategy version registration failed")
    try:
        recovery = armed_entry_tracker_service.recover_active()
        logger.info("startup armed entry recovery %s", recovery)
    except Exception as exc:
        errors.append(f"armed_entry_recovery:{type(exc).__name__}")
        logger.exception("startup armed entry recovery failed")
    try:
        kite_websocket_price_feed.cleanup_old_persisted_candles()
    except Exception as exc:
        errors.append(f"websocket_candle_cleanup:{type(exc).__name__}")
        logger.exception("startup websocket candle cleanup failed")
    try:
        raw_tick_capture_service.cleanup_retention()
    except Exception as exc:
        errors.append(f"raw_tick_cleanup:{type(exc).__name__}")
        logger.exception("startup raw tick retention cleanup failed")
    if settings.enable_kite_websocket:
        try:
            application_context.market_data_runtime_service.start()
            instruments = get_kite_provider().instruments("NSE")
            banknifty = next(
                (
                    item
                    for item in instruments
                    if str(item.get("name") or "").upper() == "NIFTY BANK"
                    or str(item.get("tradingsymbol") or "").upper() == "NIFTY BANK"
                ),
                None,
            )
            underlying_token = (
                int(banknifty.get("instrument_token"))
                if banknifty and banknifty.get("instrument_token")
                else None
            )
            if underlying_token:
                banknifty_fast_rally_service.set_underlying_token(underlying_token)
                underlying_candle_service.set_underlying_token(underlying_token)
                kite_websocket_price_feed.register_token_symbol(
                    underlying_token, "BANKNIFTY"
                )
                kite_websocket_price_feed.subscribe(
                    {underlying_token}, owner="core_market", mode="quote"
                )
        except Exception as exc:
            errors.append(f"websocket_start:{type(exc).__name__}")
            logger.exception("startup websocket start failed")
    try:
        if _scanner_market_is_open():
            _start_scanner_refresh(
                cache_key=_scanner_cache_key(
                    side=settings.automation_side,
                    symbols=settings.automation_symbols,
                    limit=settings.automation_scan_limit,
                    order_mode=settings.default_order_mode,
                ),
                side=settings.automation_side,
                symbols=settings.automation_symbols,
                limit=settings.automation_scan_limit,
                order_mode=settings.default_order_mode,
            )
    except Exception as exc:
        errors.append(f"scanner_warmup:{type(exc).__name__}")
        logger.exception("startup scanner warmup failed")
    startup_maintenance_status.update(
        {"running": False, "completed": not errors, "errors": errors}
    )


def _run_startup_broker_sync() -> None:
    if kite_auth_state.relogin_required:
        startup_broker_sync_status.update(
            {
                "running": False,
                "completed": False,
                "errors": ["kite_relogin_required"],
            }
        )
        logger.warning("startup broker sync skipped: Kite relogin required")
        return
    try:
        broker_sync_service.sync_open_trades(limit=100)
        broker_sync_service.reconcile_startup_positions()
    except Exception as exc:
        startup_broker_sync_status.update(
            {
                "running": False,
                "completed": False,
                "errors": [f"broker_sync:{type(exc).__name__}"],
            }
        )
        logger.exception("startup broker sync failed")
    else:
        startup_broker_sync_status.update(
            {"running": False, "completed": True, "errors": []}
        )


@app.on_event("shutdown")
async def shutdown_background_services() -> None:
    application_shutdown_status.update(
        {"started": True, "completed": False, "error_type": None}
    )
    try:
        application_context.market_data_runtime_service.stop()
        underlying_candle_service.stop()
        raw_tick_capture_service.stop()
        await automation_supervisor_service.stop(reason="application_shutdown")
        await option_snapshot_collector_service.stop()
        await auto_trader_service.stop()
        await opportunity_outcome_service.stop()
        evidence_persistence_queue.flush(timeout=5.0)
        evidence_persistence_queue.stop(timeout=2.0)
        episode_outcome_collector.flush(timeout=5.0)
        episode_outcome_collector.stop(timeout=3.0)
    except BaseException as exc:
        application_shutdown_status["error_type"] = type(exc).__name__
        raise
    else:
        application_shutdown_status["completed"] = True
    finally:
        runtime_instance_lock.release()


@app.get("/health", tags=["01 System"], summary="Check API health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get(
    "/market-data/pipeline-status",
    tags=["09 Market Data"],
    summary="Inspect canonical candles, raw ticks, queues, and latency",
)
def market_data_pipeline_status() -> dict[str, object]:
    return {
        "canonical_underlying_candles": underlying_candle_service.status(),
        "raw_tick_capture": raw_tick_capture_service.status(),
        "latency": latency_metrics_service.report(),
        "io_call_budgets": io_call_metrics.report(),
        "fast_scan_context": fast_scan_context_service.status(),
        "websocket": kite_websocket_price_feed.status(),
    }


@app.get(
    "/market-data/banknifty-constituents/status",
    tags=["09 Market Data"],
    summary="Inspect the reviewed Bank Nifty constituent-weight snapshot",
)
def banknifty_constituent_status() -> dict[str, object]:
    return banknifty_intelligence_service.snapshot_status()


@app.get(
    "/runtime/latency",
    tags=["01 System"],
    summary="Read end-to-end trading-path latency percentiles",
)
def runtime_latency() -> dict[str, object]:
    return {
        **latency_metrics_service.report(),
        "io_call_budgets": io_call_metrics.report(),
    }


@app.get(
    "/risk/policy/status",
    tags=["01 System"],
    summary="Inspect active and shadow risk-tier policy safety",
)
def risk_policy_status() -> dict[str, object]:
    policy = RiskPolicyService()
    conflicts = policy.configuration_conflicts()
    return {
        "risk_policy_version": settings.risk_policy_version,
        "active_entry_policy": settings.active_entry_policy,
        "shadow_entry_policy": settings.shadow_entry_policy,
        "active_risk_policy": settings.active_risk_policy,
        "shadow_risk_policy": settings.shadow_risk_policy,
        "tiers_percent": {
            "TIER_1_BASE": settings.risk_tier_1_base_pct,
            "TIER_2_STRONG": settings.risk_tier_2_strong_pct,
            "TIER_3_HIGH": settings.risk_tier_3_high_pct,
            "TIER_4_EXCEPTIONAL": settings.risk_tier_4_exceptional_pct,
        },
        "absolute_process_ceiling_percent": policy.HARD_ABSOLUTE_MAX_PERCENT,
        "configured_absolute_ceiling_percent": settings.absolute_max_risk_per_trade_percent,
        "active_paper_max_tier": settings.active_paper_max_risk_tier,
        "active_live_max_tier": settings.active_live_max_risk_tier,
        "shadow_max_tier": settings.shadow_max_risk_tier,
        "validated_higher_risk_active": settings.enable_validated_higher_risk_active,
        "exceptional_live_risk_active": settings.enable_exceptional_live_risk,
        "configuration_conflicts": conflicts,
        "configuration_valid": not any(
            not item.startswith("POLICY_CONFLICT_") for item in conflicts
        ),
    }


@app.get(
    "/risk/equity/snapshot",
    tags=["01 System"],
    summary="Create an audited executable-bid account-equity snapshot",
)
def audited_account_equity_snapshot() -> dict[str, object]:
    return risk_management_service.account_equity_state_service.snapshot(
        persist=True
    ).to_dict()


@app.get("/db/health", tags=["01 System"], summary="Check database connectivity")
def db_health() -> dict[str, object]:
    database_url = settings.database_url
    masked_url = database_url
    if "://" in database_url and "@" in database_url:
        scheme, rest = database_url.split("://", 1)
        host_part = rest.split("@", 1)[1]
        masked_url = f"{scheme}://<user>:<password>@{host_part}"
    try:
        session = get_session()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
        return {"status": "ok", "database_url": masked_url}
    except Exception as exc:
        return {
            "status": "error",
            "database_url": masked_url,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


@app.get(
    "/runtime/status",
    tags=["01 System"],
    summary="Inspect live runtime scheduler and API load controls",
)
async def runtime_status() -> dict[str, object]:
    now = ist_now_naive()
    with scanner_response_cache_lock:
        scanner_entries = len(scanner_response_cache)
        scanner_refreshing = sum(
            1 for entry in scanner_response_cache.values() if entry.get("refreshing")
        )
        scanner_errors = [
            {"key": key, "error": entry.get("last_error")}
            for key, entry in scanner_response_cache.items()
            if entry.get("last_error")
        ][-5:]
    ws_status = application_context.market_data_runtime_service.control_status()
    return {
        "status": "ok",
        "timestamp": now.isoformat(sep=" "),
        "session": market_session_service.status(now),
        "api": {
            "slow_log_threshold_seconds": settings.slow_api_log_threshold_seconds,
            "dashboard_refresh_seconds": 15,
            "dashboard_broker_cache_ttl_seconds": settings.dashboard_broker_cache_ttl_seconds,
            "scanner_response_cache_ttl_seconds": settings.scanner_response_cache_ttl_seconds,
            "scanner_response_stale_ttl_seconds": settings.scanner_response_stale_ttl_seconds,
        },
        "kite": {
            "access_token_available": bool(load_access_token()),
            "token_file": token_status(),
            "automatic_login": dict(kite_token_bootstrap_status),
            "shared_provider_created": shared_kite_provider is not None,
            "api_timeout_seconds": settings.kite_api_timeout_seconds,
        },
        "websocket": {
            "state": ws_status.get("state"),
            "reason": ws_status.get("reason"),
            "status": ws_status.get("websocket_status"),
            "running": ws_status.get("running"),
            "connected": ws_status.get("connected"),
            "market_session": ws_status.get("market_session"),
            "duplicate_start_prevented_count": ws_status.get(
                "duplicate_start_prevented_count"
            ),
            "reconnect_count": ws_status.get("reconnect_count"),
            "disconnect_count": ws_status.get("disconnect_count"),
            "last_error": ws_status.get("last_error"),
        },
        "scanner": {
            "auto_trader_running": auto_trader_service.running,
            "last_scan_at": auto_trader_service.last_scan_at,
            "error_count": len(auto_trader_service.errors),
            "response_cache_entries": scanner_entries,
            "refreshing_entries": scanner_refreshing,
            "recent_cache_errors": scanner_errors,
        },
        "runtime_jobs": {
            "automation_supervisor_running": automation_supervisor_service.running,
            "automation_duplicate_start_prevented_count": automation_supervisor_service.duplicate_start_prevented_count,
            "snapshot_collector_running": option_snapshot_collector_service.running,
            "outcome_monitor_running": opportunity_outcome_service.running,
            "after_market_review_running": after_market_research_service.running,
            "after_market_review_last_run_date": after_market_research_service.last_run_date,
            "after_market_review_last_error": after_market_research_service.last_error,
        },
        "instance_lock": runtime_instance_lock.status(),
        "startup_tasks": {
            "maintenance": dict(startup_maintenance_status),
            "broker_sync": dict(startup_broker_sync_status),
        },
        "market_data_cache": market_data_coordinator.status(),
    }


@app.get(
    "/market-data/cache/status",
    tags=["09 Market Data"],
    summary="Inspect shared market-data quote cache",
)
def market_data_cache_status() -> dict[str, object]:
    return market_data_coordinator.status()


@app.get(
    "/strategy/versions/current",
    tags=["10 Research"],
    summary="Inspect the active strategy version registry entry",
)
def current_strategy_version() -> dict[str, object]:
    return strategy_version_registry.current_version()


@app.get(
    "/strategy/versions",
    tags=["10 Research"],
    summary="List strategy version registry entries",
)
def list_strategy_versions(limit: int = 50) -> dict[str, object]:
    return strategy_version_registry.list_versions(limit=limit)


@app.get(
    "/strategy/versions/{version}",
    tags=["10 Research"],
    summary="Inspect one strategy version registry entry",
)
def get_strategy_version(version: str) -> dict[str, object]:
    result = strategy_version_registry.get_version(version)
    if result.get("status") == "not_found":
        raise HTTPException(
            status_code=404, detail=f"strategy version not found: {version}"
        )
    return result


@app.get(
    "/research/evidence-matrix",
    tags=["10 Research"],
    summary="Inspect setup/regime outcome evidence",
)
def get_evidence_matrix(
    group_by: str | None = None, limit: int = 5000
) -> dict[str, object]:
    dimensions = [
        item.strip() for item in str(group_by or "").split(",") if item.strip()
    ] or None
    return evidence_matrix_service.report(
        group_by=dimensions, limit=max(1, min(int(limit), 10000))
    )


@app.get(
    "/research/strategy-promotion",
    tags=["10 Research"],
    summary="Evaluate guarded strategy promotion readiness",
)
def get_strategy_promotion(timeframe: str = "5minute") -> dict[str, object]:
    return strategy_promotion_service.evaluate(timeframe=timeframe)


@app.get(
    "/research/shadow-validation/status",
    tags=["10 Research"],
    summary="Inspect asynchronous research collection health",
)
def shadow_validation_status() -> dict[str, object]:
    return {
        "active_policy_changed": False,
        "shadow_order_routing_available": False,
        "evidence_queue": evidence_persistence_queue.status(),
        "outcome_collector": episode_outcome_collector.status(),
    }


@app.get(
    "/research/dataset-audit",
    tags=["10 Research"],
    summary="Audit replay and executable-quote dataset completeness",
)
def research_dataset_audit() -> dict[str, object]:
    return research_dataset_audit_service.audit()


@app.get(
    "/research/shadow-policy-report",
    tags=["10 Research"],
    summary="Compare shadow policies by chronological unique episodes",
)
def shadow_policy_report(maximum_horizon_seconds: int = 900) -> dict[str, object]:
    horizon = max(30, min(int(maximum_horizon_seconds), 86400))
    return shadow_policy_research_report_service.report(maximum_horizon_seconds=horizon)


@app.post(
    "/strategy/versions/register",
    tags=["10 Research"],
    summary="Register or refresh the current strategy version with human notes",
)
def register_strategy_version(
    payload: dict[str, object] | None = Body(default=None),
) -> dict[str, object]:
    payload = payload or {}
    base = strategy_version_registry.current_payload(
        human_note=str(payload.get("human_note"))
        if payload.get("human_note")
        else None,
        reason_for_change=str(payload.get("reason_for_change"))
        if payload.get("reason_for_change")
        else None,
    )
    for key in (
        "strategy_name",
        "version",
        "status",
        "human_note",
        "reason_for_change",
        "entry_logic_summary",
        "exit_logic_summary",
        "stoploss_logic_summary",
        "target_logic_summary",
    ):
        if payload.get(key):
            base[key] = payload[key]
    if isinstance(payload.get("config_snapshot"), dict):
        base["config_snapshot"] = payload["config_snapshot"]
    if isinstance(payload.get("settings_purpose"), dict):
        base["settings_purpose"] = payload["settings_purpose"]
    base["_force_text_update"] = True
    return strategy_version_registry.register(base)


@app.get("/", tags=["01 System"], summary="Show API workflow overview")
def root() -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "docs": "/docs",
        "workflow": [
            "GET /health",
            "GET /db/health",
            "GET /kite/auth",
            "GET /kite/health",
            "GET /kite/margins",
            "POST /automation/start",
            "GET /automation/status",
            "GET /strategy/versions/current",
            "POST /data/ingest/candles",
            "POST /data/ingest/option-candles",
            "POST /data/ingest/option-snapshots",
            "POST /data/collector/start",
            "GET /scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3",
            "GET /scanner/diagnostics?side=BUY&symbols=BANKNIFTY&limit=10",
            "POST /research/greeks",
            "POST /research/option-quality",
            "POST /research/backtest",
            "POST /orders/place with confirm_live=false for paper test",
            "POST /auto-trader/start with monitor_outcomes=true",
            "GET /opportunities",
            "GET /opportunities/performance",
            "GET /opportunities/failure-analysis",
        ],
        "live_order_guard": "Requires LIVE_TRADING_MODE=true, PAPER_TRADING_MODE=false, and confirm_live=true.",
    }


@app.get(
    "/dashboard",
    response_class=HTMLResponse,
    tags=["01 System"],
    summary="Open Bank Nifty operator dashboard",
)
async def command_center_dashboard() -> HTMLResponse:
    return HTMLResponse(render_command_center_dashboard())


def _decision_event_from_opportunity(record) -> dict[str, object]:
    factors = _json_dict(getattr(record, "factor_scores_json", None))
    timing = (
        factors.get("entry_timing", {})
        if isinstance(factors.get("entry_timing"), dict)
        else {}
    )
    state = str(timing.get("entry_timing_state") or "ACCEPTED")
    reason = str(timing.get("entry_timing_reason") or "Accepted opportunity saved")
    market = (
        factors.get("market_regime", {})
        if isinstance(factors.get("market_regime"), dict)
        else {}
    )
    momentum = (
        factors.get("momentum_phase", {})
        if isinstance(factors.get("momentum_phase"), dict)
        else {}
    )
    family = (
        factors.get("setup_family", {})
        if isinstance(factors.get("setup_family"), dict)
        else {}
    )
    return {
        "time": format_ist(record.created_at),
        "sort_time": record.created_at.isoformat() if record.created_at else "",
        "type": "accepted_opportunity",
        "severity": "ok",
        "title": f"{state}: {record.action}",
        "message": reason,
        "symbol": record.symbol,
        "tradingsymbol": record.tradingsymbol,
        "score": record.score,
        "entry_price": record.entry_price,
        "stop_loss": record.stop_loss,
        "target_1": record.target_1,
        "status": record.status,
        "outcome": record.outcome,
        "state": state,
        "market_regime": market.get("regime"),
        "momentum_phase": momentum.get("phase"),
        "setup_family": family.get("name"),
    }


def _decision_event_from_rejection(record) -> dict[str, object]:
    factors = _json_dict(getattr(record, "factor_scores_json", None))
    timing = (
        factors.get("entry_timing", {})
        if isinstance(factors.get("entry_timing"), dict)
        else {}
    )
    reasons = _json_list(getattr(record, "reasons_json", None))
    state = str(timing.get("entry_timing_state") or "REJECTED")
    reason = str(
        timing.get("entry_timing_reason")
        or "; ".join(reasons[:3])
        or record.primary_gate
        or "Rejected setup"
    )
    market = (
        factors.get("market_regime", {})
        if isinstance(factors.get("market_regime"), dict)
        else {}
    )
    momentum = (
        factors.get("momentum_phase", {})
        if isinstance(factors.get("momentum_phase"), dict)
        else {}
    )
    family = (
        factors.get("setup_family", {})
        if isinstance(factors.get("setup_family"), dict)
        else {}
    )
    severity = "warn"
    if any(
        item in reasons
        for item in [
            "entry_too_late",
            "chase_risk_high",
            "selected_option_quote_invalid",
        ]
    ):
        severity = "bad"
    elif any(
        item in reasons
        for item in ["waiting_for_entry_trigger", "premium_trigger_not_broken_yet"]
    ):
        severity = "watch"
    return {
        "time": format_ist(record.created_at),
        "sort_time": record.created_at.isoformat() if record.created_at else "",
        "type": "rejected_opportunity",
        "severity": severity,
        "title": f"{state}: {record.action or record.side}",
        "message": reason,
        "symbol": record.symbol,
        "tradingsymbol": record.tradingsymbol,
        "score": record.score,
        "primary_gate": record.primary_gate,
        "reasons": reasons,
        "later_outcome": record.later_outcome,
        "state": state,
        "market_regime": market.get("regime"),
        "momentum_phase": momentum.get("phase"),
        "setup_family": family.get("name"),
        "abstention_code": market.get("abstention_code")
        or momentum.get("abstention_code")
        or family.get("abstention_code"),
    }


def _decision_event_from_trade(record) -> dict[str, object]:
    status = str(record.status or "unknown")
    outcome = str(record.outcome or "")
    if status == "closed":
        title = f"Trade closed: {outcome or 'exit'}"
        severity = "ok" if (record.net_pnl or record.pnl or 0) >= 0 else "bad"
        message = f"Exit {record.exit_price}; net P&L {record.net_pnl if record.net_pnl is not None else record.pnl}"
    elif status in {"closing", "exit_failed", "reconciliation_mismatch"}:
        title = f"Exit attention: {status}"
        severity = "bad"
        message = str(
            record.exit_last_error
            or record.exit_order_status
            or "Exit needs monitoring"
        )
    else:
        title = f"Trade open: {record.action}"
        severity = "watch"
        message = f"Entry {record.entry_price}; SL {record.stop_loss}; Target 1 {record.target_1}"
    return {
        "time": format_ist(record.updated_at or record.created_at),
        "sort_time": (record.updated_at or record.created_at).isoformat()
        if (record.updated_at or record.created_at)
        else "",
        "type": "trade",
        "severity": severity,
        "title": title,
        "message": message,
        "symbol": record.symbol,
        "tradingsymbol": record.tradingsymbol,
        "mode": record.mode,
        "strategy_version": getattr(record, "strategy_version", None),
        "config_hash": getattr(record, "config_hash", None),
        "status": record.status,
        "outcome": record.outcome,
        "entry_price": record.entry_price,
        "stop_loss": record.stop_loss,
        "target_1": record.target_1,
        "net_pnl": record.net_pnl,
    }


def _decision_event_from_execution(item: dict[str, object]) -> dict[str, object]:
    signal = item.get("signal", {}) if isinstance(item.get("signal"), dict) else {}
    result = item.get("result", {}) if isinstance(item.get("result"), dict) else {}
    return {
        "time": item.get("time"),
        "sort_time": str(item.get("time") or ""),
        "type": "auto_execution",
        "severity": "ok",
        "title": f"Order action: {result.get('status') or 'submitted'}",
        "message": f"{signal.get('action') or ''} {signal.get('tradingsymbol') or signal.get('symbol') or ''}".strip(),
        "tradingsymbol": signal.get("tradingsymbol"),
        "score": signal.get("score"),
        "order_key": item.get("order_key"),
    }


def _decision_event_from_auto_decision(item: dict[str, object]) -> dict[str, object]:
    event_type = str(item.get("event_type") or "fast_rally_candidate_validation")
    passed = bool(item.get("passed"))
    direction = str(item.get("direction") or "").lower()
    reason = str(
        item.get("reason") or ("candidate_passed" if passed else "candidate_rejected")
    )
    move_pct = item.get("move_pct")
    move_text = (
        f"; Bank Nifty move {float(move_pct):.4f}%"
        if isinstance(move_pct, (int, float))
        else ""
    )
    if event_type == "fast_rally_dispatch":
        scheduled = bool(item.get("scheduled"))
        stage = str(item.get("stage") or ("queued" if scheduled else "suppressed"))
        return {
            "time": item.get("time"),
            "sort_time": item.get("time"),
            "type": "fast_rally_dispatch",
            "severity": "watch" if scheduled else "warn",
            "title": "Fast-rally validation queued"
            if scheduled
            else "Fast-rally validation suppressed",
            "message": f"{stage}: {reason}{move_text}",
            "source": item.get("source") or "banknifty_fast_rally",
            "direction": direction or None,
            "reason": reason,
            "scheduled": scheduled,
        }
    return {
        "time": item.get("time"),
        "sort_time": item.get("time"),
        "type": "candidate_validation",
        "severity": "ok" if passed else ("bad" if item.get("error") else "warn"),
        "title": f"Fast-rally candidate {'validated' if passed else 'rejected'}",
        "message": f"{reason}{move_text}"
        + (f"; {item.get('error')}" if item.get("error") else ""),
        "source": item.get("source") or "fast_rally_candidate_validation",
        "direction": direction or None,
        "reason": reason,
        "error_type": item.get("error_type"),
    }


def _decision_event_from_scan_heartbeat(item: dict[str, object]) -> dict[str, object]:
    count = int(item.get("opportunity_count") or 0)
    placed = int(item.get("placed_count") or 0)
    if count:
        noun = "opportunity" if count == 1 else "opportunities"
        message = f"Scheduled scan completed with {count} actionable {noun}."
        if placed:
            message += f" {placed} order action{'s' if placed != 1 else ''} queued."
    else:
        message = "Scheduled scan completed; no actionable opportunity passed all primary gates."
    return {
        "time": item.get("time"),
        "sort_time": item.get("time"),
        "type": "scan_heartbeat",
        "severity": "ok" if count else "watch",
        "title": "Scheduled scan completed",
        "message": message,
        "source": item.get("source") or "scheduled_scan",
        "opportunity_count": count,
        "placed_count": placed,
    }


def _decision_event_from_error(item: dict[str, object]) -> dict[str, object]:
    source = str(item.get("source") or "automation")
    error_type = str(item.get("error_type") or "")
    raw_error = str(item.get("error") or "Unknown automation failure")
    if error_type == "KeyError" or (
        len(raw_error) >= 3
        and raw_error[0] == raw_error[-1]
        and raw_error[0] in {"'", '"'}
    ):
        field = raw_error.strip("'\"") or "unknown"
        message = f"The {source.replace('_', ' ')} could not complete because required field '{field}' was missing."
    elif raw_error == "scheduled_scan_rest_call_budget_exceeded":
        message = (
            f"The scheduled scan used {item.get('rest_calls')} broker calls, above its allowed budget of "
            f"{item.get('budget')}; candidates were suppressed safely."
        )
    else:
        message = raw_error
    return {
        "time": item.get("time"),
        "sort_time": str(item.get("time") or ""),
        "type": "auto_error",
        "severity": "bad",
        "title": f"Automation error: {source.replace('_', ' ')}",
        "message": message,
        "source": source,
        "error_type": error_type or None,
        "raw_error": raw_error,
        "order_key": item.get("order_key"),
    }


def _decision_sort_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return datetime.min
    iso_text = text[:-4] if text.endswith(" IST") else text
    try:
        parsed = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%d %b %Y, %I:%M:%S %p IST")
    except ValueError:
        return datetime.min


def _json_dict(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def opportunity_record_to_dict(record) -> dict[str, object]:
    try:
        failure_tags = json.loads(record.failure_tags_json or "[]")
    except json.JSONDecodeError:
        failure_tags = []
    return {
        "id": record.id,
        "created_at": format_ist(record.created_at),
        "symbol": record.symbol,
        "action": record.action,
        "side": record.side,
        "tradingsymbol": record.tradingsymbol,
        "exchange": record.exchange,
        "expiry": record.expiry,
        "strike": record.strike,
        "entry_price": record.entry_price,
        "stop_loss": record.stop_loss,
        "target_1": record.target_1,
        "target_2": record.target_2,
        "target_3": record.target_3,
        "quantity": record.quantity,
        "lot_size": record.lot_size,
        "score": record.score,
        "probability": record.probability,
        "heuristic_score_confidence": getattr(
            record, "heuristic_score_confidence", None
        ),
        "probability_source": getattr(record, "probability_source", None),
        "calibration_version": getattr(record, "calibration_version", None),
        "strategy_version": getattr(record, "strategy_version", None),
        "config_hash": getattr(record, "config_hash", None),
        "risk_reward": record.risk_reward,
        "status": record.status,
        "outcome": record.outcome,
        "exit_price": record.exit_price,
        "closed_at": format_ist(record.closed_at),
        "pnl": record.pnl,
        "failure_tags": failure_tags,
        "review_notes": record.review_notes,
    }


def trade_record_to_dict(record) -> dict[str, object]:
    return {
        "id": record.id,
        "created_at": format_ist(record.created_at),
        "updated_at": format_ist(record.updated_at),
        "opportunity_id": record.opportunity_id,
        "symbol": record.symbol,
        "tradingsymbol": record.tradingsymbol,
        "exchange": record.exchange,
        "instrument_token": getattr(record, "instrument_token", None),
        "action": record.action,
        "side": record.side,
        "mode": record.mode,
        "status": record.status,
        "broker_order_id": record.broker_order_id,
        "requested_quantity": record.requested_quantity,
        "placed_quantity": record.placed_quantity,
        "filled_quantity": record.filled_quantity,
        "entry_price": record.entry_price,
        "average_price": record.average_price,
        "stop_loss": record.stop_loss,
        "target_1": record.target_1,
        "target_2": record.target_2,
        "target_3": record.target_3,
        "exit_price": record.exit_price,
        "exit_rule_first_triggered": getattr(record, "exit_rule_first_triggered", None),
        "exit_triggered_rules": _json_list(
            getattr(record, "exit_triggered_rules_json", None)
        ),
        "exit_ltp": getattr(record, "exit_ltp", None),
        "exit_best_bid": getattr(record, "exit_best_bid", None),
        "exit_best_ask": getattr(record, "exit_best_ask", None),
        "exit_executable_price": getattr(record, "exit_executable_price", None),
        "exit_depth_coverage": getattr(record, "exit_depth_coverage", None),
        "exit_spread_pct": getattr(record, "exit_spread_pct", None),
        "exit_execution_source": getattr(record, "exit_execution_source", None),
        "exit_quote_timestamp": format_ist(
            getattr(record, "exit_quote_timestamp", None)
        ),
        "exit_order_id": getattr(record, "exit_order_id", None),
        "exit_order_status": getattr(record, "exit_order_status", None),
        "exit_attempt_count": getattr(record, "exit_attempt_count", 0),
        "exit_last_error": getattr(record, "exit_last_error", None),
        "exit_requested_at": format_ist(getattr(record, "exit_requested_at", None)),
        "exit_confirmed_at": format_ist(getattr(record, "exit_confirmed_at", None)),
        "protective_order_id": getattr(record, "protective_order_id", None),
        "protective_order_status": getattr(record, "protective_order_status", None),
        "protective_trigger_price": getattr(record, "protective_trigger_price", None),
        "protective_last_error": getattr(record, "protective_last_error", None),
        "protective_requested_at": format_ist(
            getattr(record, "protective_requested_at", None)
        ),
        "protective_cancelled_at": format_ist(
            getattr(record, "protective_cancelled_at", None)
        ),
        "price_source": getattr(record, "price_source", None),
        "price_timestamp": format_ist(getattr(record, "price_timestamp", None)),
        "price_age_seconds": getattr(record, "price_age_seconds", None),
        "highest_price_during_trade": getattr(
            record, "highest_price_during_trade", None
        ),
        "lowest_price_during_trade": getattr(record, "lowest_price_during_trade", None),
        "mfe_points": getattr(record, "mfe_points", None),
        "mfe_percent": getattr(record, "mfe_percent", None),
        "mae_points": getattr(record, "mae_points", None),
        "mae_percent": getattr(record, "mae_percent", None),
        "time_to_mfe": getattr(record, "time_to_mfe", None),
        "time_to_mae": getattr(record, "time_to_mae", None),
        "mfe_recorded_at": format_ist(getattr(record, "mfe_recorded_at", None)),
        "mae_recorded_at": format_ist(getattr(record, "mae_recorded_at", None)),
        "gross_pnl": getattr(record, "gross_pnl", None),
        "net_pnl": getattr(record, "net_pnl", None),
        "charges": getattr(record, "charges", None),
        "slippage_cost": getattr(record, "slippage_cost", None),
        "spread_cost": getattr(record, "spread_cost", None),
        "remaining_quantity": getattr(record, "remaining_quantity", None),
        "pnl": getattr(record, "net_pnl", None)
        if getattr(record, "net_pnl", None) is not None
        else record.pnl,
        "outcome": record.outcome,
        "notes": record.notes,
        "strategy_version": getattr(record, "strategy_version", None),
        "config_hash": getattr(record, "config_hash", None),
    }


def option_quote_snapshot_to_dict(record) -> dict[str, object]:
    return {
        "id": record.id,
        "underlying": record.underlying,
        "tradingsymbol": record.tradingsymbol,
        "exchange": record.exchange,
        "timestamp": format_ist(record.timestamp),
        "expiry": record.expiry,
        "strike": record.strike,
        "option_type": record.option_type,
        "last_price": record.last_price,
        "bid": record.bid,
        "ask": record.ask,
        "implied_volatility": record.implied_volatility,
        "delta": record.delta,
        "gamma": record.gamma,
        "theta": record.theta,
        "vega": record.vega,
        "open_interest": record.open_interest,
        "volume": record.volume,
    }


def parse_symbol_list(value: object, default: list[str] | None = None) -> list[str]:
    if isinstance(value, str):
        symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        symbols = [str(item).strip().upper() for item in value if str(item).strip()]
    else:
        symbols = default or ["BANKNIFTY"]
    return symbols or (default or ["BANKNIFTY"])


@app.get(
    "/market/{symbol}",
    tags=["09 Market Data"],
    summary="Get stored market summary for a symbol",
)
def get_market_summary(symbol: str) -> dict[str, object]:
    return market_data_service.get_market_summary(symbol)


@app.get(
    "/data/ingest/status",
    tags=["11 Data Ingestion"],
    summary="Show stored candle and option-history counts",
)
def get_ingestion_status(
    symbols: str | None = None, timeframe: str = "5minute"
) -> dict[str, object]:
    return data_ingestion_service.status(
        symbols=parse_symbol_list(symbols, default=[]) if symbols else None,
        timeframe=timeframe,
    )


@app.post(
    "/data/ingest/candles",
    tags=["11 Data Ingestion"],
    summary="Ingest historical underlying candles from Kite",
    description="Fetches Kite historical OHLCV candles for the given symbols and stores them in MySQL for backtesting.",
    dependencies=PROTECTED_ROUTE,
)
def ingest_historical_candles(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "symbols": "BANKNIFTY",
                "timeframe": "5minute",
                "days": 90,
                "use_checkpoint": True,
                "overlap_minutes": 30,
                "from": "2026-04-01",
                "to": "2026-06-30",
            }
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    _require_manual_override_for_market_heavy_operation(
        payload, "historical candle ingestion"
    )
    try:
        return data_ingestion_service.ingest_candles(
            symbols=parse_symbol_list(payload.get("symbols")),
            timeframe=str(payload.get("timeframe") or "5minute"),
            from_date=str(payload.get("from")) if payload.get("from") else None,
            to_date=str(payload.get("to")) if payload.get("to") else None,
            days=_bounded_int(payload.get("days"), 90, maximum=365),
            use_checkpoint=bool(payload.get("use_checkpoint", False)),
            overlap_minutes=int(
                payload.get("overlap_minutes")
                or settings.automation_checkpoint_overlap_minutes
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/data/ingest/banknifty-canonical-bootstrap",
    tags=["11 Data Ingestion"],
    summary="Safely bootstrap canonical Bank Nifty 1m/5m candles",
    dependencies=PROTECTED_ROUTE,
)
def bootstrap_banknifty_canonical(
    payload: dict[str, object] | None = Body(default=None),
) -> dict[str, object]:
    payload = payload or {}
    _require_manual_override_for_market_heavy_operation(
        payload, "Bank Nifty canonical candle bootstrap"
    )
    try:
        return data_ingestion_service.bootstrap_canonical_banknifty(
            from_date=str(payload.get("from")) if payload.get("from") else None,
            to_date=str(payload.get("to")) if payload.get("to") else None,
            days=_bounded_int(payload.get("days"), 90, maximum=365),
            use_checkpoint=bool(payload.get("use_checkpoint", True)),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/data/ingest/option-snapshots",
    tags=["11 Data Ingestion"],
    summary="Capture current option-chain quote snapshots",
    description=(
        "Stores current nearby option quotes, bid/ask, OI, volume, and estimated Greeks/IV. "
        "Run this repeatedly during market hours to build historical option-chain data going forward."
    ),
    dependencies=PROTECTED_ROUTE,
)
def ingest_option_snapshots(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "symbols": "BANKNIFTY",
                "strike_window_pct": 4.0,
                "max_contracts_per_symbol": 120,
            }
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    try:
        return data_ingestion_service.capture_option_snapshots(
            symbols=parse_symbol_list(payload.get("symbols")),
            strike_window_pct=float(payload.get("strike_window_pct") or 4.0),
            max_contracts_per_symbol=int(
                payload.get("max_contracts_per_symbol") or 120
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/data/collector/start",
    tags=["11 Data Ingestion"],
    summary="Start continuous option-chain snapshot collection",
    description="Continuously stores option quote, bid/ask, OI, IV, and Greeks snapshots. Use during market hours to build option-history data.",
    dependencies=PROTECTED_ROUTE,
)
async def start_option_snapshot_collector(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "symbols": "BANKNIFTY",
                "interval_seconds": 300,
                "strike_window_pct": 4.0,
                "max_contracts_per_symbol": 120,
            }
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    return option_snapshot_collector_service.start(
        symbols=parse_symbol_list(payload.get("symbols")),
        interval_seconds=int(payload.get("interval_seconds") or 300),
        strike_window_pct=float(payload.get("strike_window_pct") or 4.0),
        max_contracts_per_symbol=int(payload.get("max_contracts_per_symbol") or 120),
    )


@app.post(
    "/data/collector/stop",
    tags=["11 Data Ingestion"],
    summary="Stop continuous option-chain snapshot collection",
    dependencies=PROTECTED_ROUTE,
)
async def stop_option_snapshot_collector() -> dict[str, object]:
    return await option_snapshot_collector_service.stop()


@app.get(
    "/data/collector/status",
    tags=["11 Data Ingestion"],
    summary="Get option-chain snapshot collector status",
)
def get_option_snapshot_collector_status() -> dict[str, object]:
    return option_snapshot_collector_service.status()


@app.post(
    "/automation/start",
    tags=["12 Automation"],
    summary="Start one-switch automation supervisor",
    description=(
        "Automatically handles session candle ingestion, market-hour option snapshot collection, auto scanning, "
        "and outcome monitoring. Orders stay disabled unless `place_orders=true`; live orders still require live config and `confirm_live=true`."
    ),
    dependencies=PROTECTED_ROUTE,
)
async def start_automation(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "symbols": "BANKNIFTY",
                "side": "BUY",
                "scan_interval_seconds": 30,
                "snapshot_interval_seconds": 300,
                "outcome_interval_seconds": 30,
                "ingest_days": 7,
                "checkpoint_overlap_minutes": 30,
                "scan_limit": 3,
                "place_orders": False,
                "confirm_live": False,
            }
        ],
    ),
) -> dict[str, object]:
    merged_payload = runtime_trading_config_service.automation_payload(payload or {})
    return automation_supervisor_service.start(merged_payload, trigger="api_start")


@app.post(
    "/automation/stop",
    tags=["12 Automation"],
    summary="Stop automation supervisor and intraday collector/scanner",
    dependencies=PROTECTED_ROUTE,
)
async def stop_automation() -> dict[str, object]:
    return await automation_supervisor_service.stop(reason="api_stop")


@app.post(
    "/automation/run-once",
    tags=["12 Automation"],
    summary="Run one automation supervisor cycle now",
    description="Runs the same decision cycle the supervisor loop runs: bootstrap data if needed, start market-hour services, or evaluate open outcomes after hours.",
    dependencies=PROTECTED_ROUTE,
)
def run_automation_once(
    payload: dict[str, object] | None = Body(default=None),
) -> dict[str, object]:
    return automation_supervisor_service.run_once(payload or None)


@app.get(
    "/automation/status",
    tags=["12 Automation"],
    summary="Get automation supervisor status",
)
def get_automation_status() -> dict[str, object]:
    return automation_supervisor_service.status()


@app.get(
    "/runtime/trading-config",
    tags=["12 Automation"],
    summary="Inspect dashboard runtime trading mode config",
)
async def get_runtime_trading_config() -> dict[str, object]:
    return runtime_trading_config_service.status()


@app.get(
    "/runtime/trading-config/preview",
    tags=["12 Automation"],
    summary="Preview paper/live dashboard mode changes",
)
async def preview_runtime_trading_config(mode: str = "paper") -> dict[str, object]:
    return runtime_trading_config_service.preview(mode)


@app.post(
    "/runtime/trading-config/apply",
    tags=["12 Automation"],
    summary="Apply dashboard runtime trading mode config",
    dependencies=PROTECTED_ROUTE,
)
async def apply_runtime_trading_config(
    payload: dict[str, object] | None = Body(default=None),
) -> dict[str, object]:
    return runtime_trading_config_service.apply(payload or {})


@app.post(
    "/data/ingest/option-candles",
    tags=["11 Data Ingestion"],
    summary="Ingest historical candles for nearby option contracts",
    description=(
        "Fetches OHLCV candles for currently listed nearby option contracts. "
        "This stores option premium movement, while `/data/ingest/option-snapshots` stores bid/ask, OI, IV, and Greeks."
    ),
    dependencies=PROTECTED_ROUTE,
)
def ingest_option_candles(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "symbols": "BANKNIFTY",
                "timeframe": "5minute",
                "days": 30,
                "strike_window_pct": 2.0,
                "max_contracts_per_symbol": 20,
            }
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    _require_manual_override_for_market_heavy_operation(
        payload, "historical option candle ingestion"
    )
    try:
        return data_ingestion_service.ingest_option_candles(
            symbols=parse_symbol_list(payload.get("symbols")),
            timeframe=str(payload.get("timeframe") or "5minute"),
            from_date=str(payload.get("from")) if payload.get("from") else None,
            to_date=str(payload.get("to")) if payload.get("to") else None,
            days=_bounded_int(payload.get("days"), 30, maximum=90),
            strike_window_pct=float(payload.get("strike_window_pct") or 2.0),
            max_contracts_per_symbol=_bounded_int(
                payload.get("max_contracts_per_symbol"), 20, maximum=120
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/data/option-candle-coverage",
    tags=["11 Data Ingestion"],
    summary="Check candle coverage for app-relevant option contracts",
    description="Measures 1-minute/5-minute option candle completeness for contracts that were traded, rejected, or accepted today.",
    dependencies=PROTECTED_ROUTE,
)
def get_option_candle_coverage(
    symbols: str = "BANKNIFTY",
    trading_date: str | None = None,
    timeframes: str | None = None,
    max_contracts: int | None = None,
) -> dict[str, object]:
    timeframe_values = [
        item.strip()
        for item in str(
            timeframes or settings.targeted_option_candle_backfill_timeframes
        ).split(",")
        if item.strip()
    ]
    return data_ingestion_service.option_candle_coverage_report(
        symbols=parse_symbol_list(symbols),
        trading_date=trading_date,
        timeframes=timeframe_values,
        max_contracts=_bounded_int(
            max_contracts,
            settings.targeted_option_candle_backfill_max_contracts,
            maximum=500,
        ),
    )


@app.post(
    "/data/ingest/relevant-option-candles",
    tags=["11 Data Ingestion"],
    summary="Backfill candles for option contracts actually touched by the app",
    description="Targets traded, accepted, and rejected option contracts for the selected day, then reports candle coverage after ingestion.",
    dependencies=PROTECTED_ROUTE,
)
def ingest_relevant_option_candles(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "symbols": "BANKNIFTY",
                "trading_date": "2026-07-13",
                "timeframes": "1minute,5minute",
                "max_contracts": 120,
                "batch_limit": 25,
                "delay_seconds": 0.5,
                "manual_override": True,
            }
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    _require_manual_override_for_market_heavy_operation(
        payload, "targeted relevant option candle backfill"
    )
    timeframe_values = [
        item.strip()
        for item in str(
            payload.get("timeframes")
            or settings.targeted_option_candle_backfill_timeframes
        ).split(",")
        if item.strip()
    ]
    try:
        return data_ingestion_service.backfill_relevant_option_candles(
            symbols=parse_symbol_list(payload.get("symbols")),
            trading_date=str(payload.get("trading_date"))
            if payload.get("trading_date")
            else None,
            timeframes=timeframe_values,
            max_contracts=_bounded_int(
                payload.get("max_contracts"),
                settings.targeted_option_candle_backfill_max_contracts,
                maximum=500,
            ),
            batch_limit=_bounded_int(
                payload.get("batch_limit"),
                settings.targeted_option_candle_backfill_batch_limit,
                maximum=100,
            ),
            delay_seconds=float(
                payload.get(
                    "delay_seconds",
                    settings.targeted_option_candle_backfill_delay_seconds,
                )
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/data/ingest/all",
    tags=["11 Data Ingestion"],
    summary="Ingest candles and capture option snapshots",
    description="Runs historical underlying candle ingestion first, then captures current option-chain snapshots for the same symbols.",
    dependencies=PROTECTED_ROUTE,
)
def ingest_all_market_research_data(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "symbols": "BANKNIFTY",
                "timeframe": "5minute",
                "days": 90,
                "use_checkpoint": True,
                "overlap_minutes": 30,
                "strike_window_pct": 4.0,
                "max_contracts_per_symbol": 120,
            }
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    _require_manual_override_for_market_heavy_operation(
        payload, "full market research data ingestion"
    )
    try:
        return data_ingestion_service.ingest_all(
            symbols=parse_symbol_list(payload.get("symbols")),
            timeframe=str(payload.get("timeframe") or "5minute"),
            from_date=str(payload.get("from")) if payload.get("from") else None,
            to_date=str(payload.get("to")) if payload.get("to") else None,
            days=_bounded_int(payload.get("days"), 90, maximum=365),
            strike_window_pct=float(payload.get("strike_window_pct") or 4.0),
            max_contracts_per_symbol=_bounded_int(
                payload.get("max_contracts_per_symbol"), 120, maximum=200
            ),
            use_checkpoint=bool(payload.get("use_checkpoint", False)),
            overlap_minutes=int(
                payload.get("overlap_minutes")
                or settings.automation_checkpoint_overlap_minutes
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/research/settings",
    tags=["10 Research"],
    summary="Show option-buying research thresholds",
)
def get_research_settings() -> dict[str, object]:
    return {
        "option_quality": {
            "min_delta": settings.min_option_buy_delta,
            "max_delta": settings.max_option_buy_delta,
            "max_theta_pct": settings.max_option_buy_theta_pct,
            "min_iv": settings.min_option_buy_iv,
            "max_iv": settings.max_option_buy_iv,
            "min_quality_score": settings.min_option_quality_score,
            "min_buy_premium": settings.min_option_buy_premium,
            "max_bid_ask_spread_pct": settings.max_bid_ask_spread_pct,
        },
        "backtest": {
            "supported_timeframes": ["1minute", "5minute"],
            "default_horizon_candles": settings.backtest_horizon_candles,
            "mode": "underlying_rule_replay",
            "historical_option_snapshots": option_history_repository.count_snapshots(),
            "option_stop_loss_pct": settings.backtest_option_stop_loss_pct,
            "option_target_pct": settings.backtest_option_target_pct,
            "slippage_pct": settings.backtest_slippage_pct,
            "charges_pct": settings.backtest_charges_pct,
            "walk_forward_train_pct": settings.backtest_walk_forward_train_pct,
        },
        "after_market_research_job": {
            "enabled": settings.enable_after_market_research_job,
            "run_time": settings.after_market_research_time,
            "symbol": settings.after_market_research_symbol,
            "timeframe": settings.after_market_research_timeframe,
            "direction": settings.after_market_research_direction,
            "horizon_candles": settings.after_market_research_horizon_candles,
            "limit": settings.after_market_research_limit,
            "decision_mode": settings.after_market_research_decision_mode,
            "note": "Runs after market close and never inside live scanner decision flow.",
        },
        "strategy_edge_guard": {
            "enabled": settings.enable_strategy_edge_guard,
            "min_trades": settings.min_strategy_trades,
            "min_expectancy_pct": settings.min_strategy_expectancy_pct,
            "min_profit_factor": settings.min_strategy_profit_factor,
            "min_win_rate_pct": settings.min_strategy_win_rate_pct,
        },
        "institutional_decision_policy": {
            "active_timeframes": ["1minute", "5minute"],
            "structure_min_completed_candles": settings.structure_min_completed_candles,
            "direction_source": "completed_multi_candle_price_structure",
            "legacy_indicator_role": "diagnostic_only",
            "score_role": "ranking_only_after_primary_gates",
            "five_minute_role": "setup direction, regime, day structure, and completed-candle confirmation",
            "one_minute_role": "entry timing and fast confirmation",
            "shadow_only_layers": [
                "hierarchical_market_state",
                "momentum_phase",
                "setup_family_adjustment",
                "candidate_utility",
                "duplicate_regime_scores",
            ],
            "fast_context_max_age_seconds": settings.fast_scan_context_max_age_seconds,
            "scheduled_scan_max_rest_calls": settings.scheduled_scan_max_rest_calls,
            "note": "Shadow layers are collected for research and cannot block, approve, or adjust an entry.",
        },
        "evidence_governance": {
            "minimum_trades_per_cell": settings.evidence_matrix_min_trades,
            "minimum_expectancy_pct": settings.evidence_matrix_min_expectancy_pct,
            "minimum_profit_factor": settings.evidence_matrix_min_profit_factor,
            "maximum_drawdown_pct": settings.evidence_matrix_max_drawdown_pct,
            "automatic_live_mutation": False,
            "promotion_endpoint": "/research/strategy-promotion",
            "matrix_endpoint": "/research/evidence-matrix",
        },
        "market_insights": {
            "day_type_filter": settings.enable_day_type_filter,
            "min_day_type_score": settings.min_day_type_score,
            "opening_range_minutes": settings.opening_range_minutes,
            "option_premium_confirmation": settings.enable_option_premium_confirmation,
            "min_option_premium_confirmation_score": settings.min_option_premium_confirmation_score,
            "option_premium_lookback_candles": settings.option_premium_lookback_candles,
            "time_bucket_filter": settings.enable_time_bucket_filter,
            "min_time_bucket_trades": settings.min_time_bucket_trades,
            "min_time_bucket_expectancy_pct": settings.min_time_bucket_expectancy_pct,
            "outcome_learning_guard": settings.enable_outcome_learning_guard,
            "min_outcome_learning_trades": settings.min_outcome_learning_trades,
            "min_outcome_learning_expectancy_pct": settings.min_outcome_learning_expectancy_pct,
            "min_outcome_learning_win_rate_pct": settings.min_outcome_learning_win_rate_pct,
            "outcome_learning_lookback": settings.outcome_learning_lookback,
            "rejected_outcome_replay": {
                "enabled": settings.enable_rejected_outcome_candle_replay,
                "timeframes": settings.rejected_outcome_replay_timeframes,
                "max_candles": settings.rejected_outcome_replay_max_candles,
                "use_ws_token_candles": settings.rejected_outcome_use_ws_token_candles,
                "ambiguous_candle_policy": settings.rejected_outcome_ambiguous_candle_policy,
                "batch_limit": settings.rejected_outcome_batch_limit,
                "max_batches": settings.rejected_outcome_max_batches,
                "batch_delay_seconds": settings.rejected_outcome_batch_delay_seconds,
            },
            "targeted_option_candle_backfill": {
                "enabled": settings.enable_targeted_option_candle_backfill,
                "timeframes": settings.targeted_option_candle_backfill_timeframes,
                "batch_limit": settings.targeted_option_candle_backfill_batch_limit,
                "max_contracts": settings.targeted_option_candle_backfill_max_contracts,
                "batch_delay_seconds": settings.targeted_option_candle_backfill_delay_seconds,
                "min_coverage_pct": settings.min_targeted_option_candle_coverage_pct,
            },
            "live_option_candle_gap_backfill": {
                "enabled": settings.enable_live_option_candle_gap_backfill,
                "timeframes": settings.live_option_candle_backfill_timeframes,
                "lookback_minutes": settings.live_option_candle_backfill_lookback_minutes,
                "interval_seconds": settings.live_option_candle_backfill_interval_seconds,
                "min_gap_seconds": settings.live_option_candle_backfill_min_gap_seconds,
                "max_contracts": settings.live_option_candle_backfill_max_contracts,
                "batch_limit": settings.live_option_candle_backfill_batch_limit,
                "batch_delay_seconds": settings.live_option_candle_backfill_delay_seconds,
                "market_open_on_first_seen": settings.live_option_candle_backfill_market_open_on_first_seen,
                "session_start_time": settings.live_option_candle_backfill_session_start_time,
                "max_historical_calls_per_run": settings.live_option_candle_backfill_max_historical_calls_per_run,
                "on_demand_enabled": settings.enable_on_demand_premium_candle_backfill,
                "on_demand_cooldown_seconds": settings.on_demand_premium_candle_backfill_cooldown_seconds,
            },
            "banknifty_regime_filter": {
                "enabled": settings.enable_banknifty_regime_filter,
                "min_score": settings.min_banknifty_regime_score,
                "significant_gap_pct": settings.banknifty_significant_gap_pct,
                "compression_day_range_pct": settings.banknifty_compression_day_range_pct,
                "late_trade_cutoff_time": settings.banknifty_late_trade_cutoff_time,
                "late_trade_min_premium_score": settings.banknifty_late_trade_min_premium_score,
                "expiry_min_premium_score": settings.banknifty_expiry_min_premium_score,
            },
        },
        "automation_learning_loop": {
            "intraday_candle_sync": settings.automation_intraday_candle_sync,
            "intraday_candle_sync_minutes": settings.automation_intraday_candle_sync_minutes,
            "checkpoint_overlap_minutes": settings.automation_checkpoint_overlap_minutes,
            "snapshot_interval_seconds": settings.automation_snapshot_interval_seconds,
            "outcome_interval_seconds": settings.automation_outcome_interval_seconds,
            "auto_squareoff": settings.enable_auto_squareoff,
            "live_auto_squareoff": settings.live_auto_squareoff,
        },
        "execution_quality": {
            "enabled": settings.enforce_execution_quality,
            "default_order_mode": settings.default_order_mode,
            "max_execution_spread_pct": settings.max_execution_spread_pct,
            "max_entry_price_deviation_pct": settings.max_entry_price_deviation_pct,
            "min_execution_quote_price": settings.min_execution_quote_price,
            "max_trend_momentum_score": settings.max_trend_momentum_score,
            "option_time_stop_minutes": settings.option_time_stop_minutes,
            "option_time_stop_min_move_pct": settings.option_time_stop_min_move_pct,
            "option_trailing_stop_lock_pct": settings.option_trailing_stop_lock_pct,
            "exit_open_trades_before_close_minutes": settings.exit_open_trades_before_close_minutes,
            "fast_exit_interval_seconds": settings.fast_exit_interval_seconds,
            "underlying_invalidation_exit": settings.enable_underlying_invalidation_exit,
            "premium_invalidation_exit": settings.enable_premium_invalidation_exit,
            "broker_emergency_sl": {
                "enabled": settings.enable_broker_emergency_sl,
                "status": "supported_when_enabled_for_live_filled_buy_trades",
                "order_type": "SL-M",
                "fallback": "startup broker sync plus software square-off",
            },
            "partial_booking": {
                "enabled": settings.enable_partial_booking,
                "target1_pct": settings.partial_target1_pct,
                "move_sl_to_cost": settings.partial_move_sl_to_cost,
                "note": "disabled by default; only practical when quantity can be split by lot size",
            },
            "execution_realism": {
                "enabled": settings.enable_execution_realism,
                "entry_buy_slippage_pct": settings.realism_entry_buy_slippage_pct,
                "exit_target_slippage_pct": settings.realism_exit_target_slippage_pct,
                "exit_stop_overshoot_pct": settings.realism_exit_stop_overshoot_pct,
                "no_fill_touch_buffer_pct": settings.realism_no_fill_touch_buffer_pct,
                "first_15_min_extra_slippage_pct": settings.realism_first_15_min_extra_slippage_pct,
                "expiry_day_extra_slippage_pct": settings.realism_expiry_day_extra_slippage_pct,
                "high_iv_extra_slippage_pct": settings.realism_high_iv_extra_slippage_pct,
                "wide_spread_extra_slippage_pct": settings.realism_wide_spread_extra_slippage_pct,
            },
        },
        "freshness": {
            "max_live_quote_age_seconds": settings.max_live_quote_age_seconds,
            "max_live_option_quote_age_seconds": settings.max_live_option_quote_age_seconds,
            "max_live_chain_age_seconds": settings.max_live_chain_age_seconds,
            "max_live_candle_age_seconds": settings.max_live_candle_age_seconds,
            "max_paper_candle_age_seconds": settings.max_paper_candle_age_seconds,
        },
    }


@app.post(
    "/research/market-insights",
    tags=["10 Research"],
    summary="Inspect live market insight filters for a symbol and option contract",
    description="Use this to audit day type, option-premium confirmation, and time-bucket edge before trusting an option-buying signal.",
    dependencies=PROTECTED_ROUTE,
)
def get_market_insights(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "NIFTY",
                "trend": "bearish",
                "side": "BUY",
                "timeframe": "5minute",
                "contract": {
                    "tradingsymbol": "NIFTY2670723950PE",
                    "exchange": "NFO",
                    "instrument_token": 123456,
                    "name": "NIFTY",
                    "expiry": "2026-07-07",
                    "strike": 23950,
                    "option_type": "PE",
                    "lot_size": 75,
                    "last_price": 120.0,
                    "bid": 119.5,
                    "ask": 120.0,
                    "open_interest": 1000000,
                    "volume": 250000,
                },
            }
        ]
    ),
) -> dict[str, object]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    trend = str(payload.get("trend") or "").strip().lower()
    side = str(payload.get("side") or "BUY").strip().upper()
    timeframe = str(payload.get("timeframe") or "5minute").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if trend not in {"bullish", "bearish"}:
        raise HTTPException(status_code=400, detail="trend must be bullish or bearish")

    contract_payload = payload.get("contract")
    premium_eval: dict[str, object]
    if isinstance(contract_payload, dict):
        contract = OptionContract(
            tradingsymbol=str(contract_payload.get("tradingsymbol") or ""),
            exchange=str(contract_payload.get("exchange") or "NFO"),
            instrument_token=int(contract_payload["instrument_token"])
            if contract_payload.get("instrument_token")
            else None,
            name=str(contract_payload.get("name") or symbol),
            expiry=str(contract_payload.get("expiry") or ""),
            strike=float(contract_payload.get("strike") or 0),
            option_type=str(contract_payload.get("option_type") or ""),
            lot_size=int(contract_payload.get("lot_size") or 0),
            last_price=float(contract_payload.get("last_price") or 0),
            open_interest=float(contract_payload.get("open_interest") or 0),
            volume=float(contract_payload.get("volume") or 0),
            bid=float(contract_payload.get("bid") or 0),
            ask=float(contract_payload.get("ask") or 0),
        )
        premium_eval = option_premium_confirmation_service.evaluate(
            contract=contract, side=side, timeframe=timeframe
        )
    else:
        premium_eval = {
            "enabled": settings.enable_option_premium_confirmation,
            "score": 0,
            "passed": False,
            "reasons": ["contract is required for option premium confirmation"],
            "details": {},
        }

    return {
        "status": "ok",
        "symbol": symbol,
        "trend": trend,
        "side": side,
        "timeframe": timeframe,
        "insights": {
            "day_type": day_type_service.evaluate(
                symbol=symbol, trend=trend, timeframe=timeframe
            ),
            "option_premium_confirmation": premium_eval,
            "time_bucket_edge": time_bucket_edge_service.evaluate(
                symbol=symbol, trend=trend, timeframe=timeframe
            ),
        },
    }


@app.get(
    "/research/outcome-learning",
    tags=["10 Research"],
    summary="Analyze actual opportunity outcomes for adaptive setup guards",
    description="Shows which symbols, actions, setup types, day types, and time buckets have enough closed-trade evidence to trust or avoid.",
    dependencies=PROTECTED_ROUTE,
)
def get_outcome_learning() -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("outcome_learning"):
        return deferred
    return {"status": "ok", "learning": outcome_learning_service.analyze()}


@app.get(
    "/research/opportunity-analytics",
    tags=["10 Research"],
    summary="Analyze scanner opportunity outcomes by professional segments",
    description="Studies saved opportunities by CE/PE, expiry day, time bucket, score bucket, setup type, and failure tags.",
    dependencies=PROTECTED_ROUTE,
)
def get_opportunity_analytics(
    symbol: str = "BANKNIFTY", limit: int = 1000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("opportunity_analytics"):
        return deferred
    return opportunity_analytics_service.analyze(
        symbol=symbol, limit=_bounded_int(limit, 1000, maximum=3000)
    )


@app.get(
    "/research/execution-analytics",
    tags=["10 Research"],
    summary="Analyze paper/live execution quality and trade outcomes",
    description="Separates execution quality from signal quality: fill rate, entry deviation, CE/PE results, time bucket, and P&L metrics.",
    dependencies=PROTECTED_ROUTE,
)
def get_execution_analytics(
    symbol: str = "BANKNIFTY", limit: int = 1000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("execution_analytics"):
        return deferred
    return execution_analytics_service.analyze(
        symbol=symbol, limit=_bounded_int(limit, 1000, maximum=3000)
    )


@app.get(
    "/research/professional-insights",
    tags=["10 Research"],
    summary="Professional Bank Nifty decision-quality insights",
    description="Combines accepted vs rejected analysis, time buckets, DTE, factor attribution, exits, data quality, and shadow/live evidence.",
    dependencies=PROTECTED_ROUTE,
)
def get_professional_insights(
    symbol: str = "BANKNIFTY", limit: int = 1000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("professional_insights"):
        return deferred
    return professional_insights_service.analyze(
        symbol=symbol, limit=_bounded_int(limit, 1000, maximum=3000)
    )


@app.get(
    "/research/research-engine",
    tags=["10 Research"],
    summary="Research engine report for accepted and rejected Bank Nifty setups",
    description=(
        "Shows filter rejection quality, missed rejected winners, accepted loss impact, and net expectancy "
        "by setup family, weekday, time block, DTE, IV regime, and trend regime. Read-only; does not alter strategy."
    ),
    dependencies=PROTECTED_ROUTE,
)
def get_research_engine_report(
    symbol: str = "BANKNIFTY", limit: int = 2000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("research_engine"):
        return deferred
    return professional_insights_service.research_engine_report(
        symbol=symbol, limit=_bounded_int(limit, 2000, maximum=5000)
    )


@app.get(
    "/research/gate-effectiveness",
    tags=["10 Research"],
    summary="Measure which rejection gates helped or hurt",
    description="Shows missed winners, saved losers, unresolved rows, ambiguity, average move, and time-to-outcome by rejection gate.",
    dependencies=PROTECTED_ROUTE,
)
def get_gate_effectiveness_report(
    symbol: str = "BANKNIFTY",
    limit: int = 3000,
    summary_only: bool = False,
    top_n: int | None = None,
    cache_seconds: int = 300,
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("gate_effectiveness"):
        return deferred
    bounded_limit = _bounded_int(limit, 3000, maximum=5000)
    bounded_top_n = _bounded_int(top_n, 12, maximum=100) if top_n is not None else None
    cache_key = (
        "gate_effectiveness",
        symbol.upper() if symbol else "ALL",
        bounded_limit,
        bool(summary_only),
        bounded_top_n,
    )
    return _cached_research_report(
        cache_key,
        cache_seconds=_bounded_int(cache_seconds, 300, minimum=0, maximum=900),
        builder=lambda: professional_insights_service.gate_effectiveness_report(
            symbol=symbol,
            limit=bounded_limit,
            summary_only=summary_only,
            top_n=bounded_top_n,
        ),
    )


@app.get(
    "/research/rejected-opportunity-quality",
    tags=["10 Research"],
    summary="Summarize rejected setup later outcomes",
    description="Shows missed winners, saved losers, unresolved and ambiguous rejected setups after candle replay.",
    dependencies=PROTECTED_ROUTE,
)
def get_rejected_opportunity_quality_report(
    symbol: str = "BANKNIFTY", limit: int = 3000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("rejected_opportunity_quality"):
        return deferred
    return professional_insights_service.rejected_opportunity_quality_report(
        symbol=symbol, limit=_bounded_int(limit, 3000, maximum=5000)
    )


@app.get(
    "/research/threshold-validation",
    tags=["10 Research"],
    summary="Validate whether current strategy thresholds are helping or hurting",
    description=(
        "Read-only threshold evidence report. It inventories configured thresholds and validates score buckets, "
        "rejection gates, setup families, time/regime segments, and score sensitivity without changing live strategy."
    ),
    dependencies=PROTECTED_ROUTE,
)
def get_threshold_validation_report(
    symbol: str = "BANKNIFTY",
    limit: int = 3000,
    start_date: str | None = None,
    end_date: str | None = None,
    setup_family: str | None = None,
    mode: str = "all",
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("threshold_validation"):
        return deferred
    parsed_start = datetime.fromisoformat(start_date).date() if start_date else None
    parsed_end = datetime.fromisoformat(end_date).date() if end_date else None
    return professional_insights_service.threshold_validation_report(
        symbol=symbol,
        limit=_bounded_int(limit, 3000, maximum=5000),
        start_date=parsed_start,
        end_date=parsed_end,
        setup_family=setup_family,
        mode=mode,
    )


@app.get(
    "/research/execution-realism",
    tags=["10 Research"],
    summary="Inspect conservative paper/backtest fill realism and execution drag",
    dependencies=PROTECTED_ROUTE,
)
def get_execution_realism_report(
    symbol: str = "BANKNIFTY", limit: int = 1000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("execution_realism"):
        return deferred
    return professional_insights_service.execution_realism_report(
        symbol=symbol, limit=_bounded_int(limit, 1000, maximum=3000)
    )


@app.get(
    "/research/daily-review",
    tags=["10 Research"],
    summary="Daily Bank Nifty trading review",
    description="Shows the day's accepted setups, rejected setups, trades, outcomes, top rejection gates, and review timeline.",
    dependencies=PROTECTED_ROUTE,
)
def get_daily_review(
    symbol: str = "BANKNIFTY", review_date: str | None = None, limit: int = 1000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("daily_review"):
        return deferred
    parsed_date = datetime.fromisoformat(review_date).date() if review_date else None
    return professional_insights_service.daily_review(
        symbol=symbol,
        review_date=parsed_date,
        limit=_bounded_int(limit, 1000, maximum=3000),
    )


@app.get(
    "/research/daily-banknifty-summary",
    tags=["10 Research"],
    summary="Minimal daily Bank Nifty evidence summary",
    description="Small paper/live-shadow evidence summary for one Bank Nifty session. This does not replay rejected trades or change strategy.",
    dependencies=PROTECTED_ROUTE,
)
def get_daily_banknifty_summary(date: str | None = None) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("daily_banknifty_summary"):
        return deferred
    parsed_date = datetime.fromisoformat(date).date() if date else None
    return professional_insights_service.daily_banknifty_summary(
        summary_date=parsed_date
    )


@app.get(
    "/research/after-market/status",
    tags=["10 Research"],
    summary="Get after-market research job status",
    description="Shows the scheduled after-market evidence job status. This job does not run inside live scanning.",
)
def get_after_market_research_status() -> dict[str, object]:
    return after_market_research_service.status()


@app.post(
    "/research/after-market/run",
    tags=["10 Research"],
    summary="Run after-market research job once",
    description=(
        "Runs the after-market evidence job manually. By default it only runs after market close; pass "
        "`force=true` only when you deliberately want a manual research refresh."
    ),
    dependencies=PROTECTED_ROUTE,
)
def run_after_market_research(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[{"force": False}],
    ),
) -> dict[str, object]:
    payload = payload or {}
    _require_manual_override_for_market_heavy_operation(
        payload, "after-market research job"
    )
    if hasattr(after_market_research_service, "run_async"):
        return after_market_research_service.run_async(
            trigger="manual", force=bool(payload.get("force", False))
        )
    return after_market_research_service.run_once(
        trigger="manual", force=bool(payload.get("force", False))
    )


@app.get(
    "/research/trade-journal",
    tags=["10 Research"],
    summary="Unified opportunity, rejection, and trade timeline",
    dependencies=PROTECTED_ROUTE,
)
def get_trade_journal(symbol: str = "BANKNIFTY", limit: int = 200) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("trade_journal"):
        return deferred
    return professional_insights_service.trade_journal(
        symbol=symbol, limit=_bounded_int(limit, 200, maximum=1000)
    )


@app.get(
    "/research/data-completeness",
    tags=["10 Research"],
    summary="Inspect stored candle and option snapshot completeness",
    dependencies=PROTECTED_ROUTE,
)
def get_data_completeness(symbol: str = "BANKNIFTY") -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("data_completeness"):
        return deferred
    return professional_insights_service.data_completeness(symbol=symbol)


@app.get(
    "/research/shadow-comparison",
    tags=["10 Research"],
    summary="Compare shadow, paper, and live trade evidence",
    dependencies=PROTECTED_ROUTE,
)
def get_shadow_comparison(
    symbol: str = "BANKNIFTY", limit: int = 1000
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("shadow_comparison"):
        return deferred
    return professional_insights_service.analyze(
        symbol=symbol, limit=_bounded_int(limit, 1000, maximum=3000)
    )["shadow_mode_comparison"]


@app.get(
    "/research/professional-readiness",
    tags=["10 Research"],
    summary="Combined professional-readiness report for Bank Nifty option buying",
    description="Combines option data coverage, opportunity outcomes, execution analytics, option backtest, ablation, and walk-forward validation.",
    dependencies=PROTECTED_ROUTE,
)
def get_professional_readiness(
    symbol: str = "BANKNIFTY",
    timeframe: str = "5minute",
    direction: str = "BOTH",
    limit: int = 3000,
) -> dict[str, object]:
    status = after_market_research_service.status()
    latest = status.get("last_result") if isinstance(status, dict) else None
    if not isinstance(latest, dict):
        job_run = status.get("job_run", {}) if isinstance(status, dict) else {}
        metadata = job_run.get("metadata", {}) if isinstance(job_run, dict) else {}
        latest = metadata.get("result") if isinstance(metadata, dict) else None
    reports = latest.get("reports", {}) if isinstance(latest, dict) else {}
    stage = (
        reports.get("professional_readiness", {}) if isinstance(reports, dict) else {}
    )
    result = stage.get("result") if isinstance(stage, dict) else None
    if isinstance(result, dict):
        return {
            **result,
            "source": "after_market_research_cache",
            "non_blocking": True,
            "last_run_date": status.get("last_run_date"),
        }
    return {
        "status": "not_ready",
        "ready_for_live": False,
        "source": "after_market_research_cache",
        "non_blocking": True,
        "message": "No completed professional-readiness report is cached. Queue /research/after-market/run after market close.",
        "job": status.get("job_run") if isinstance(status, dict) else None,
    }


@app.post(
    "/research/greeks",
    tags=["10 Research"],
    summary="Estimate IV and Greeks for an option",
    description="Use this to inspect whether a proposed option has usable delta, acceptable theta decay, and reasonable IV.",
    dependencies=PROTECTED_ROUTE,
)
def estimate_greeks(
    payload: dict[str, object] = Body(
        examples=[
            {
                "spot_price": 796,
                "strike": 800,
                "option_price": 2.4,
                "option_type": "PE",
                "expiry": "2026-06-30",
            }
        ]
    ),
) -> dict[str, object]:
    try:
        greeks = greeks_service.estimate(
            spot_price=float(payload["spot_price"]),
            strike=float(payload["strike"]),
            option_price=float(payload["option_price"]),
            option_type=str(payload["option_type"]),
            expiry=payload.get("expiry"),
        )
        return {"status": "ok", "greeks": greeks.to_dict()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/option-quality",
    tags=["10 Research"],
    summary="Score an option contract for buying quality",
    description="Applies the same option-quality gate used by the scanner: delta, theta, IV, expiry, spread, and premium noise.",
    dependencies=PROTECTED_ROUTE,
)
def score_option_quality(
    payload: dict[str, object] = Body(
        examples=[
            {
                "spot_price": 796,
                "entry_price": 2.4,
                "side": "BUY",
                "contract": {
                    "tradingsymbol": "BANKNIFTY2670758000PE",
                    "exchange": "NFO",
                    "name": "BANKNIFTY",
                    "expiry": "2026-06-30",
                    "strike": 800,
                    "option_type": "PE",
                    "lot_size": 550,
                    "last_price": 2.4,
                    "bid": 2.3,
                    "ask": 2.4,
                    "open_interest": 3550800,
                    "volume": 2090550,
                },
            }
        ]
    ),
) -> dict[str, object]:
    try:
        contract_payload = payload.get("contract") or payload
        if not isinstance(contract_payload, dict):
            raise ValueError("contract must be an object")
        contract = OptionContract(
            tradingsymbol=str(contract_payload.get("tradingsymbol") or ""),
            exchange=str(contract_payload.get("exchange") or settings.option_exchange),
            instrument_token=int(contract_payload["instrument_token"])
            if contract_payload.get("instrument_token") is not None
            else None,
            name=str(contract_payload.get("name") or payload.get("symbol") or ""),
            expiry=str(contract_payload.get("expiry") or ""),
            strike=float(contract_payload.get("strike") or 0),
            option_type=str(
                contract_payload.get("option_type")
                or contract_payload.get("instrument_type")
                or ""
            ),
            lot_size=int(contract_payload.get("lot_size") or 1),
            last_price=float(
                contract_payload.get("last_price") or payload.get("entry_price") or 0
            ),
            open_interest=float(contract_payload.get("open_interest") or 0),
            volume=float(contract_payload.get("volume") or 0),
            bid=float(contract_payload.get("bid") or 0),
            ask=float(contract_payload.get("ask") or 0),
        )
        evaluation = option_quality_service.evaluate(
            spot_price=float(payload["spot_price"]),
            contract=contract,
            entry_price=float(payload.get("entry_price") or contract.last_price),
            side=str(payload.get("side") or "BUY"),
        )
        return {"status": "ok", "evaluation": evaluation}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/backtest",
    tags=["10 Research"],
    summary="Replay stored candles against directional option-buying rules",
    description=(
        "Runs an underlying-candle rule replay. This improves scanner filters, but exact option P&L still needs "
        "historical option-chain quotes, bid/ask, IV, and Greeks snapshots."
    ),
    dependencies=PROTECTED_ROUTE,
)
def run_research_backtest(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "NIFTY",
                "timeframe": "5minute",
                "side": "BUY",
                "direction": "BOTH",
                "horizon_candles": 12,
                "limit": 2000,
            }
        ]
    ),
) -> dict[str, object]:
    _require_manual_override_for_market_heavy_operation(payload, "research backtest")
    if deferred := _defer_review_analysis_during_market(
        "backtest", manual_override=_manual_override_requested(payload)
    ):
        return deferred
    try:
        return backtest_service.run(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            side=str(payload.get("side") or "BUY"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"])
            if payload.get("horizon_candles") is not None
            else None,
            limit=_bounded_int(payload.get("limit"), 2000, maximum=5000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/backtest/options",
    tags=["10 Research"],
    summary="Replay strategy on historical option premium candles",
    description="Uses stored option OHLC candles to simulate real option entry, premium stop loss, target, slippage, and exits.",
    dependencies=PROTECTED_ROUTE,
)
def run_option_premium_backtest(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "NIFTY",
                "timeframe": "5minute",
                "direction": "BOTH",
                "decision_mode": "scanner_parity",
                "horizon_candles": 12,
                "limit": 3000,
            }
        ]
    ),
) -> dict[str, object]:
    _require_manual_override_for_market_heavy_operation(
        payload, "option premium backtest"
    )
    if deferred := _defer_review_analysis_during_market(
        "option_premium_backtest", manual_override=_manual_override_requested(payload)
    ):
        return deferred
    try:
        return backtest_service.run_option_premium(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"])
            if payload.get("horizon_candles") is not None
            else None,
            limit=_bounded_int(payload.get("limit"), 3000, maximum=5000),
            decision_mode=str(payload.get("decision_mode") or "scanner_parity"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/backtest/ablation",
    tags=["10 Research"],
    summary="Compare backtest performance with selected factors removed",
    description="Runs option-premium replay variants to identify which available factors improve or hurt net expectancy.",
    dependencies=PROTECTED_ROUTE,
)
def run_ablation_backtest(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "BANKNIFTY",
                "timeframe": "5minute",
                "direction": "BOTH",
                "horizon_candles": 12,
                "limit": 1000,
            }
        ]
    ),
) -> dict[str, object]:
    _require_manual_override_for_market_heavy_operation(payload, "ablation backtest")
    if deferred := _defer_review_analysis_during_market(
        "ablation_backtest", manual_override=_manual_override_requested(payload)
    ):
        return deferred
    try:
        return backtest_service.run_ablation(
            symbol=str(payload.get("symbol") or "BANKNIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"])
            if payload.get("horizon_candles") is not None
            else None,
            limit=_bounded_int(payload.get("limit"), 1000, maximum=3000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/walk-forward",
    tags=["10 Research"],
    summary="Run walk-forward option-premium validation",
    description="Splits historical data into train/test periods and validates out-of-sample option-premium performance.",
    dependencies=PROTECTED_ROUTE,
)
def run_walk_forward_validation(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "NIFTY",
                "timeframe": "5minute",
                "direction": "BOTH",
                "decision_mode": "scanner_parity",
                "horizon_candles": 12,
                "limit": 3000,
            }
        ]
    ),
) -> dict[str, object]:
    _require_manual_override_for_market_heavy_operation(
        payload, "walk-forward validation"
    )
    if deferred := _defer_review_analysis_during_market(
        "walk_forward", manual_override=_manual_override_requested(payload)
    ):
        return deferred
    try:
        return backtest_service.run_walk_forward(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"])
            if payload.get("horizon_candles") is not None
            else None,
            limit=_bounded_int(payload.get("limit"), 3000, maximum=5000),
            decision_mode=str(payload.get("decision_mode") or "scanner_parity"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/strategy-edge/validate",
    tags=["10 Research"],
    summary="Validate and save measured strategy edge",
    description="Runs walk-forward validation, stores the result, and makes it available to the optional scanner edge guard.",
    dependencies=PROTECTED_ROUTE,
)
def validate_strategy_edge(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "NIFTY",
                "timeframe": "5minute",
                "direction": "CALL",
                "limit": 3000,
            }
        ]
    ),
) -> dict[str, object]:
    _require_manual_override_for_market_heavy_operation(
        payload, "strategy edge validation"
    )
    if deferred := _defer_review_analysis_during_market(
        "strategy_edge_validation", manual_override=_manual_override_requested(payload)
    ):
        return deferred
    try:
        return strategy_edge_service.validate(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            limit=_bounded_int(payload.get("limit"), 3000, maximum=5000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/research/strategy-edge",
    tags=["10 Research"],
    summary="List recent saved strategy edge validations",
)
def list_strategy_edge(limit: int = 50) -> dict[str, object]:
    rows = strategy_edge_service.recent(limit=limit)
    return {"count": len(rows), "validations": rows}


@app.post(
    "/research/strategy-ranking",
    tags=["10 Research"],
    summary="Rank symbols and directions by measured walk-forward edge",
    description="Runs/saves strategy validations and returns the highest expectancy setups first.",
    dependencies=PROTECTED_ROUTE,
)
def rank_strategy_edge(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbols": "BANKNIFTY",
                "directions": "CALL,PUT",
                "timeframe": "5minute",
                "limit": 3000,
            }
        ]
    ),
) -> dict[str, object]:
    _require_manual_override_for_market_heavy_operation(payload, "strategy ranking")
    if deferred := _defer_review_analysis_during_market(
        "strategy_ranking", manual_override=_manual_override_requested(payload)
    ):
        return deferred
    symbols = parse_symbol_list(payload.get("symbols"))
    direction_value = payload.get("directions") or "CALL,PUT"
    directions = [
        item.strip().upper() for item in str(direction_value).split(",") if item.strip()
    ]
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        for direction in directions:
            try:
                rows.append(
                    strategy_edge_service.validate(
                        symbol=symbol,
                        timeframe=str(payload.get("timeframe") or "5minute"),
                        direction=direction,
                        limit=_bounded_int(payload.get("limit"), 3000, maximum=5000),
                    )
                )
            except Exception as exc:
                rows.append(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "passed": False,
                        "reasons": [str(exc)],
                        "summary": {},
                    }
                )
    ranked = sorted(
        rows,
        key=lambda row: (
            bool(row.get("passed")),
            float((row.get("summary") or {}).get("expectancy_pct") or 0),
            float((row.get("summary") or {}).get("profit_factor") or 0),
        ),
        reverse=True,
    )
    return {"count": len(ranked), "ranked": ranked}


@app.post(
    "/research/option-history/import",
    tags=["10 Research"],
    summary="Import historical option quote snapshots",
    description="Stores historical option-chain quotes, IV, and Greeks snapshots in MySQL so exact option backtests can be built from real option history.",
    dependencies=PROTECTED_ROUTE,
)
def import_option_history(
    payload: dict[str, object] = Body(
        examples=[
            {
                "snapshots": [
                    {
                        "underlying": "BANKNIFTY",
                        "tradingsymbol": "BANKNIFTY2670758000PE",
                        "exchange": "NFO",
                        "timestamp": "2026-06-30 12:15:00",
                        "expiry": "2026-06-30",
                        "strike": 800,
                        "option_type": "PE",
                        "last_price": 2.4,
                        "bid": 2.3,
                        "ask": 2.4,
                        "iv": 0.22,
                        "delta": -0.48,
                        "gamma": 0.03,
                        "theta": -0.35,
                        "vega": 0.07,
                        "oi": 3550800,
                        "volume": 2090550,
                    }
                ]
            }
        ]
    ),
) -> dict[str, object]:
    _require_manual_override_for_market_heavy_operation(
        payload, "option history import"
    )
    if deferred := _defer_review_analysis_during_market(
        "option_history_import", manual_override=_manual_override_requested(payload)
    ):
        return deferred
    try:
        rows = payload.get("snapshots")
        if not isinstance(rows, list):
            raise ValueError("snapshots must be a list")
        return option_history_repository.import_snapshots(
            [row for row in rows if isinstance(row, dict)]
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/research/option-history",
    tags=["10 Research"],
    summary="List stored historical option quote snapshots",
)
def list_option_history(
    underlying: str | None = None, limit: int = 20
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("option_history"):
        return deferred
    records = option_history_repository.latest_snapshots(
        underlying=underlying, limit=limit
    )
    return {
        "count": len(records),
        "total_snapshots": option_history_repository.count_snapshots(underlying),
        "coverage": option_history_repository.coverage_summary(underlying),
        "snapshots": [option_quote_snapshot_to_dict(record) for record in records],
    }


@app.get("/signals", tags=["03 Scanner"], summary="Generate and save ranked signals")
def get_signals(side: str = "BUY", limit: int = 10) -> list[dict[str, object]]:
    scanner_service = get_scanner_service()
    recommendations = scanner_service.scan_symbols(
        side=side.upper(),
        rejection_source="manual_scan",
    )
    for signal in recommendations:
        signal_repository.save_signal(
            symbol=signal.symbol,
            action=signal.action,
            score=signal.score,
            confidence=signal.confidence,
            trend="bullish" if signal.action == "BUY_CE" else "bearish",
            explanation=signal.explanation,
        )
    return [asdict(signal) for signal in recommendations[:limit]]


@app.get(
    "/scanner/opportunities",
    tags=["03 Scanner"],
    summary="Scan option opportunities and save them to DB",
    description="Use this for manual Bank Nifty scanning. Example: `/scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3`.",
)
def get_opportunities(
    side: str = "BUY",
    symbols: str | None = None,
    limit: int = 10,
    order_mode: str = "paper",
) -> dict[str, object]:
    if not _scanner_market_is_open():
        return {
            "mode": order_mode.lower(),
            "market_data": "not_requested",
            "kite_access_token": bool(load_access_token()),
            "side": side.upper(),
            "count": 0,
            "saved_ids": [],
            "opportunities": [],
            "market_open": False,
            "reason": "market_closed",
            "disclaimer": "Scanner opportunities are only evaluated during configured market hours.",
        }
    cache_key = _scanner_cache_key(
        side=side, symbols=symbols, limit=limit, order_mode=order_mode
    )
    cached = _scanner_cached_response(cache_key)
    if cached is not None:
        if cached.get("refresh_due"):
            _start_scanner_refresh(
                cache_key=cache_key,
                side=side,
                symbols=symbols,
                limit=limit,
                order_mode=order_mode,
            )
        return cached["payload"]  # type: ignore[return-value]

    _start_scanner_refresh(
        cache_key=cache_key,
        side=side,
        symbols=symbols,
        limit=limit,
        order_mode=order_mode,
    )
    return {
        "status": "warming",
        "mode": order_mode.lower(),
        "market_data": "refresh_in_progress",
        "kite_access_token": bool(load_access_token()),
        "side": side.upper(),
        "count": 0,
        "saved_ids": [],
        "opportunities": [],
        "market_open": True,
        "reason": "scanner_refresh_started",
        "disclaimer": "Scanner refresh is running in the background; retry shortly for the latest cached result.",
    }


def _build_scanner_opportunities_payload(
    side: str, symbols: str | None, limit: int, order_mode: str
) -> dict[str, object]:
    scanner_service = get_scanner_service()
    symbol_list = (
        [item.strip().upper() for item in symbols.split(",")] if symbols else None
    )
    recommendations = scanner_service.scan_symbols(
        symbols=symbol_list,
        side=side.upper(),
        order_mode=order_mode.lower(),
        rejection_source="manual_scan",
    )
    saved_ids = [
        opportunity_repository.save_opportunity(signal).id
        for signal in recommendations[:limit]
    ]
    return {
        "mode": order_mode.lower(),
        "market_data": type(scanner_service.feed).__name__,
        "kite_access_token": bool(load_access_token()),
        "side": side.upper(),
        "count": len(recommendations),
        "saved_ids": saved_ids,
        "opportunities": [asdict(signal) for signal in recommendations[:limit]],
        "disclaimer": "Signals are score-ranked; uncalibrated heuristic confidence is not a probability and trades are not guaranteed.",
    }


def _scanner_cache_key(
    *, side: str, symbols: str | None, limit: int, order_mode: str
) -> str:
    normalized_symbols = ",".join(
        item.strip().upper() for item in (symbols or "").split(",") if item.strip()
    )
    return "|".join(
        [
            side.upper(),
            normalized_symbols or "DEFAULT",
            str(max(1, int(limit))),
            order_mode.lower(),
        ]
    )


def _scanner_cached_response(cache_key: str) -> dict[str, object] | None:
    now = ist_now_naive()
    fresh_ttl = max(1, int(settings.scanner_response_cache_ttl_seconds))
    stale_ttl = max(fresh_ttl, int(settings.scanner_response_stale_ttl_seconds))
    with scanner_response_cache_lock:
        entry = scanner_response_cache.get(cache_key)
        if not entry:
            return None
        payload = entry.get("payload")
        cached_at = entry.get("cached_at")
        if not isinstance(payload, dict) or not isinstance(cached_at, datetime):
            return None
        age = (now - cached_at).total_seconds()
        if age > stale_ttl:
            return None
        response = {**payload, "cached": True, "cached_age_seconds": round(age, 3)}
        return {"payload": response, "refresh_due": age > fresh_ttl}


def _start_scanner_refresh(
    *, cache_key: str, side: str, symbols: str | None, limit: int, order_mode: str
) -> None:
    now = ist_now_naive()
    stuck_after = max(5, int(settings.scanner_refresh_stuck_seconds))
    with scanner_response_cache_lock:
        entry = scanner_response_cache.setdefault(cache_key, {})
        started_at = entry.get("refresh_started_at")
        if (
            entry.get("refreshing")
            and isinstance(started_at, datetime)
            and (now - started_at).total_seconds() < stuck_after
        ):
            return
        entry["refreshing"] = True
        entry["refresh_started_at"] = now

    thread = threading.Thread(
        target=_refresh_scanner_cache,
        kwargs={
            "cache_key": cache_key,
            "side": side,
            "symbols": symbols,
            "limit": limit,
            "order_mode": order_mode,
        },
        name=f"scanner-refresh-{cache_key}",
        daemon=True,
    )
    thread.start()


def _refresh_scanner_cache(
    *, cache_key: str, side: str, symbols: str | None, limit: int, order_mode: str
) -> None:
    started = ist_now_naive()
    try:
        payload = _build_scanner_opportunities_payload(
            side=side, symbols=symbols, limit=limit, order_mode=order_mode
        )
        payload = {
            **payload,
            "status": "ok",
            "cached": False,
            "refreshed_at": ist_now_naive().isoformat(sep=" "),
        }
        with scanner_response_cache_lock:
            scanner_response_cache[cache_key] = {
                "payload": payload,
                "cached_at": ist_now_naive(),
                "refreshing": False,
                "last_duration_seconds": round(
                    (ist_now_naive() - started).total_seconds(), 3
                ),
            }
    except Exception as exc:
        logger.exception("scanner refresh failed")
        with scanner_response_cache_lock:
            entry = scanner_response_cache.setdefault(cache_key, {})
            entry["refreshing"] = False
            entry["last_error"] = str(exc)


def _scanner_market_is_open() -> bool:
    return _current_market_session() == "REGULAR_MARKET"


def _current_market_session() -> str:
    now = ist_now_naive()
    mode = market_session_service.current_runtime_mode(now)
    if mode in {"MARKET_OPEN", "MARKET_CLOSING", "MANUAL_OVERRIDE"}:
        return "REGULAR_MARKET"
    if mode == "AFTER_MARKET_REVIEW":
        return "AFTER_MARKET"
    if mode == "HOLIDAY":
        return "HOLIDAY"
    if mode == "PRE_MARKET":
        return "PRE_MARKET"
    if now.weekday() >= 5:
        return "WEEKEND"
    if now.time() > _parse_market_time(settings.runtime_market_close_time):
        return "AFTER_MARKET"
    return "MARKET_CLOSED"


def _defer_review_analysis_during_market(
    report: str, *, manual_override: bool = False
) -> dict[str, object] | None:
    session = _current_market_session()
    if session != "REGULAR_MARKET" or manual_override:
        return None
    return {
        "status": "deferred",
        "reason": "market_open",
        "market_session": session,
        "report": report,
        "message": "Review and learning analysis is deferred until after market close to keep live trading responsive.",
    }


def _require_manual_override_for_market_heavy_operation(
    payload: dict[str, object], operation: str
) -> None:
    if _current_market_session() != "REGULAR_MARKET":
        return
    if bool(
        payload.get("manual_override")
        or payload.get("force")
        or settings.runtime_manual_override
    ):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"{operation} is blocked during market hours to keep runtime responsive; "
            "pass manual_override=true only for a deliberate manual refresh."
        ),
    )


def _manual_override_requested(payload: dict[str, object]) -> bool:
    return bool(
        payload.get("manual_override")
        or payload.get("force")
        or settings.runtime_manual_override
    )


def _parse_market_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


@app.get(
    "/scanner/diagnostics",
    tags=["03 Scanner"],
    summary="Explain why symbols passed or failed scanner gates",
    description="Use after `/scanner/opportunities` when a symbol is not appearing or when a signal needs explanation.",
)
def get_scanner_diagnostics(
    side: str = "BUY",
    symbols: str | None = None,
    limit: int = 25,
    order_mode: str = "paper",
) -> dict[str, object]:
    scanner_service = get_scanner_service()
    symbol_list = (
        [item.strip().upper() for item in symbols.split(",")] if symbols else None
    )
    diagnostics = scanner_service.scan_with_diagnostics(
        symbols=symbol_list,
        side=side.upper(),
        order_mode=order_mode.lower(),
        rejection_source="manual_diagnostic",
    )
    rows: list[dict[str, object]] = []
    for item in diagnostics[:limit]:
        row = dict(item)
        signal = row.get("signal")
        row["signal"] = asdict(signal) if signal is not None else None
        rows.append(row)
    return {
        "mode": order_mode.lower(),
        "market_data": type(scanner_service.feed).__name__,
        "kite_access_token": bool(load_access_token()),
        "use_kite_market_data": settings.use_kite_market_data,
        "websocket": active_trade_price_feed.status(),
        "side": side.upper(),
        "count": len(rows),
        "diagnostics": rows,
    }


@app.get(
    "/scanner/armed-entries",
    tags=["03 Scanner"],
    summary="Inspect event-driven armed paper entries",
    description="Shows Bank Nifty option setups waiting for WebSocket trigger, plus entered/expired/too-late states.",
)
def get_scanner_armed_entries() -> dict[str, object]:
    if armed_entry_tracker_service is None:
        return {"status": "unavailable", "reason": "armed_entry_tracker_unavailable"}
    return armed_entry_tracker_service.list_entries()


@app.post(
    "/orders/place",
    tags=["04 Orders"],
    summary="Place a paper or live order from a scanner signal",
    description=(
        "Paper order: send `confirm_live=false`. Live order requires `LIVE_TRADING_MODE=true`, "
        "`PAPER_TRADING_MODE=false`, and `confirm_live=true`."
    ),
    dependencies=PROTECTED_ROUTE,
)
def place_order(
    payload: dict[str, object] = Body(
        examples=[
            {
                "confirm_live": False,
                "signal": {
                    "symbol": "BANKNIFTY",
                    "action": "BUY_CE",
                    "side": "BUY",
                    "tradingsymbol": "BANKNIFTY26JUL58000CE",
                    "exchange": "NFO",
                    "expiry": "2026-07-26",
                    "strike": 58000,
                    "entry_price": 100,
                    "stop_loss": 78,
                    "target_1": 125,
                    "quantity": 15,
                    "lot_size": 15,
                    "score": 85,
                    "factor_scores": {
                        "strategy_metadata": {
                            "strategy_name": "banknifty_option_buying",
                            "strategy_version": "banknifty_option_buying_v1",
                        },
                        "contract": {"expiry": "2026-07-26"},
                    },
                },
            }
        ]
    ),
) -> dict[str, object]:
    from app.models import Signal

    try:
        signal_payload = payload.get("signal")
        if not isinstance(signal_payload, dict):
            signal_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"confirm_live", "opportunity_id", "order_mode"}
            }
        signal = Signal(**signal_payload)  # type: ignore[arg-type]
        confirm_live = bool(payload.get("confirm_live", False))
        order_mode = str(
            payload.get("order_mode") or settings.default_order_mode or "paper"
        ).lower()
        opportunity_id_value = payload.get("opportunity_id")
        opportunity_id = (
            int(opportunity_id_value) if opportunity_id_value is not None else None
        )
        return get_order_service().place_signal_order(
            signal,
            confirm_live=confirm_live,
            opportunity_id=opportunity_id,
            order_mode=order_mode,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/risk/status", tags=["04 Orders"], summary="Check daily risk guard status")
def get_risk_status() -> dict[str, object]:
    return {
        **risk_management_service.evaluate_entry(),
        "broker_reconciliation": broker_sync_service.live_block_status(),
    }


@app.get(
    "/trades",
    tags=["04 Orders"],
    summary="List actual paper/live trade lifecycle records",
)
def list_trades(
    status: str | None = None, limit: int = 100, include_artifacts: bool = False
) -> dict[str, object]:
    records = trade_repository.list_trades(
        status=status, limit=limit, include_artifacts=include_artifacts
    )
    return {
        "count": len(records),
        "trades": [trade_record_to_dict(record) for record in records],
    }


@app.get(
    "/trades/exit-alerts",
    tags=["04 Orders"],
    summary="List live trades stuck in closing, exit_failed, or reconciliation mismatch",
)
def trade_exit_alerts(limit: int = 100) -> dict[str, object]:
    records = trade_repository.exit_alerts(limit=limit)
    return {
        "count": len(records),
        "alerts": [trade_record_to_dict(record) for record in records],
        "broker_reconciliation": broker_sync_service.live_block_status(),
    }


@app.get(
    "/trades/test-artifacts",
    tags=["04 Orders"],
    summary="List suspected synthetic test rows in trades",
)
def trade_test_artifacts(limit: int = 100) -> dict[str, object]:
    return trade_repository.suspected_test_artifacts(limit=limit)


@app.post(
    "/trades/test-artifacts/quarantine",
    tags=["04 Orders"],
    summary="Quarantine suspected synthetic test rows in trades",
    dependencies=PROTECTED_ROUTE,
)
def quarantine_trade_test_artifacts(
    payload: dict[str, object] | None = Body(default=None),
) -> dict[str, object]:
    payload = payload or {}
    return trade_repository.quarantine_suspected_test_artifacts(
        limit=int(payload.get("limit") or 100),
        dry_run=bool(payload.get("dry_run", True)),
    )


@app.post(
    "/trades/{trade_id}/close",
    tags=["04 Orders"],
    summary="Manually close a lifecycle trade",
    dependencies=PROTECTED_ROUTE,
)
def close_trade(
    trade_id: int,
    payload: dict[str, object] = Body(
        examples=[
            {"outcome": "target_1", "exit_price": 115, "notes": "Manual paper exit"}
        ]
    ),
) -> dict[str, object]:
    outcome = str(payload.get("outcome") or "")
    exit_price = payload.get("exit_price")
    if not outcome:
        raise HTTPException(status_code=400, detail="outcome is required")
    if exit_price is None:
        raise HTTPException(status_code=400, detail="exit_price is required")
    try:
        record = trade_repository.close_trade(
            trade_id,
            outcome=outcome,
            exit_price=float(exit_price),
            notes=str(payload.get("notes") or ""),
        )
        return {"trade": trade_record_to_dict(record)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/trades/sync",
    tags=["04 Orders"],
    summary="Sync open live trades with Kite order status",
    dependencies=PROTECTED_ROUTE,
)
def sync_live_trades(
    payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100}]),
) -> dict[str, object]:
    payload = payload or {}
    try:
        return broker_sync_service.sync_open_trades(
            limit=_bounded_int(payload.get("limit"), 100, maximum=500)
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/broker/reconcile",
    tags=["04 Orders"],
    summary="Run broker/local live position reconciliation",
    dependencies=PROTECTED_ROUTE,
)
def reconcile_broker_positions() -> dict[str, object]:
    try:
        return broker_sync_service.reconcile_startup_positions()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/broker/reconciliation/status",
    tags=["04 Orders"],
    summary="Inspect broker/local reconciliation live-trading block",
)
def broker_reconciliation_status() -> dict[str, object]:
    return broker_sync_service.live_block_status()


@app.get(
    "/broker/emergency-protection/status",
    tags=["04 Orders"],
    summary="Inspect broker-side emergency SL/GTT capability",
)
def broker_emergency_protection_status() -> dict[str, object]:
    return {
        "enabled": settings.enable_broker_emergency_sl,
        "supported": True,
        "mechanism": "regular Kite SL-M protective SELL order after live entry fill confirmation",
        "scope": "live BUY option trades only",
        "disabled_by_default": not settings.enable_broker_emergency_sl,
        "fallback": "software exits via TradeExitService, active WebSocket/polling price checks, startup broker reconciliation, and exit-failure alerts",
    }


@app.post(
    "/trades/evaluate-exits",
    tags=["04 Orders"],
    summary="Auto square-off open trades at target or stop",
    dependencies=PROTECTED_ROUTE,
)
def evaluate_trade_exits(
    payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100}]),
) -> dict[str, object]:
    payload = payload or {}
    try:
        return trade_exit_service.evaluate_once(
            limit=_bounded_int(payload.get("limit"), 100, maximum=500)
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/alerts/test",
    tags=["01 System"],
    summary="Send a test Telegram alert if configured",
    dependencies=PROTECTED_ROUTE,
)
def send_test_alert(
    payload: dict[str, object] | None = Body(
        default=None, examples=[{"message": "AI Option Trader test alert"}]
    ),
) -> dict[str, object]:
    payload = payload or {}
    return notification_service.send(
        str(payload.get("message") or "AI Option Trader test alert"),
        kind="test",
    )


@app.get(
    "/alerts/status",
    tags=["01 System"],
    summary="Inspect Telegram notification configuration and last delivery attempt",
)
def alert_delivery_status() -> dict[str, object]:
    return notification_service.status()


@app.post(
    "/auto-trader/start",
    tags=["05 Auto Trader"],
    summary="Start continuous opportunity scanning",
    description="Starts scanner loop. By default it also starts outcome monitoring so saved opportunities are studied.",
    dependencies=PROTECTED_ROUTE,
)
async def start_auto_trader(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "order_mode": "paper",
                "side": "BUY",
                "symbols": "BANKNIFTY",
                "interval_seconds": 30,
                "limit": 5,
                "monitor_outcomes": True,
                "outcome_interval_seconds": 30,
            },
            {
                "order_mode": "live",
                "side": "BUY",
                "symbols": "BANKNIFTY",
                "interval_seconds": 30,
                "limit": 1,
                "monitor_outcomes": True,
            },
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    symbols_value = payload.get("symbols")
    symbols = None
    if isinstance(symbols_value, str):
        symbols = [
            item.strip().upper() for item in symbols_value.split(",") if item.strip()
        ]
    elif isinstance(symbols_value, list):
        symbols = [
            str(item).strip().upper() for item in symbols_value if str(item).strip()
        ]

    order_mode = str(
        payload.get("order_mode") or settings.default_order_mode or "paper"
    ).lower()
    status = auto_trader_service.start(
        side=str(payload.get("side") or "BUY"),
        symbols=symbols,
        interval_seconds=int(
            payload.get("interval_seconds") or settings.scanner_interval_seconds
        ),
        limit=int(payload.get("limit") or 5),
        place_orders=bool(payload.get("place_orders", True)),
        confirm_live=bool(payload.get("confirm_live", order_mode == "live")),
        order_mode=order_mode,
    )
    if bool(payload.get("monitor_outcomes", True)):
        opportunity_outcome_service.start(
            interval_seconds=int(payload.get("outcome_interval_seconds") or 30)
        )
    return {
        "auto_trader": status,
        "opportunity_monitor": opportunity_outcome_service.status(),
    }


@app.post(
    "/auto-trader/stop",
    tags=["05 Auto Trader"],
    summary="Stop continuous scanning",
    dependencies=PROTECTED_ROUTE,
)
async def stop_auto_trader() -> dict[str, object]:
    return await auto_trader_service.stop()


@app.get(
    "/auto-trader/status",
    tags=["05 Auto Trader"],
    summary="Get continuous scanner status",
)
def get_auto_trader_status() -> dict[str, object]:
    return auto_trader_service.status()


@app.get(
    "/auto-trader/latest",
    tags=["05 Auto Trader"],
    summary="Get latest auto-trader opportunities",
)
def get_auto_trader_latest() -> dict[str, object]:
    return {
        "status": auto_trader_service.status(),
        "opportunities": auto_trader_service.latest_opportunities,
    }


@app.get(
    "/auto-trader/executions",
    tags=["05 Auto Trader"],
    summary="Get auto-trader order execution history",
)
def get_auto_trader_executions() -> dict[str, object]:
    return {
        "count": len(auto_trader_service.executions),
        "executions": auto_trader_service.executions,
        "errors": auto_trader_service.errors,
    }


@app.get(
    "/dashboard/decision-feed",
    tags=["01 System"],
    summary="Show recent scanner thoughts, rejected setups, accepted opportunities, and trade events",
)
def get_dashboard_decision_feed(limit: int = 30) -> dict[str, object]:
    limit = max(5, min(int(limit or 30), 100))
    events: list[dict[str, object]] = []
    for record in opportunity_repository.list_opportunities(limit=limit):
        events.append(_decision_event_from_opportunity(record))
    for record in rejected_opportunity_repository.list_rejections(
        symbol="BANKNIFTY", limit=limit
    ):
        events.append(_decision_event_from_rejection(record))
    for record in trade_repository.list_trades(limit=limit):
        events.append(_decision_event_from_trade(record))
    for item in auto_trader_service.executions[-limit:]:
        events.append(_decision_event_from_execution(item))
    for item in auto_trader_service.recent_decision_events(limit=limit):
        events.append(_decision_event_from_auto_decision(item))
    if auto_trader_service.last_scan_result:
        events.append(
            _decision_event_from_scan_heartbeat(auto_trader_service.last_scan_result)
        )
    for item in auto_trader_service.errors[-limit:]:
        events.append(_decision_event_from_error(item))
    events = sorted(
        events,
        key=lambda item: _decision_sort_timestamp(item.get("sort_time")),
        reverse=True,
    )[:limit]
    for item in events:
        item.pop("sort_time", None)
    return {"status": "ok", "count": len(events), "events": events}


@app.get(
    "/opportunities",
    tags=["06 Opportunity Journal"],
    summary="List saved scanner opportunities",
    description="Use `status=open` to see opportunities still being monitored, or omit status to see recent records.",
)
def list_saved_opportunities(
    status: str | None = None, limit: int = 50
) -> dict[str, object]:
    records = opportunity_repository.list_opportunities(status=status, limit=limit)
    return {
        "count": len(records),
        "opportunities": [opportunity_record_to_dict(record) for record in records],
    }


@app.post(
    "/opportunities/{opportunity_id}/outcome",
    tags=["06 Opportunity Journal"],
    summary="Manually mark an opportunity outcome",
    description="Use this when you know a signal hit stop, target, expired, or was a false signal before the monitor catches it.",
)
def update_opportunity_outcome(
    opportunity_id: int,
    payload: dict[str, object] = Body(
        examples=[
            {
                "outcome": "stop_loss",
                "exit_price": 1.87,
                "review_notes": "Expiry-day low-premium PE failed after false breakdown",
            },
            {
                "outcome": "target_1",
                "exit_price": 3.24,
                "review_notes": "First target achieved cleanly",
            },
        ]
    ),
) -> dict[str, object]:
    outcome = str(payload.get("outcome") or "")
    if not outcome:
        raise HTTPException(status_code=400, detail="outcome is required")
    exit_price_value = payload.get("exit_price")
    exit_price = float(exit_price_value) if exit_price_value is not None else None
    review_notes = payload.get("review_notes")
    try:
        record = opportunity_repository.update_outcome(
            opportunity_id,
            outcome=outcome,
            exit_price=exit_price,
            review_notes=str(review_notes) if review_notes is not None else None,
        )
        return {"opportunity": opportunity_record_to_dict(record)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/opportunities/performance",
    tags=["06 Opportunity Journal"],
    summary="Summarize opportunity win/loss performance",
)
def get_opportunity_performance() -> dict[str, object]:
    return opportunity_repository.summarize_performance()


@app.get(
    "/opportunities/failure-analysis",
    tags=["06 Opportunity Journal"],
    summary="Aggregate repeated failure reasons",
    description="Shows tags such as low premium noise, expiry-day risk, weak confirmation, and spread/slippage drag.",
)
def get_opportunity_failure_analysis() -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market("opportunity_failure_analysis"):
        return deferred
    return opportunity_repository.failure_analysis()


@app.get(
    "/opportunities/rejections",
    tags=["06 Opportunity Journal"],
    summary="Analyze rejected scanner setups",
    description="Shows hard-gate rejection reasons and any later manually/evaluated outcome for rejected setups.",
)
def get_rejected_opportunities(
    symbol: str | None = "BANKNIFTY",
    limit: int = 1000,
    learning_eligible: bool | None = None,
) -> dict[str, object]:
    if deferred := _defer_review_analysis_during_market(
        "rejected_opportunity_analysis"
    ):
        return deferred
    return rejected_opportunity_repository.analyze(
        symbol=symbol, limit=limit, learning_eligible=learning_eligible
    )


@app.post(
    "/opportunities/rejections/{rejection_id}/outcome",
    tags=["06 Opportunity Journal"],
    summary="Mark later outcome for a rejected setup",
)
def update_rejected_opportunity_outcome(
    rejection_id: int,
    payload: dict[str, object] = Body(
        examples=[
            {
                "outcome": "would_have_hit_target",
                "exit_price": 250.0,
                "notes": "Rejected setup later moved well",
            }
        ]
    ),
) -> dict[str, object]:
    outcome = str(payload.get("outcome") or "")
    if not outcome:
        raise HTTPException(status_code=400, detail="outcome is required")
    exit_price = (
        float(payload["exit_price"]) if payload.get("exit_price") is not None else None
    )
    try:
        record = rejected_opportunity_repository.mark_later_outcome(
            rejection_id,
            outcome=outcome,
            exit_price=exit_price,
            notes=str(payload.get("notes") or ""),
            outcome_source=str(payload.get("outcome_source") or "manual"),
            outcome_timeframe=str(payload.get("outcome_timeframe") or "") or None,
            outcome_minutes=float(payload["outcome_minutes"])
            if payload.get("outcome_minutes") is not None
            else None,
            ambiguous=bool(payload.get("ambiguous", False)),
        )
        return {"rejection": rejected_opportunity_repository.to_dict(record)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/opportunities/evaluate-open",
    tags=["07 Outcome Monitor"],
    summary="Evaluate open accepted and rejected opportunities once",
    description="Fetches current option prices and auto-marks accepted opportunity outcomes plus later outcomes for rejected setups where possible.",
)
def evaluate_open_opportunities(
    payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100}]),
) -> dict[str, object]:
    payload = payload or {}
    return opportunity_outcome_service.evaluate_once(
        limit=int(payload.get("limit") or 100),
        exhaust_rejected=bool(payload.get("exhaust_rejected", False)),
    )


@app.post(
    "/opportunities/rejections/evaluate-open",
    tags=["07 Outcome Monitor"],
    summary="Evaluate rejected setups for later outcomes",
    description="Checks rejected scanner setups against current option prices and stores later_outcome when target/stop/expiry can be inferred.",
)
def evaluate_rejected_opportunities(
    payload: dict[str, object] | None = Body(
        default=None, examples=[{"limit": 100, "symbol": "BANKNIFTY", "exhaust": True}]
    ),
) -> dict[str, object]:
    payload = payload or {}
    symbol_value = payload.get("symbol", "BANKNIFTY")
    symbol = str(symbol_value) if symbol_value is not None else None
    if bool(payload.get("exhaust", payload.get("all", False))):
        return rejected_opportunity_outcome_service.evaluate_batches(
            symbol=symbol,
            batch_limit=int(
                payload.get("limit") or settings.rejected_outcome_batch_limit
            ),
            max_batches=int(
                payload.get("max_batches") or settings.rejected_outcome_max_batches
            ),
            learning_only=bool(payload.get("learning_only", True)),
            delay_seconds=float(
                payload.get(
                    "delay_seconds", settings.rejected_outcome_batch_delay_seconds
                )
            ),
        )
    return rejected_opportunity_outcome_service.evaluate_once(
        symbol=symbol,
        limit=int(payload.get("limit") or 100),
        learning_only=bool(payload.get("learning_only", True)),
    )


@app.post(
    "/opportunity-monitor/start",
    tags=["07 Outcome Monitor"],
    summary="Start automatic outcome monitoring",
    description="Usually started automatically by `/auto-trader/start` when `monitor_outcomes=true`.",
)
async def start_opportunity_monitor(
    payload: dict[str, object] | None = Body(
        default=None, examples=[{"interval_seconds": 30}]
    ),
) -> dict[str, object]:
    payload = payload or {}
    return opportunity_outcome_service.start(
        interval_seconds=int(payload.get("interval_seconds") or 30)
    )


@app.post(
    "/opportunity-monitor/stop",
    tags=["07 Outcome Monitor"],
    summary="Stop automatic outcome monitoring",
)
async def stop_opportunity_monitor() -> dict[str, object]:
    return await opportunity_outcome_service.stop()


@app.get(
    "/opportunity-monitor/status",
    tags=["07 Outcome Monitor"],
    summary="Get outcome monitor status",
)
def get_opportunity_monitor_status() -> dict[str, object]:
    return opportunity_outcome_service.status()


@app.post(
    "/auto-trader/scan-once",
    tags=["05 Auto Trader"],
    summary="Run one scanner cycle using auto-trader config",
    description="Useful for testing auto-trader settings without starting the continuous loop.",
)
def scan_once_auto_trader(
    payload: dict[str, object] | None = Body(
        default=None,
        examples=[
            {
                "side": "BUY",
                "symbols": "BANKNIFTY",
                "limit": 3,
                "order_mode": "paper",
            }
        ],
    ),
) -> dict[str, object]:
    payload = payload or {}
    if not auto_trader_service.config:
        symbols_value = payload.get("symbols")
        symbols = None
        if isinstance(symbols_value, str):
            symbols = [
                item.strip().upper()
                for item in symbols_value.split(",")
                if item.strip()
            ]
        elif isinstance(symbols_value, list):
            symbols = [
                str(item).strip().upper() for item in symbols_value if str(item).strip()
            ]
        auto_trader_service.config = {
            "side": str(payload.get("side") or "BUY").upper(),
            "symbols": symbols,
            "interval_seconds": int(
                payload.get("interval_seconds") or settings.scanner_interval_seconds
            ),
            "limit": int(payload.get("limit") or 5),
            "place_orders": bool(payload.get("place_orders", False)),
            "confirm_live": False,
            "order_mode": str(
                payload.get("order_mode") or settings.default_order_mode or "paper"
            ).lower(),
        }
    return auto_trader_service.scan_once()


@app.get(
    "/paper/summary", tags=["08 Paper Trading"], summary="Get paper-trading summary"
)
def get_paper_summary() -> dict[str, object]:
    return paper_trading_service.get_summary()


@app.get(
    "/paper/positions", tags=["08 Paper Trading"], summary="List open paper positions"
)
def get_paper_positions() -> dict[str, object]:
    return {
        "count": len(paper_trading_service.positions),
        "positions": paper_trading_service.positions,
    }


@app.get(
    "/paper/trades",
    tags=["08 Paper Trading"],
    summary="List open and closed paper trades",
)
def get_paper_trades() -> dict[str, object]:
    return {
        "open_positions": paper_trading_service.positions,
        "closed_trades": paper_trading_service.closed_trades,
        "summary": paper_trading_service.get_summary(),
    }


@app.post(
    "/paper/close",
    tags=["08 Paper Trading"],
    summary="Close a paper position",
    description="Closes an in-memory paper position and records simulated P&L.",
)
def close_paper_position(
    payload: dict[str, object] = Body(
        examples=[{"symbol": "NIFTY24JUN22000CE", "exit_price": 115}]
    ),
) -> dict[str, object]:
    symbol = str(payload.get("symbol") or "")
    exit_price = payload.get("exit_price")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if exit_price is None:
        raise HTTPException(status_code=400, detail="exit_price is required")
    try:
        trade = paper_trading_service.close_trade(
            symbol=symbol, exit_price=float(exit_price)
        )
        return {
            "status": "closed",
            "trade": trade,
            "summary": paper_trading_service.get_summary(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/kite/login", tags=["02 Kite Login"], summary="Return Kite login URL")
def kite_login() -> dict[str, str]:
    try:
        kite_provider = get_kite_provider()
        url = kite_provider.generate_login_url()
        return {"login_url": url}
    except Exception as exc:
        return {"error": str(exc)}


@app.get(
    "/kite/auth",
    tags=["02 Kite Login"],
    summary="Redirect browser to Kite login",
    description="Open this in the browser first. Kite redirects back to `/kite/callback` or `/login` with a request token.",
)
def kite_auth() -> RedirectResponse:
    """Redirect the browser to the Kite Connect login URL."""
    try:
        kite_provider = get_kite_provider()
        url = kite_provider.generate_login_url()
        return RedirectResponse(url)
    except Exception as exc:
        return RedirectResponse(f"/kite/health?error={exc}")


@app.get("/kite/health", tags=["02 Kite Login"], summary="Verify Kite profile access")
def kite_health() -> dict[str, object]:
    try:
        profile = _cached_dashboard_broker_call(
            "kite_health", lambda: get_kite_provider().profile()
        )
        return {**profile, "auth": kite_auth_state.status()}
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "auth": kite_auth_state.status(),
        }


@app.get(
    "/kite/websocket/status",
    tags=["02 Kite Login"],
    summary="Inspect active Kite WebSocket price feed",
)
def kite_websocket_status() -> dict[str, object]:
    status = application_context.market_data_runtime_service.status()
    status["kite_auth"] = kite_auth_state.status()
    if armed_entry_tracker_service is not None:
        status.update(armed_entry_tracker_service.status())
        status["armed_entry"] = armed_entry_tracker_service.list_entries()
    status["banknifty_fast_rally"] = banknifty_fast_rally_service.status()
    return status


@app.get("/kite/margins", tags=["02 Kite Login"], summary="Get Zerodha margins/funds")
def kite_margins() -> dict[str, object]:
    try:
        return _cached_dashboard_broker_call(
            "kite_margins", lambda: get_kite_provider().margins()
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/positions", tags=["02 Kite Login"], summary="Get Zerodha positions")
def kite_positions() -> dict[str, object]:
    try:
        return _cached_dashboard_broker_call(
            "kite_positions", lambda: get_kite_provider().positions()
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _cached_dashboard_broker_call(
    cache_key: str, fetcher: Callable[[], dict[str, object]]
) -> dict[str, object]:
    now = ist_now_naive()
    ttl = max(1, int(settings.dashboard_broker_cache_ttl_seconds))
    cached = dashboard_broker_cache.get(cache_key)
    if cached is not None:
        cached_at, payload = cached
        if (now - cached_at).total_seconds() <= ttl:
            return {
                **payload,
                "cached": True,
                "cached_at": cached_at.isoformat(sep=" "),
            }
    payload = fetcher()
    dashboard_broker_cache[cache_key] = (now, payload)
    return {**payload, "cached": False, "cached_at": now.isoformat(sep=" ")}


@app.get(
    "/kite/callback",
    response_class=HTMLResponse,
    tags=["02 Kite Login"],
    summary="Handle Kite redirect callback",
)
@app.get(
    "/login",
    response_class=HTMLResponse,
    tags=["02 Kite Login"],
    summary="Handle alternate Kite redirect callback",
)
def kite_callback(request_token: str | None = None, error: str | None = None):
    """Handle Kite redirect: exchange request_token for access token and show a simple page."""
    if error:
        return HTMLResponse(f"<h3>Kite login error: {error}</h3>")
    if not request_token:
        return HTMLResponse("<h3>No request_token provided in callback.</h3>")

    try:
        kite_provider = get_kite_provider()
        data = kite_provider.generate_session(request_token)
        access_token = data.get("access_token")
        if access_token:
            save_access_token(access_token)
            application_context.market_data_runtime_service.refresh_credentials(
                access_token=access_token
            )
            html = f'<h3>Login successful</h3><p>Access token saved.</p><p><a href="/">Back to app</a></p>'
        else:
            html = f"<h3>Login completed but no access token returned.</h3><pre>{data}</pre>"
        return HTMLResponse(html)
    except Exception as exc:
        return HTMLResponse(f"<h3>Error exchanging token: {exc}</h3>")


@app.post(
    "/kite/session",
    tags=["02 Kite Login"],
    summary="Exchange request_token for access token",
    description="Use this only if you are manually copying the Kite `request_token` instead of using browser redirect.",
)
def kite_session(
    payload: dict[str, str] = Body(
        examples=[{"request_token": "paste_request_token_here"}]
    ),
) -> dict[str, object]:
    """Exchange a Kite request_token for an access token and persist it locally."""
    request_token = payload.get("request_token")
    if not request_token:
        raise HTTPException(status_code=400, detail="request_token is required")

    try:
        kite_provider = get_kite_provider()
        data = kite_provider.generate_session(request_token)
        access_token = data.get("access_token")
        if access_token:
            save_access_token(access_token)
            application_context.market_data_runtime_service.refresh_credentials(
                access_token=access_token
            )
        profile = kite_provider.profile()
        return {
            "status": "ok" if access_token else "missing-access-token",
            "saved": bool(access_token),
            "profile": profile,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
