import json
from dataclasses import asdict
from datetime import datetime

from fastapi import Body, FastAPI, HTTPException
from sqlalchemy import text

from app.config import settings
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
from app.services.rejected_opportunity_outcome_service import RejectedOpportunityOutcomeService
from app.services.broker_sync_service import BrokerSyncService
from app.services.backtest_service import BacktestService
from app.services.data_ingestion_service import DataIngestionService
from app.services.day_type_service import DayTypeService
from app.services.database import get_session
from app.services.greeks_service import GreeksService
from app.services.notification_service import NotificationService
from app.services.execution_analytics_service import ExecutionAnalyticsService
from app.services.active_price_feed import ActiveTradePriceFeed
from app.services.banknifty_option_prewarm_service import BankNiftyOptionPrewarmService
from app.services.kite_websocket_price_feed import KiteWebSocketPriceFeed
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.opportunity_analytics_service import OpportunityAnalyticsService
from app.services.option_history_repository import OptionHistoryRepository
from app.services.option_premium_confirmation_service import OptionPremiumConfirmationService
from app.services.option_quality_service import OptionQualityService
from app.services.option_snapshot_collector_service import OptionSnapshotCollectorService
from app.services.outcome_learning_service import OutcomeLearningService
from app.services.professional_readiness_service import ProfessionalReadinessService
from app.services.professional_insights_service import ProfessionalInsightsService
from app.services.risk_management_service import RiskManagementService
from app.services.strategy_edge_service import StrategyEdgeService
from app.services.strategy_validation_repository import StrategyValidationRepository
from app.services.time_bucket_edge_service import TimeBucketEdgeService
from app.services.trade_exit_service import TradeExitService
from app.services.trade_repository import TradeRepository
from app.services.trade_setup_service import OptionContract
from app.services.time_utils import format_ist
from app.providers.kite_provider import KiteProvider
from app.providers.kite_feed import KiteFeed
from app.providers.token_store import save_access_token, load_access_token
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
7. Research quality: `GET /research/professional-readiness`, `GET /research/opportunity-analytics`, `GET /research/execution-analytics`, `GET /research/outcome-learning`, `POST /research/backtest/options`, `POST /research/walk-forward`
8. Watch saved opportunities: `GET /opportunities`, `GET /opportunities/performance`
9. Study failures: `POST /opportunities/evaluate-open`, `GET /opportunities/failure-analysis`
10. Live orders only after validation: set `LIVE_TRADING_MODE=true`, `PAPER_TRADING_MODE=false`, `AUTOMATION_PLACE_ORDERS=true`, and `AUTOMATION_CONFIRM_LIVE=true`

Safety note: signals are probability-ranked trade setups, not guaranteed-profit trades.
"""

OPENAPI_TAGS = [
    {"name": "01 System", "description": "Start here: app health, DB health, and workflow overview."},
    {"name": "02 Kite Login", "description": "Authenticate with Zerodha Kite and verify account connectivity."},
    {"name": "03 Scanner", "description": "Find and diagnose option opportunities. Emitted opportunities are saved to DB."},
    {"name": "04 Orders", "description": "Place paper orders by default; live orders require explicit live config and confirmation."},
    {"name": "05 Auto Trader", "description": "Continuously scan for opportunities and optionally place orders."},
    {"name": "06 Opportunity Journal", "description": "Review saved opportunities, mark outcomes, and study failures."},
    {"name": "07 Outcome Monitor", "description": "Automatically evaluate open opportunities against stop/target prices."},
    {"name": "08 Paper Trading", "description": "Inspect and close simulated in-memory paper trades."},
    {"name": "09 Market Data", "description": "Read stored market summaries."},
    {"name": "10 Research", "description": "Market insight filters, Greeks, IV, option-quality filters, and historical rule replay."},
    {"name": "11 Data Ingestion", "description": "Pull historical candles and option-chain snapshots into MySQL."},
    {"name": "12 Automation", "description": "One-switch supervisor for daily ingestion, collectors, scanners, and monitors."},
]

app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
)
market_data_service = MarketDataService()
signal_repository = SignalRepository()
paper_trading_service = PaperTradingService()
opportunity_repository = OpportunityRepository()
rejected_opportunity_repository = RejectedOpportunityRepository()
trade_repository = TradeRepository()
risk_management_service = RiskManagementService(trade_repository)
notification_service = NotificationService()
greeks_service = GreeksService()
option_quality_service = OptionQualityService(greeks_service)
backtest_service = BacktestService()
option_history_repository = OptionHistoryRepository()
shared_kite_feed = KiteFeed() if settings.use_kite_market_data else None
kite_websocket_price_feed = KiteWebSocketPriceFeed()
active_trade_price_feed = ActiveTradePriceFeed(kite_websocket_price_feed)
banknifty_option_prewarm_service = BankNiftyOptionPrewarmService(kite_websocket_price_feed)
strategy_validation_repository = StrategyValidationRepository()
strategy_edge_service = StrategyEdgeService(backtest_service=backtest_service, repository=strategy_validation_repository)
day_type_service = DayTypeService()
option_premium_confirmation_service = OptionPremiumConfirmationService()
time_bucket_edge_service = TimeBucketEdgeService(backtest_service=backtest_service)
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


def get_kite_provider() -> KiteProvider:
    provider = KiteProvider()
    existing_token = load_access_token()
    if existing_token:
        provider.set_access_token(existing_token)
    return provider


market_data_coordinator = MarketDataCoordinator(get_kite_provider)
active_trade_price_feed.market_data_coordinator = market_data_coordinator
rejected_opportunity_outcome_service = RejectedOpportunityOutcomeService(
    rejected_opportunity_repository,
    kite_provider_factory=get_kite_provider,
    market_data_coordinator=market_data_coordinator,
)


data_ingestion_service = DataIngestionService(
    kite_provider_factory=get_kite_provider,
    market_data_service=market_data_service,
    option_history_repository=option_history_repository,
    greeks_service=greeks_service,
    market_data_coordinator=market_data_coordinator,
)
option_snapshot_collector_service = OptionSnapshotCollectorService(data_ingestion_service)


def get_scanner_service() -> ScannerService:
    return ScannerService(
        strategy_edge_service=strategy_edge_service,
        feed=shared_kite_feed,
        option_premium_confirmation_service=OptionPremiumConfirmationService(kite_websocket_price_feed),
        rejected_opportunity_repository=rejected_opportunity_repository,
        banknifty_option_prewarm_service=banknifty_option_prewarm_service,
    )


def get_order_service() -> OrderService:
    return OrderService(
        kite_provider=get_kite_provider(),
        paper_trading_service=paper_trading_service,
        trade_repository=trade_repository,
        risk_management_service=risk_management_service,
        active_price_feed=active_trade_price_feed,
        live_safety_checker=broker_sync_service.live_block_status,
        market_data_coordinator=market_data_coordinator,
    )


auto_trader_service = AutoTraderService(
    scanner_factory=get_scanner_service,
    order_service_factory=get_order_service,
    opportunity_repository=opportunity_repository,
    risk_management_service=risk_management_service,
    notification_service=notification_service,
)
trade_exit_service = TradeExitService(
    trade_repository=trade_repository,
    kite_provider_factory=get_kite_provider,
    paper_trading_service=paper_trading_service,
    active_price_feed=active_trade_price_feed,
    notification_service=notification_service,
    market_data_coordinator=market_data_coordinator,
)
opportunity_outcome_service = OpportunityOutcomeService(
    repository=opportunity_repository,
    kite_provider_factory=get_kite_provider,
    trade_exit_service=trade_exit_service,
    rejected_outcome_service=rejected_opportunity_outcome_service,
    market_data_coordinator=market_data_coordinator,
)
broker_sync_service = BrokerSyncService(
    trade_repository=trade_repository,
    kite_provider_factory=get_kite_provider,
    notification_service=notification_service,
)
broker_sync_service.set_exit_confirmation_callback(trade_exit_service.confirm_live_exit_for_trade)
kite_websocket_price_feed.order_update_handler = broker_sync_service.handle_order_postback
automation_supervisor_service = AutomationSupervisorService(
    data_ingestion_service=data_ingestion_service,
    snapshot_collector_service=option_snapshot_collector_service,
    auto_trader_service=auto_trader_service,
    outcome_service=opportunity_outcome_service,
    risk_management_service=risk_management_service,
    notification_service=notification_service,
)


@app.on_event("startup")
async def startup_automation() -> None:
    if settings.enable_kite_websocket:
        active_trade_price_feed.start()
    try:
        broker_sync_service.sync_open_trades(limit=100)
        broker_sync_service.reconcile_startup_positions()
    except Exception:
        pass
    if settings.automation_enabled:
        automation_supervisor_service.start()


@app.on_event("shutdown")
async def shutdown_background_services() -> None:
    active_trade_price_feed.stop()
    await automation_supervisor_service.stop()
    await option_snapshot_collector_service.stop()
    await auto_trader_service.stop()
    await opportunity_outcome_service.stop()


@app.get("/health", tags=["01 System"], summary="Check API health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


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


@app.get("/market-data/cache/status", tags=["09 Market Data"], summary="Inspect shared market-data quote cache")
def market_data_cache_status() -> dict[str, object]:
    return market_data_coordinator.status()


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


@app.get("/dashboard", response_class=HTMLResponse, tags=["01 System"], summary="Open command center dashboard")
def command_center_dashboard() -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Option Trader Command Center</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; --line:#d0d5dd; --muted:#667085; --ink:#101828; --good:#047857; --bad:#b42318; --warn:#b54708; --blue:#175cd3; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f4f6f8; color: var(--ink); }
    header { padding: 16px 24px; background: #ffffff; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; gap: 16px; align-items: center; position: sticky; top: 0; z-index: 5; }
    h1 { font-size: 22px; margin: 0; letter-spacing: 0; }
    h2 { font-size: 15px; margin: 0 0 12px; letter-spacing: 0; }
    main { padding: 16px 24px 32px; display: grid; gap: 14px; }
    .topbar { display: grid; grid-template-columns: minmax(320px, 1fr) minmax(280px, .55fr); gap: 14px; align-items: stretch; }
    .control-grid { display: grid; grid-template-columns: minmax(340px, 420px) minmax(0, 1fr); gap: 14px; align-items: start; }
    .data-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; align-items: start; }
    .panel { background: #ffffff; border: 1px solid var(--line); border-radius: 8px; padding: 14px; min-width: 0; overflow: hidden; }
    .full { grid-column: 1 / -1; }
    .ready { min-height: 156px; border-left: 6px solid var(--warn); display: grid; gap: 12px; }
    .ready.ok { border-left-color: var(--good); }
    .ready.bad { border-left-color: var(--bad); }
    .ready-title { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
    .ready-title strong { font-size: 24px; line-height: 1.1; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .chip { border: 1px solid #e4e7ec; border-radius: 999px; padding: 5px 9px; font-size: 12px; color: #344054; background: #f9fafb; max-width: 100%; overflow-wrap: anywhere; }
    .chip.ok { border-color: #a7f3d0; background: #ecfdf3; color: var(--good); }
    .chip.bad { border-color: #fecaca; background: #fff1f3; color: var(--bad); }
    .chip.warn { border-color: #fedf89; background: #fffaeb; color: var(--warn); }
    .summary-line { color: #344054; font-size: 14px; overflow-wrap: anywhere; }
    .watch { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .watch-item { border: 1px solid #eaecf0; border-radius: 8px; padding: 10px; min-height: 72px; background: #fcfcfd; }
    .watch-item span { color: var(--muted); font-size: 12px; display: block; }
    .watch-item strong { display: block; margin-top: 5px; font-size: 16px; overflow-wrap: anywhere; }
    .status { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 8px; }
    .pill { border: 1px solid #e4e7ec; border-radius: 8px; padding: 9px; background: #fcfcfd; min-height: 58px; }
    .pill.ok { border-color: #a7f3d0; background: #ecfdf3; color: var(--good); }
    .pill.bad { border-color: #fecaca; background: #fff1f3; color: var(--bad); }
    .pill.warn { border-color: #fedf89; background: #fffaeb; color: var(--warn); }
    .pill strong { display: block; font-size: 12px; } .pill span { display: block; font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; color: #475467; }
    label { display: block; font-size: 12px; color: #475467; margin: 10px 0 4px; }
    input, select { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px; font: inherit; background: #fff; min-height: 38px; }
    .row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
    button, .linkbtn { border: 1px solid var(--blue); background: var(--blue); color: white; padding: 9px 10px; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; text-align: center; display: inline-block; min-height: 38px; }
    button.secondary, .linkbtn.secondary { background: #fff; color: #344054; border-color: var(--line); }
    button.danger { background: var(--bad); border-color: var(--bad); }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .actions button { flex: 1 1 130px; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .metric { border: 1px solid #eaecf0; border-radius: 8px; padding: 10px; background: #fcfcfd; min-height: 70px; }
    .metric span { display: block; color: #667085; font-size: 12px; } .metric strong { font-size: 18px; display: block; margin-top: 4px; overflow-wrap: anywhere; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
    th, td { border-bottom: 1px solid #eaecf0; padding: 8px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
    th { color: #475467; background: #f9fafb; font-weight: 600; position: sticky; top: 0; }
    .tablewrap { min-height: 160px; max-height: 360px; overflow: auto; border: 1px solid #eaecf0; border-radius: 8px; }
    pre { max-height: 300px; overflow: auto; background: #101828; color: #f9fafb; border-radius: 8px; padding: 10px; font-size: 12px; }
    .links { display: grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); gap: 8px; }
    .muted { color: var(--muted); font-size: 12px; }
    .raw-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; }
    .empty { color: var(--muted); padding: 14px; }
    .feed { display: grid; gap: 8px; max-height: 430px; overflow: auto; padding-right: 4px; }
    .feed-item { border: 1px solid #eaecf0; border-radius: 8px; padding: 10px; background: #fcfcfd; display: grid; gap: 5px; }
    .feed-item.ok { border-left: 4px solid var(--good); }
    .feed-item.bad { border-left: 4px solid var(--bad); }
    .feed-item.warn { border-left: 4px solid var(--warn); }
    .feed-item.watch { border-left: 4px solid var(--blue); }
    .feed-head { display: flex; justify-content: space-between; gap: 10px; align-items: start; }
    .feed-title { font-weight: 700; font-size: 13px; overflow-wrap: anywhere; }
    .feed-time { color: var(--muted); font-size: 11px; white-space: nowrap; }
    .feed-message { color: #344054; font-size: 13px; overflow-wrap: anywhere; }
    .feed-meta { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    details { margin-top: 12px; border-top: 1px solid #eaecf0; padding-top: 10px; }
    summary { cursor: pointer; font-weight: 600; color: #344054; }
    @media (max-width: 1180px) { .topbar,.control-grid,.data-grid,.raw-grid { grid-template-columns: 1fr; } .full { grid-column: auto; } .watch,.metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 680px) { main, header { padding-left: 12px; padding-right: 12px; } header { align-items: flex-start; flex-direction: column; } .status,.metrics,.links,.row,.watch { grid-template-columns: 1fr; } .actions { width: 100%; } }
  </style>
</head>
<body>
  <header>
    <div><h1>Bank Nifty Option Command Center</h1><div class="muted" id="refreshText">Loading...</div></div>
    <div class="actions">
      <a class="linkbtn secondary" href="/docs" target="_blank">Docs</a>
      <a class="linkbtn secondary" href="/kite/auth" target="_blank">Kite Login</a>
      <button class="secondary" onclick="refreshAll()">Refresh</button>
    </div>
  </header>
  <main>
    <section class="topbar">
      <div class="panel ready" id="readyPanel">
        <div class="ready-title">
          <div><strong id="readyTitle">Checking system</strong><div class="summary-line" id="readySummary">Loading live state...</div></div>
          <span class="chip" id="modeChip">Mode</span>
        </div>
        <div class="chips" id="readyChips"></div>
        <div class="watch" id="watchPanel"></div>
      </div>
      <div class="panel">
        <h2>System Health</h2>
        <div class="status" id="statusCards"></div>
      </div>
    </section>
    <section class="control-grid">
      <div class="panel">
        <h2>Automation</h2>
        <label>Order mode</label><select id="orderMode"><option value="paper">Paper orders</option><option value="live">Live orders</option></select>
        <div class="actions">
          <button onclick="startAutomation()">Start</button><button class="danger" onclick="stopAutomation()">Stop</button>
          <button class="secondary" onclick="scanOnce()">Scan Once</button><button class="secondary" onclick="evaluateOpen()">Evaluate Open</button>
        </div>
        <details>
          <summary>Advanced</summary>
          <div class="row"><div><label>Side</label><select id="side"><option>BUY</option><option>SELL</option></select></div><div><label>Limit</label><input id="limit" type="number" value="5" min="1" max="25"></div></div>
          <label>Symbols</label><input id="symbols" value="BANKNIFTY">
          <div class="row"><div><label>Scan seconds</label><input id="interval" type="number" value="30" min="3"></div><div><label>Outcome seconds</label><input id="outcomeInterval" type="number" value="30" min="10"></div></div>
        </details>
        <pre id="actionResult">{}</pre>
      </div>
      <div class="panel">
        <h2>Today</h2>
        <div class="metrics" id="metrics"></div>
        <details>
          <summary>Activity JSON</summary>
          <pre id="activityJson">{}</pre>
        </details>
      </div>
    </section>
    <section class="panel">
      <h2>Quick Modules</h2>
      <div class="links">
        <a class="linkbtn secondary" href="/scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3" target="_blank">Scanner</a>
        <a class="linkbtn secondary" href="/scanner/diagnostics?side=BUY&symbols=BANKNIFTY&limit=10" target="_blank">Diagnostics</a>
        <a class="linkbtn secondary" href="/opportunities?limit=20" target="_blank">Journal</a>
        <a class="linkbtn secondary" href="/opportunities/failure-analysis" target="_blank">Failures</a>
        <a class="linkbtn secondary" href="/paper/positions" target="_blank">Paper</a>
        <a class="linkbtn secondary" href="/trades?limit=20" target="_blank">Trades</a>
        <a class="linkbtn secondary" href="/risk/status" target="_blank">Risk</a>
        <a class="linkbtn secondary" href="/research/settings" target="_blank">Research</a>
        <a class="linkbtn secondary" href="/research/outcome-learning" target="_blank">Learning</a>
        <a class="linkbtn secondary" href="/research/professional-insights" target="_blank">Insights</a>
        <a class="linkbtn secondary" href="/research/daily-review" target="_blank">Daily Review</a>
        <a class="linkbtn secondary" href="/kite/websocket/status" target="_blank">WebSocket</a>
        <a class="linkbtn secondary" href="/market-data/cache/status" target="_blank">Data Cache</a>
        <a class="linkbtn secondary" href="/data/ingest/status" target="_blank">Ingestion</a>
        <a class="linkbtn secondary" href="/data/collector/status" target="_blank">Collector</a>
        <a class="linkbtn secondary" href="/automation/status" target="_blank">Automation</a>
        <a class="linkbtn secondary" href="/kite/margins" target="_blank">Margins</a>
        <a class="linkbtn secondary" href="/kite/positions" target="_blank">Positions</a>
        <a class="linkbtn secondary" href="/db/health" target="_blank">DB</a>
      </div>
    </section>
    <section class="data-grid">
      <div class="panel"><h2>Active Trade / Exit Watch</h2><div id="activeTrade"></div></div>
      <div class="panel"><h2>Latest Watch</h2><div id="latestWatch"></div></div>
      <div class="panel full"><h2>System Thought Feed</h2><div class="feed" id="decisionFeed"><div class="empty">Loading recent decisions...</div></div></div>
      <div class="panel"><h2>Latest Opportunities</h2><div class="tablewrap"><table id="latestTable"></table></div></div>
      <div class="panel"><h2>Opportunity Journal</h2><div class="tablewrap"><table id="journalTable"></table></div></div>
      <div class="panel full">
        <h2>Raw Details</h2>
        <div class="raw-grid">
          <details open><summary>Decision / Exit</summary><pre id="decisionJson">{}</pre></details>
          <details><summary>Orders / Paper</summary><pre id="ordersJson">{}</pre></details>
          <details><summary>Learning / Failures</summary><pre id="learningJson">{}</pre><pre id="failureJson">{}</pre></details>
          <details><summary>Kite Account</summary><pre id="accountJson">{}</pre></details>
        </div>
      </div>
    </section>
  </main>
<script>
async function getJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return await res.json();
}
async function postJson(path, body={}) {
  const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return await res.json();
}
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function pill(label, state, detail) {
  return `<div class="pill ${state}"><strong>${esc(label)}</strong><span>${esc(detail || "")}</span></div>`;
}
function chip(label, state="") {
  return `<span class="chip ${state}">${esc(label)}</span>`;
}
function metric(label, value) {
  return `<div class="metric"><span>${esc(label)}</span><strong>${esc(value ?? "-")}</strong></div>`;
}
function table(el, rows, keys) {
  if (!rows || !rows.length) { el.innerHTML = "<tr><td>No rows yet.</td></tr>"; return; }
  el.innerHTML = `<thead><tr>${keys.map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => `<tr>${keys.map(k => `<td>${esc(r[k])}</td>`).join("")}</tr>`).join("") + "</tbody>";
}
function latestSignal(latest) {
  return (latest.opportunities || [])[0] || null;
}
function firstOpenTrade(trades) {
  return (trades || []).find(x => x.status !== "closed") || null;
}
function timing(signal) {
  return signal?.factor_scores?.entry_timing || {};
}
function watchItem(label, value) {
  return `<div class="watch-item"><span>${esc(label)}</span><strong>${esc(value ?? "-")}</strong></div>`;
}
function renderActiveTrade(trade) {
  const el = document.getElementById("activeTrade");
  if (!trade) { el.innerHTML = `<div class="empty">No open trade.</div>`; return; }
  el.innerHTML = `<div class="watch">
    ${watchItem("Contract", trade.tradingsymbol)}
    ${watchItem("Status", trade.status)}
    ${watchItem("Entry", trade.entry_price)}
    ${watchItem("SL", trade.stop_loss)}
    ${watchItem("Target 1", trade.target_1)}
    ${watchItem("Price source", trade.price_source || "-")}
    ${watchItem("Exit order", trade.exit_order_status || "-")}
    ${watchItem("Net P&L", trade.net_pnl ?? trade.pnl ?? "-")}
  </div>`;
}
function renderLatestWatch(signal) {
  const el = document.getElementById("latestWatch");
  if (!signal) { el.innerHTML = `<div class="empty">No current opportunity.</div>`; return; }
  const t = timing(signal);
  el.innerHTML = `<div class="watch">
    ${watchItem("State", t.entry_timing_state || signal.setup_state || "SIGNAL")}
    ${watchItem("Action", signal.action)}
    ${watchItem("Contract", signal.tradingsymbol)}
    ${watchItem("Entry", signal.entry_price)}
    ${watchItem("SL", signal.stop_loss)}
    ${watchItem("Target 1", signal.target_1)}
    ${watchItem("Chase", t.chase_risk || "-")}
    ${watchItem("Reason", t.entry_timing_reason || "Qualified signal")}
  </div>`;
}
function renderReady(h, d, k, au, a, m, c, ws, risk, latest, trades) {
  const blockers = [];
  if (h.status !== "ok") blockers.push("API");
  if (d.status !== "ok") blockers.push("Database");
  if (k.status !== "ok") blockers.push("Kite");
  if (risk.passed === false) blockers.push("Risk");
  if (!au.running && !a.running) blockers.push("Automation stopped");
  if (!m.running) blockers.push("Exit monitor stopped");
  if ((ws.websocket_enabled || ws.enabled) && ws.websocket_connected === false) blockers.push("WebSocket");
  const panel = document.getElementById("readyPanel");
  const mode = au.config?.order_mode || a.mode || "paper";
  const sig = latestSignal(latest);
  const active = firstOpenTrade(trades.trades || []);
  const state = timing(sig).entry_timing_state || sig?.setup_state || (sig ? "SIGNAL" : "NO_ACTIVE_SETUP");
  const panelState = blockers.length ? (blockers.length > 2 ? "bad" : "warn") : "ok";
  panel.className = `panel ready ${panelState}`;
  document.getElementById("readyTitle").textContent = blockers.length ? "Needs Attention" : "System Ready";
  document.getElementById("readySummary").textContent = blockers.length ? `Blocked by: ${blockers.join(", ")}` : `${state}${active ? " with active trade" : ""}`;
  document.getElementById("modeChip").className = `chip ${mode === "live" ? "bad" : "ok"}`;
  document.getElementById("modeChip").textContent = mode.toUpperCase();
  document.getElementById("readyChips").innerHTML = [
    chip(au.running ? "Automation running" : "Automation stopped", au.running ? "ok" : "bad"),
    chip(m.running ? "Exit monitor running" : "Exit monitor stopped", m.running ? "ok" : "bad"),
    chip(k.status === "ok" ? "Kite OK" : "Kite issue", k.status === "ok" ? "ok" : "bad"),
    chip(ws.websocket_connected ? "WebSocket connected" : "WebSocket not connected", ws.websocket_connected ? "ok" : "warn"),
    chip(risk.passed === false ? "Risk blocked" : "Risk allowed", risk.passed === false ? "bad" : "ok")
  ].join("");
  const t = timing(sig);
  document.getElementById("watchPanel").innerHTML =
    watchItem("Setup", state) +
    watchItem("Contract", sig?.tradingsymbol || active?.tradingsymbol || "-") +
    watchItem("Trigger", t.entry_trigger_price || "-") +
    watchItem("Reason", t.entry_timing_reason || (sig ? "Qualified signal" : "-"));
}
function renderDecisionFeed(feed) {
  const el = document.getElementById("decisionFeed");
  const events = feed.events || [];
  if (!events.length) { el.innerHTML = `<div class="empty">No scanner thoughts recorded yet.</div>`; return; }
  el.innerHTML = events.map(item => {
    const meta = [item.tradingsymbol, item.score !== undefined && item.score !== null ? `score ${item.score}` : null, item.status, item.outcome]
      .filter(Boolean).join(" | ");
    return `<div class="feed-item ${esc(item.severity || "warn")}">
      <div class="feed-head"><div class="feed-title">${esc(item.title)}</div><div class="feed-time">${esc(item.time || "")}</div></div>
      <div class="feed-message">${esc(item.message || "")}</div>
      <div class="feed-meta">${esc(meta)}</div>
    </div>`;
  }).join("");
}
function payload() {
  const orderMode = document.getElementById("orderMode").value;
  return {
    order_mode: orderMode,
    side: document.getElementById("side").value,
    symbols: document.getElementById("symbols").value,
    interval_seconds: Number(document.getElementById("interval").value),
    limit: Number(document.getElementById("limit").value),
    place_orders: true,
    confirm_live: orderMode === "live",
    monitor_outcomes: true,
    outcome_interval_seconds: Number(document.getElementById("outcomeInterval").value)
  };
}
async function showAction(fn) {
  const out = document.getElementById("actionResult");
  try { const data = await fn(); out.textContent = JSON.stringify(data, null, 2); await refreshAll(); }
  catch (e) { out.textContent = e.message; }
}
function scanOnce(){ showAction(() => postJson("/auto-trader/scan-once", {...payload(), place_orders:false})); }
function evaluateOpen(){ showAction(() => postJson("/opportunities/evaluate-open", {limit:100})); }
function automationPayload(){ return {
  order_mode: document.getElementById("orderMode").value,
  symbols: document.getElementById("symbols").value,
  side: document.getElementById("side").value,
  scan_interval_seconds: Number(document.getElementById("interval").value),
  snapshot_interval_seconds: 180,
  outcome_interval_seconds: Number(document.getElementById("outcomeInterval").value),
  checkpoint_overlap_minutes: 30,
  intraday_candle_sync: true,
  intraday_candle_sync_minutes: 5,
  scan_limit: Number(document.getElementById("limit").value),
  place_orders: true,
  confirm_live: document.getElementById("orderMode").value === "live"
}; }
function startAutomation(){ showAction(() => postJson("/automation/start", automationPayload())); }
function stopAutomation(){ showAction(() => postJson("/automation/stop")); }
function loadControls(config) {
  if (window.controlsLoaded || !config) return;
  document.getElementById("symbols").value = config.symbols || "";
  document.getElementById("orderMode").value = config.order_mode || "paper";
  document.getElementById("side").value = config.side || "BUY";
  document.getElementById("interval").value = config.scan_interval_seconds || 30;
  document.getElementById("outcomeInterval").value = config.outcome_interval_seconds || 30;
  document.getElementById("limit").value = config.scan_limit || 5;
  window.controlsLoaded = true;
}
async function refreshAll() {
  const [health, db, kite, auto, monitor, perf, latest, journal, failures, learning, execs, paper, margins, positions, risk, trades, automation, collector, ingest, websocket, cache, decisionFeed] = await Promise.allSettled([
    getJson("/health"), getJson("/db/health"), getJson("/kite/health"), getJson("/auto-trader/status"), getJson("/opportunity-monitor/status"),
    getJson("/opportunities/performance"), getJson("/auto-trader/latest"), getJson("/opportunities?limit=50"), getJson("/opportunities/failure-analysis"),
    getJson("/research/outcome-learning"), getJson("/auto-trader/executions"), getJson("/paper/trades"), getJson("/kite/margins"), getJson("/kite/positions"), getJson("/risk/status"), getJson("/trades?limit=50"),
    getJson("/automation/status"), getJson("/data/collector/status"), getJson("/data/ingest/status"), getJson("/kite/websocket/status"), getJson("/market-data/cache/status"), getJson("/dashboard/decision-feed?limit=30")
  ]);
  const val = r => r.status === "fulfilled" ? r.value : {error: r.reason.message};
  const h=val(health), d=val(db), k=val(kite), a=val(auto), m=val(monitor), p=val(perf), l=val(latest), j=val(journal), f=val(failures), learn=val(learning), r=val(risk), t=val(trades), au=val(automation), c=val(collector), ing=val(ingest), ws=val(websocket), cacheStatus=val(cache), feed=val(decisionFeed);
  loadControls(au.config);
  document.getElementById("statusCards").innerHTML =
    pill("API", h.status === "ok" ? "ok" : "bad", h.status || h.error) + pill("Database", d.status === "ok" ? "ok" : "bad", d.status || d.error) +
    pill("Kite", k.status === "ok" ? "ok" : "bad", k.status || k.error || "check") + pill("WebSocket", ws.websocket_connected ? "ok" : "warn", ws.websocket_connected ? "connected" : (ws.websocket_enabled ? "not connected" : "disabled")) +
    pill("Automation", au.running ? "ok" : "bad", au.running ? (au.market_open ? "running, market open" : "running") : "stopped") +
    pill("Auto Trader", a.running ? "ok" : "bad", a.running ? "running" : "stopped") +
    pill("Collector", c.running ? "ok" : "warn", c.running ? "running" : "stopped") + pill("Exit Monitor", m.running ? "ok" : "bad", m.running ? "running" : "stopped") +
    pill("Data Cache", cacheStatus.status === "ok" ? "ok" : "warn", cacheStatus.status || cacheStatus.error || "check");
  renderReady(h, d, k, au, a, m, c, ws, r, l, t);
  renderActiveTrade(firstOpenTrade(t.trades || []));
  renderLatestWatch(latestSignal(l));
  renderDecisionFeed(feed);
  document.getElementById("metrics").innerHTML =
    metric("Last scan", a.last_scan_at || "-") + metric("Latest found", a.latest_count || 0) + metric("Executions", a.execution_count || 0) +
    metric("Open", p.open || 0) + metric("Closed", p.closed || 0) + metric("Win rate", p.closed ? `${Math.round((p.wins || 0) / p.closed * 100)}%` : "0%") +
    metric("P&L", p.pnl || 0) + metric("Risk", r.passed === false ? "Blocked" : "Allowed");
  document.getElementById("activityJson").textContent = JSON.stringify({automation:au, auto_trader:a, collector:c, ingestion:ing, monitor:m, performance:p, risk:r, websocket:ws, cache:cacheStatus, decision_feed:feed}, null, 2);
  table(document.getElementById("latestTable"), l.opportunities || [], ["symbol","action","tradingsymbol","entry_price","stop_loss","target_1","quantity","score"]);
  table(document.getElementById("journalTable"), j.opportunities || [], ["id","created_at","action","tradingsymbol","entry_price","stop_loss","target_1","score","status","outcome","pnl"]);
  document.getElementById("failureJson").textContent = JSON.stringify(f, null, 2);
  document.getElementById("learningJson").textContent = JSON.stringify(learn, null, 2);
  document.getElementById("decisionJson").textContent = JSON.stringify({
    latest_opportunity: (l.opportunities || [])[0] || null,
    open_lifecycle_trades: (t.trades || []).filter(x => x.status !== "closed"),
    last_monitor_results: m
  }, null, 2);
  document.getElementById("ordersJson").textContent = JSON.stringify({executions: val(execs), lifecycle_trades: t, paper: val(paper)}, null, 2);
  document.getElementById("accountJson").textContent = JSON.stringify({margins: val(margins), positions: val(positions)}, null, 2);
  document.getElementById("refreshText").textContent = `Last refreshed ${new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })} IST`;
}
refreshAll();
setInterval(refreshAll, 5000);
</script>
</body>
</html>
        """
    )


def _decision_event_from_opportunity(record) -> dict[str, object]:
    factors = _json_dict(getattr(record, "factor_scores_json", None))
    timing = factors.get("entry_timing", {}) if isinstance(factors.get("entry_timing"), dict) else {}
    state = str(timing.get("entry_timing_state") or "ACCEPTED")
    reason = str(timing.get("entry_timing_reason") or "Accepted opportunity saved")
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
    }


def _decision_event_from_rejection(record) -> dict[str, object]:
    factors = _json_dict(getattr(record, "factor_scores_json", None))
    timing = factors.get("entry_timing", {}) if isinstance(factors.get("entry_timing"), dict) else {}
    reasons = _json_list(getattr(record, "reasons_json", None))
    state = str(timing.get("entry_timing_state") or "REJECTED")
    reason = str(timing.get("entry_timing_reason") or "; ".join(reasons[:3]) or record.primary_gate or "Rejected setup")
    severity = "warn"
    if any(item in reasons for item in ["entry_too_late", "chase_risk_high", "selected_option_quote_invalid"]):
        severity = "bad"
    elif any(item in reasons for item in ["waiting_for_entry_trigger", "premium_trigger_not_broken_yet"]):
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
        message = str(record.exit_last_error or record.exit_order_status or "Exit needs monitoring")
    else:
        title = f"Trade open: {record.action}"
        severity = "watch"
        message = f"Entry {record.entry_price}; SL {record.stop_loss}; Target 1 {record.target_1}"
    return {
        "time": format_ist(record.updated_at or record.created_at),
        "sort_time": (record.updated_at or record.created_at).isoformat() if (record.updated_at or record.created_at) else "",
        "type": "trade",
        "severity": severity,
        "title": title,
        "message": message,
        "symbol": record.symbol,
        "tradingsymbol": record.tradingsymbol,
        "mode": record.mode,
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


def _decision_event_from_error(item: dict[str, object]) -> dict[str, object]:
    return {
        "time": item.get("time"),
        "sort_time": str(item.get("time") or ""),
        "type": "auto_error",
        "severity": "bad",
        "title": "Automation error",
        "message": item.get("error"),
        "order_key": item.get("order_key"),
    }


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
        "exit_order_id": getattr(record, "exit_order_id", None),
        "exit_order_status": getattr(record, "exit_order_status", None),
        "exit_attempt_count": getattr(record, "exit_attempt_count", 0),
        "exit_last_error": getattr(record, "exit_last_error", None),
        "exit_requested_at": format_ist(getattr(record, "exit_requested_at", None)),
        "exit_confirmed_at": format_ist(getattr(record, "exit_confirmed_at", None)),
        "price_source": getattr(record, "price_source", None),
        "price_timestamp": format_ist(getattr(record, "price_timestamp", None)),
        "price_age_seconds": getattr(record, "price_age_seconds", None),
        "gross_pnl": getattr(record, "gross_pnl", None),
        "net_pnl": getattr(record, "net_pnl", None),
        "charges": getattr(record, "charges", None),
        "slippage_cost": getattr(record, "slippage_cost", None),
        "spread_cost": getattr(record, "spread_cost", None),
        "remaining_quantity": getattr(record, "remaining_quantity", None),
        "pnl": getattr(record, "net_pnl", None) if getattr(record, "net_pnl", None) is not None else record.pnl,
        "outcome": record.outcome,
        "notes": record.notes,
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


@app.get("/market/{symbol}", tags=["09 Market Data"], summary="Get stored market summary for a symbol")
def get_market_summary(symbol: str) -> dict[str, object]:
    return market_data_service.get_market_summary(symbol)


@app.get("/data/ingest/status", tags=["11 Data Ingestion"], summary="Show stored candle and option-history counts")
def get_ingestion_status(symbols: str | None = None, timeframe: str = "5minute") -> dict[str, object]:
    return data_ingestion_service.status(symbols=parse_symbol_list(symbols, default=[]) if symbols else None, timeframe=timeframe)


@app.post(
    "/data/ingest/candles",
    tags=["11 Data Ingestion"],
    summary="Ingest historical underlying candles from Kite",
    description="Fetches Kite historical OHLCV candles for the given symbols and stores them in MySQL for backtesting.",
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
    try:
        return data_ingestion_service.ingest_candles(
            symbols=parse_symbol_list(payload.get("symbols")),
            timeframe=str(payload.get("timeframe") or "5minute"),
            from_date=str(payload.get("from")) if payload.get("from") else None,
            to_date=str(payload.get("to")) if payload.get("to") else None,
            days=int(payload.get("days") or 90),
            use_checkpoint=bool(payload.get("use_checkpoint", False)),
            overlap_minutes=int(payload.get("overlap_minutes") or settings.automation_checkpoint_overlap_minutes),
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
            max_contracts_per_symbol=int(payload.get("max_contracts_per_symbol") or 120),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/data/collector/start",
    tags=["11 Data Ingestion"],
    summary="Start continuous option-chain snapshot collection",
    description="Continuously stores option quote, bid/ask, OI, IV, and Greeks snapshots. Use during market hours to build option-history data.",
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


@app.post("/data/collector/stop", tags=["11 Data Ingestion"], summary="Stop continuous option-chain snapshot collection")
async def stop_option_snapshot_collector() -> dict[str, object]:
    return await option_snapshot_collector_service.stop()


@app.get("/data/collector/status", tags=["11 Data Ingestion"], summary="Get option-chain snapshot collector status")
def get_option_snapshot_collector_status() -> dict[str, object]:
    return option_snapshot_collector_service.status()


@app.post(
    "/automation/start",
    tags=["12 Automation"],
    summary="Start one-switch automation supervisor",
    description=(
        "Automatically handles daily candle ingestion, market-hour option snapshot collection, auto scanning, "
        "and outcome monitoring. Orders stay disabled unless `place_orders=true`; live orders still require live config and `confirm_live=true`."
    ),
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
    return automation_supervisor_service.start(payload or {})


@app.post("/automation/stop", tags=["12 Automation"], summary="Stop automation supervisor and intraday collector/scanner")
async def stop_automation() -> dict[str, object]:
    return await automation_supervisor_service.stop()


@app.post(
    "/automation/run-once",
    tags=["12 Automation"],
    summary="Run one automation supervisor cycle now",
    description="Runs the same decision cycle the supervisor loop runs: bootstrap data if needed, start market-hour services, or evaluate open outcomes after hours.",
)
def run_automation_once(payload: dict[str, object] | None = Body(default=None)) -> dict[str, object]:
    return automation_supervisor_service.run_once(payload or None)


@app.get("/automation/status", tags=["12 Automation"], summary="Get automation supervisor status")
def get_automation_status() -> dict[str, object]:
    return automation_supervisor_service.status()


@app.post(
    "/data/ingest/option-candles",
    tags=["11 Data Ingestion"],
    summary="Ingest historical candles for nearby option contracts",
    description=(
        "Fetches OHLCV candles for currently listed nearby option contracts. "
        "This stores option premium movement, while `/data/ingest/option-snapshots` stores bid/ask, OI, IV, and Greeks."
    ),
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
    try:
        return data_ingestion_service.ingest_option_candles(
            symbols=parse_symbol_list(payload.get("symbols")),
            timeframe=str(payload.get("timeframe") or "5minute"),
            from_date=str(payload.get("from")) if payload.get("from") else None,
            to_date=str(payload.get("to")) if payload.get("to") else None,
            days=int(payload.get("days") or 30),
            strike_window_pct=float(payload.get("strike_window_pct") or 2.0),
            max_contracts_per_symbol=int(payload.get("max_contracts_per_symbol") or 20),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/data/ingest/all",
    tags=["11 Data Ingestion"],
    summary="Ingest candles and capture option snapshots",
    description="Runs historical underlying candle ingestion first, then captures current option-chain snapshots for the same symbols.",
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
    try:
        return data_ingestion_service.ingest_all(
            symbols=parse_symbol_list(payload.get("symbols")),
            timeframe=str(payload.get("timeframe") or "5minute"),
            from_date=str(payload.get("from")) if payload.get("from") else None,
            to_date=str(payload.get("to")) if payload.get("to") else None,
            days=int(payload.get("days") or 90),
            strike_window_pct=float(payload.get("strike_window_pct") or 4.0),
            max_contracts_per_symbol=int(payload.get("max_contracts_per_symbol") or 120),
            use_checkpoint=bool(payload.get("use_checkpoint", False)),
            overlap_minutes=int(payload.get("overlap_minutes") or settings.automation_checkpoint_overlap_minutes),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/research/settings", tags=["10 Research"], summary="Show option-buying research thresholds")
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
            "default_horizon_candles": settings.backtest_horizon_candles,
            "mode": "underlying_rule_replay",
            "historical_option_snapshots": option_history_repository.count_snapshots(),
            "option_stop_loss_pct": settings.backtest_option_stop_loss_pct,
            "option_target_pct": settings.backtest_option_target_pct,
            "slippage_pct": settings.backtest_slippage_pct,
            "charges_pct": settings.backtest_charges_pct,
            "walk_forward_train_pct": settings.backtest_walk_forward_train_pct,
        },
        "strategy_edge_guard": {
            "enabled": settings.enable_strategy_edge_guard,
            "min_trades": settings.min_strategy_trades,
            "min_expectancy_pct": settings.min_strategy_expectancy_pct,
            "min_profit_factor": settings.min_strategy_profit_factor,
            "min_win_rate_pct": settings.min_strategy_win_rate_pct,
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
                "status": "not_supported_by_current_order_service",
                "fallback": "startup broker sync plus software square-off",
            },
            "partial_booking": {
                "enabled": settings.enable_partial_booking,
                "target1_pct": settings.partial_target1_pct,
                "move_sl_to_cost": settings.partial_move_sl_to_cost,
                "note": "disabled by default; only practical when quantity can be split by lot size",
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
            instrument_token=int(contract_payload["instrument_token"]) if contract_payload.get("instrument_token") else None,
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
        premium_eval = option_premium_confirmation_service.evaluate(contract=contract, side=side, timeframe=timeframe)
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
            "day_type": day_type_service.evaluate(symbol=symbol, trend=trend, timeframe=timeframe),
            "option_premium_confirmation": premium_eval,
            "time_bucket_edge": time_bucket_edge_service.evaluate(symbol=symbol, trend=trend, timeframe=timeframe),
        },
    }


@app.get(
    "/research/outcome-learning",
    tags=["10 Research"],
    summary="Analyze actual opportunity outcomes for adaptive setup guards",
    description="Shows which symbols, actions, setup types, day types, and time buckets have enough closed-trade evidence to trust or avoid.",
)
def get_outcome_learning() -> dict[str, object]:
    return {"status": "ok", "learning": outcome_learning_service.analyze()}


@app.get(
    "/research/opportunity-analytics",
    tags=["10 Research"],
    summary="Analyze scanner opportunity outcomes by professional segments",
    description="Studies saved opportunities by CE/PE, expiry day, time bucket, score bucket, setup type, and failure tags.",
)
def get_opportunity_analytics(symbol: str = "BANKNIFTY", limit: int = 1000) -> dict[str, object]:
    return opportunity_analytics_service.analyze(symbol=symbol, limit=limit)


@app.get(
    "/research/execution-analytics",
    tags=["10 Research"],
    summary="Analyze paper/live execution quality and trade outcomes",
    description="Separates execution quality from signal quality: fill rate, entry deviation, CE/PE results, time bucket, and P&L metrics.",
)
def get_execution_analytics(symbol: str = "BANKNIFTY", limit: int = 1000) -> dict[str, object]:
    return execution_analytics_service.analyze(symbol=symbol, limit=limit)


@app.get(
    "/research/professional-insights",
    tags=["10 Research"],
    summary="Professional Bank Nifty decision-quality insights",
    description="Combines accepted vs rejected analysis, time buckets, DTE, factor attribution, exits, data quality, and shadow/live evidence.",
)
def get_professional_insights(symbol: str = "BANKNIFTY", limit: int = 1000) -> dict[str, object]:
    return professional_insights_service.analyze(symbol=symbol, limit=limit)


@app.get(
    "/research/daily-review",
    tags=["10 Research"],
    summary="Daily Bank Nifty trading review",
    description="Shows the day's accepted setups, rejected setups, trades, outcomes, top rejection gates, and review timeline.",
)
def get_daily_review(symbol: str = "BANKNIFTY", review_date: str | None = None, limit: int = 1000) -> dict[str, object]:
    parsed_date = datetime.fromisoformat(review_date).date() if review_date else None
    return professional_insights_service.daily_review(symbol=symbol, review_date=parsed_date, limit=limit)


@app.get(
    "/research/trade-journal",
    tags=["10 Research"],
    summary="Unified opportunity, rejection, and trade timeline",
)
def get_trade_journal(symbol: str = "BANKNIFTY", limit: int = 200) -> dict[str, object]:
    return professional_insights_service.trade_journal(symbol=symbol, limit=limit)


@app.get(
    "/research/data-completeness",
    tags=["10 Research"],
    summary="Inspect stored candle and option snapshot completeness",
)
def get_data_completeness(symbol: str = "BANKNIFTY") -> dict[str, object]:
    return professional_insights_service.data_completeness(symbol=symbol)


@app.get(
    "/research/shadow-comparison",
    tags=["10 Research"],
    summary="Compare shadow, paper, and live trade evidence",
)
def get_shadow_comparison(symbol: str = "BANKNIFTY", limit: int = 1000) -> dict[str, object]:
    return professional_insights_service.analyze(symbol=symbol, limit=limit)["shadow_mode_comparison"]


@app.get(
    "/research/professional-readiness",
    tags=["10 Research"],
    summary="Combined professional-readiness report for Bank Nifty option buying",
    description="Combines option data coverage, opportunity outcomes, execution analytics, option backtest, ablation, and walk-forward validation.",
)
def get_professional_readiness(
    symbol: str = "BANKNIFTY",
    timeframe: str = "5minute",
    direction: str = "BOTH",
    limit: int = 3000,
) -> dict[str, object]:
    return professional_readiness_service.report(symbol=symbol, timeframe=timeframe, direction=direction, limit=limit)


@app.post(
    "/research/greeks",
    tags=["10 Research"],
    summary="Estimate IV and Greeks for an option",
    description="Use this to inspect whether a proposed option has usable delta, acceptable theta decay, and reasonable IV.",
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
            instrument_token=int(contract_payload["instrument_token"]) if contract_payload.get("instrument_token") is not None else None,
            name=str(contract_payload.get("name") or payload.get("symbol") or ""),
            expiry=str(contract_payload.get("expiry") or ""),
            strike=float(contract_payload.get("strike") or 0),
            option_type=str(contract_payload.get("option_type") or contract_payload.get("instrument_type") or ""),
            lot_size=int(contract_payload.get("lot_size") or 1),
            last_price=float(contract_payload.get("last_price") or payload.get("entry_price") or 0),
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
    try:
        return backtest_service.run(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            side=str(payload.get("side") or "BUY"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"]) if payload.get("horizon_candles") is not None else None,
            limit=int(payload.get("limit") or 2000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/backtest/options",
    tags=["10 Research"],
    summary="Replay strategy on historical option premium candles",
    description="Uses stored option OHLC candles to simulate real option entry, premium stop loss, target, slippage, and exits.",
)
def run_option_premium_backtest(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "NIFTY",
                "timeframe": "5minute",
                "direction": "BOTH",
                "horizon_candles": 12,
                "limit": 3000,
            }
        ]
    ),
) -> dict[str, object]:
    try:
        return backtest_service.run_option_premium(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"]) if payload.get("horizon_candles") is not None else None,
            limit=int(payload.get("limit") or 3000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/backtest/ablation",
    tags=["10 Research"],
    summary="Compare backtest performance with selected factors removed",
    description="Runs option-premium replay variants to identify which available factors improve or hurt net expectancy.",
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
    try:
        return backtest_service.run_ablation(
            symbol=str(payload.get("symbol") or "BANKNIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"]) if payload.get("horizon_candles") is not None else None,
            limit=int(payload.get("limit") or 1000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/walk-forward",
    tags=["10 Research"],
    summary="Run walk-forward option-premium validation",
    description="Splits historical data into train/test periods and validates out-of-sample option-premium performance.",
)
def run_walk_forward_validation(
    payload: dict[str, object] = Body(
        examples=[
            {
                "symbol": "NIFTY",
                "timeframe": "5minute",
                "direction": "BOTH",
                "horizon_candles": 12,
                "limit": 3000,
            }
        ]
    ),
) -> dict[str, object]:
    try:
        return backtest_service.run_walk_forward(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            horizon_candles=int(payload["horizon_candles"]) if payload.get("horizon_candles") is not None else None,
            limit=int(payload.get("limit") or 3000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/research/strategy-edge/validate",
    tags=["10 Research"],
    summary="Validate and save measured strategy edge",
    description="Runs walk-forward validation, stores the result, and makes it available to the optional scanner edge guard.",
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
    try:
        return strategy_edge_service.validate(
            symbol=str(payload.get("symbol") or "NIFTY"),
            timeframe=str(payload.get("timeframe") or "5minute"),
            direction=str(payload.get("direction") or "BOTH"),
            limit=int(payload.get("limit") or 3000),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/research/strategy-edge", tags=["10 Research"], summary="List recent saved strategy edge validations")
def list_strategy_edge(limit: int = 50) -> dict[str, object]:
    rows = strategy_edge_service.recent(limit=limit)
    return {"count": len(rows), "validations": rows}


@app.post(
    "/research/strategy-ranking",
    tags=["10 Research"],
    summary="Rank symbols and directions by measured walk-forward edge",
    description="Runs/saves strategy validations and returns the highest expectancy setups first.",
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
    symbols = parse_symbol_list(payload.get("symbols"))
    direction_value = payload.get("directions") or "CALL,PUT"
    directions = [item.strip().upper() for item in str(direction_value).split(",") if item.strip()]
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        for direction in directions:
            try:
                rows.append(
                    strategy_edge_service.validate(
                        symbol=symbol,
                        timeframe=str(payload.get("timeframe") or "5minute"),
                        direction=direction,
                        limit=int(payload.get("limit") or 3000),
                    )
                )
            except Exception as exc:
                rows.append({"symbol": symbol, "direction": direction, "passed": False, "reasons": [str(exc)], "summary": {}})
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
    try:
        rows = payload.get("snapshots")
        if not isinstance(rows, list):
            raise ValueError("snapshots must be a list")
        return option_history_repository.import_snapshots([row for row in rows if isinstance(row, dict)])
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/research/option-history",
    tags=["10 Research"],
    summary="List stored historical option quote snapshots",
)
def list_option_history(underlying: str | None = None, limit: int = 20) -> dict[str, object]:
    records = option_history_repository.latest_snapshots(underlying=underlying, limit=limit)
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
def get_opportunities(side: str = "BUY", symbols: str | None = None, limit: int = 10, order_mode: str = "paper") -> dict[str, object]:
    scanner_service = get_scanner_service()
    symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
    recommendations = scanner_service.scan_symbols(symbols=symbol_list, side=side.upper(), order_mode=order_mode.lower())
    saved_ids = [opportunity_repository.save_opportunity(signal).id for signal in recommendations[:limit]]
    return {
        "mode": order_mode.lower(),
        "market_data": type(scanner_service.feed).__name__,
        "kite_access_token": bool(load_access_token()),
        "side": side.upper(),
        "count": len(recommendations),
        "saved_ids": saved_ids,
        "opportunities": [asdict(signal) for signal in recommendations[:limit]],
        "disclaimer": "Signals are probability-ranked trade setups with risk checks, not guaranteed profits.",
    }


@app.get(
    "/scanner/diagnostics",
    tags=["03 Scanner"],
    summary="Explain why symbols passed or failed scanner gates",
    description="Use after `/scanner/opportunities` when a symbol is not appearing or when a signal needs explanation.",
)
def get_scanner_diagnostics(side: str = "BUY", symbols: str | None = None, limit: int = 25, order_mode: str = "paper") -> dict[str, object]:
    scanner_service = get_scanner_service()
    symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
    diagnostics = scanner_service.scan_with_diagnostics(symbols=symbol_list, side=side.upper(), order_mode=order_mode.lower())
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


@app.post(
    "/orders/place",
    tags=["04 Orders"],
    summary="Place a paper or live order from a scanner signal",
    description=(
        "Paper order: send `confirm_live=false`. Live order requires `LIVE_TRADING_MODE=true`, "
        "`PAPER_TRADING_MODE=false`, and `confirm_live=true`."
    ),
)
def place_order(
    payload: dict[str, object] = Body(
        examples=[
            {
                "confirm_live": False,
                "signal": {
                    "symbol": "NIFTY",
                    "action": "BUY_CE",
                    "side": "BUY",
                    "tradingsymbol": "NIFTY24JUN22000CE",
                    "exchange": "NFO",
                    "entry_price": 100,
                    "stop_loss": 78,
                    "quantity": 50,
                    "lot_size": 50,
                    "score": 85,
                },
            }
        ]
    ),
) -> dict[str, object]:
    from app.models import Signal

    try:
        signal_payload = payload.get("signal")
        if not isinstance(signal_payload, dict):
            signal_payload = {key: value for key, value in payload.items() if key not in {"confirm_live", "opportunity_id", "order_mode"}}
        signal = Signal(**signal_payload)  # type: ignore[arg-type]
        confirm_live = bool(payload.get("confirm_live", False))
        order_mode = str(payload.get("order_mode") or settings.default_order_mode or "paper").lower()
        opportunity_id_value = payload.get("opportunity_id")
        opportunity_id = int(opportunity_id_value) if opportunity_id_value is not None else None
        return get_order_service().place_signal_order(signal, confirm_live=confirm_live, opportunity_id=opportunity_id, order_mode=order_mode)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/risk/status", tags=["04 Orders"], summary="Check daily risk guard status")
def get_risk_status() -> dict[str, object]:
    return {
        **risk_management_service.evaluate_entry(),
        "broker_reconciliation": broker_sync_service.live_block_status(),
    }


@app.get("/trades", tags=["04 Orders"], summary="List actual paper/live trade lifecycle records")
def list_trades(status: str | None = None, limit: int = 100) -> dict[str, object]:
    records = trade_repository.list_trades(status=status, limit=limit)
    return {"count": len(records), "trades": [trade_record_to_dict(record) for record in records]}


@app.get("/trades/exit-alerts", tags=["04 Orders"], summary="List live trades stuck in closing, exit_failed, or reconciliation mismatch")
def trade_exit_alerts(limit: int = 100) -> dict[str, object]:
    records = trade_repository.exit_alerts(limit=limit)
    return {
        "count": len(records),
        "alerts": [trade_record_to_dict(record) for record in records],
        "broker_reconciliation": broker_sync_service.live_block_status(),
    }


@app.post("/trades/{trade_id}/close", tags=["04 Orders"], summary="Manually close a lifecycle trade")
def close_trade(
    trade_id: int,
    payload: dict[str, object] = Body(examples=[{"outcome": "target_1", "exit_price": 115, "notes": "Manual paper exit"}]),
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


@app.post("/trades/sync", tags=["04 Orders"], summary="Sync open live trades with Kite order status")
def sync_live_trades(payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100}])) -> dict[str, object]:
    payload = payload or {}
    try:
        return broker_sync_service.sync_open_trades(limit=int(payload.get("limit") or 100))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/broker/reconcile", tags=["04 Orders"], summary="Run broker/local live position reconciliation")
def reconcile_broker_positions() -> dict[str, object]:
    try:
        return broker_sync_service.reconcile_startup_positions()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/broker/reconciliation/status", tags=["04 Orders"], summary="Inspect broker/local reconciliation live-trading block")
def broker_reconciliation_status() -> dict[str, object]:
    return broker_sync_service.live_block_status()


@app.get("/broker/emergency-protection/status", tags=["04 Orders"], summary="Inspect broker-side emergency SL/GTT capability")
def broker_emergency_protection_status() -> dict[str, object]:
    return {
        "enabled": settings.enable_broker_emergency_sl,
        "supported": False,
        "reason": "KiteProvider currently exposes regular market order placement only; trigger_price/GTT/OCO protective order APIs are not wrapped and validated for this flow.",
        "fallback": "software exits via TradeExitService, active WebSocket/polling price checks, startup broker reconciliation, and exit-failure alerts",
    }


@app.post("/trades/evaluate-exits", tags=["04 Orders"], summary="Auto square-off open trades at target or stop")
def evaluate_trade_exits(payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100}])) -> dict[str, object]:
    payload = payload or {}
    try:
        return trade_exit_service.evaluate_once(limit=int(payload.get("limit") or 100))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/alerts/test", tags=["01 System"], summary="Send a test Telegram alert if configured")
def send_test_alert(payload: dict[str, object] | None = Body(default=None, examples=[{"message": "AI Option Trader test alert"}])) -> dict[str, object]:
    payload = payload or {}
    return notification_service.send(str(payload.get("message") or "AI Option Trader test alert"))


@app.post(
    "/auto-trader/start",
    tags=["05 Auto Trader"],
    summary="Start continuous opportunity scanning",
    description="Starts scanner loop. By default it also starts outcome monitoring so saved opportunities are studied.",
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
        symbols = [item.strip().upper() for item in symbols_value.split(",") if item.strip()]
    elif isinstance(symbols_value, list):
        symbols = [str(item).strip().upper() for item in symbols_value if str(item).strip()]

    order_mode = str(payload.get("order_mode") or settings.default_order_mode or "paper").lower()
    status = auto_trader_service.start(
        side=str(payload.get("side") or "BUY"),
        symbols=symbols,
        interval_seconds=int(payload.get("interval_seconds") or settings.scanner_interval_seconds),
        limit=int(payload.get("limit") or 5),
        place_orders=bool(payload.get("place_orders", True)),
        confirm_live=bool(payload.get("confirm_live", order_mode == "live")),
        order_mode=order_mode,
    )
    if bool(payload.get("monitor_outcomes", True)):
        opportunity_outcome_service.start(interval_seconds=int(payload.get("outcome_interval_seconds") or 30))
    return {
        "auto_trader": status,
        "opportunity_monitor": opportunity_outcome_service.status(),
    }


@app.post("/auto-trader/stop", tags=["05 Auto Trader"], summary="Stop continuous scanning")
async def stop_auto_trader() -> dict[str, object]:
    return await auto_trader_service.stop()


@app.get("/auto-trader/status", tags=["05 Auto Trader"], summary="Get continuous scanner status")
def get_auto_trader_status() -> dict[str, object]:
    return auto_trader_service.status()


@app.get("/auto-trader/latest", tags=["05 Auto Trader"], summary="Get latest auto-trader opportunities")
def get_auto_trader_latest() -> dict[str, object]:
    return {
        "status": auto_trader_service.status(),
        "opportunities": auto_trader_service.latest_opportunities,
    }


@app.get("/auto-trader/executions", tags=["05 Auto Trader"], summary="Get auto-trader order execution history")
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
    for record in rejected_opportunity_repository.list_rejections(symbol="BANKNIFTY", limit=limit):
        events.append(_decision_event_from_rejection(record))
    for record in trade_repository.list_trades(limit=limit):
        events.append(_decision_event_from_trade(record))
    for item in auto_trader_service.executions[-limit:]:
        events.append(_decision_event_from_execution(item))
    for item in auto_trader_service.errors[-limit:]:
        events.append(_decision_event_from_error(item))
    events = sorted(events, key=lambda item: str(item.get("sort_time") or ""), reverse=True)[:limit]
    for item in events:
        item.pop("sort_time", None)
    return {"status": "ok", "count": len(events), "events": events}


@app.get(
    "/opportunities",
    tags=["06 Opportunity Journal"],
    summary="List saved scanner opportunities",
    description="Use `status=open` to see opportunities still being monitored, or omit status to see recent records.",
)
def list_saved_opportunities(status: str | None = None, limit: int = 50) -> dict[str, object]:
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


@app.get("/opportunities/performance", tags=["06 Opportunity Journal"], summary="Summarize opportunity win/loss performance")
def get_opportunity_performance() -> dict[str, object]:
    return opportunity_repository.summarize_performance()


@app.get(
    "/opportunities/failure-analysis",
    tags=["06 Opportunity Journal"],
    summary="Aggregate repeated failure reasons",
    description="Shows tags such as low premium noise, expiry-day risk, weak confirmation, and spread/slippage drag.",
)
def get_opportunity_failure_analysis() -> dict[str, object]:
    return opportunity_repository.failure_analysis()


@app.get(
    "/opportunities/rejections",
    tags=["06 Opportunity Journal"],
    summary="Analyze rejected scanner setups",
    description="Shows hard-gate rejection reasons and any later manually/evaluated outcome for rejected setups.",
)
def get_rejected_opportunities(symbol: str | None = "BANKNIFTY", limit: int = 1000) -> dict[str, object]:
    return rejected_opportunity_repository.analyze(symbol=symbol, limit=limit)


@app.post(
    "/opportunities/rejections/{rejection_id}/outcome",
    tags=["06 Opportunity Journal"],
    summary="Mark later outcome for a rejected setup",
)
def update_rejected_opportunity_outcome(
    rejection_id: int,
    payload: dict[str, object] = Body(examples=[{"outcome": "would_have_hit_target", "exit_price": 250.0, "notes": "Rejected setup later moved well"}]),
) -> dict[str, object]:
    outcome = str(payload.get("outcome") or "")
    if not outcome:
        raise HTTPException(status_code=400, detail="outcome is required")
    exit_price = float(payload["exit_price"]) if payload.get("exit_price") is not None else None
    try:
        record = rejected_opportunity_repository.mark_later_outcome(
            rejection_id,
            outcome=outcome,
            exit_price=exit_price,
            notes=str(payload.get("notes") or ""),
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
    payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100}])
) -> dict[str, object]:
    payload = payload or {}
    return opportunity_outcome_service.evaluate_once(limit=int(payload.get("limit") or 100))


@app.post(
    "/opportunities/rejections/evaluate-open",
    tags=["07 Outcome Monitor"],
    summary="Evaluate rejected setups for later outcomes",
    description="Checks rejected scanner setups against current option prices and stores later_outcome when target/stop/expiry can be inferred.",
)
def evaluate_rejected_opportunities(
    payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100, "symbol": "BANKNIFTY"}])
) -> dict[str, object]:
    payload = payload or {}
    symbol_value = payload.get("symbol", "BANKNIFTY")
    symbol = str(symbol_value) if symbol_value is not None else None
    return rejected_opportunity_outcome_service.evaluate_once(symbol=symbol, limit=int(payload.get("limit") or 100))


@app.post(
    "/opportunity-monitor/start",
    tags=["07 Outcome Monitor"],
    summary="Start automatic outcome monitoring",
    description="Usually started automatically by `/auto-trader/start` when `monitor_outcomes=true`.",
)
async def start_opportunity_monitor(
    payload: dict[str, object] | None = Body(default=None, examples=[{"interval_seconds": 30}])
) -> dict[str, object]:
    payload = payload or {}
    return opportunity_outcome_service.start(interval_seconds=int(payload.get("interval_seconds") or 30))


@app.post("/opportunity-monitor/stop", tags=["07 Outcome Monitor"], summary="Stop automatic outcome monitoring")
async def stop_opportunity_monitor() -> dict[str, object]:
    return await opportunity_outcome_service.stop()


@app.get("/opportunity-monitor/status", tags=["07 Outcome Monitor"], summary="Get outcome monitor status")
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
            symbols = [item.strip().upper() for item in symbols_value.split(",") if item.strip()]
        elif isinstance(symbols_value, list):
            symbols = [str(item).strip().upper() for item in symbols_value if str(item).strip()]
        auto_trader_service.config = {
            "side": str(payload.get("side") or "BUY").upper(),
            "symbols": symbols,
            "interval_seconds": int(payload.get("interval_seconds") or settings.scanner_interval_seconds),
            "limit": int(payload.get("limit") or 5),
            "place_orders": bool(payload.get("place_orders", False)),
            "confirm_live": False,
            "order_mode": str(payload.get("order_mode") or settings.default_order_mode or "paper").lower(),
        }
    return auto_trader_service.scan_once()


@app.get("/paper/summary", tags=["08 Paper Trading"], summary="Get paper-trading summary")
def get_paper_summary() -> dict[str, object]:
    return paper_trading_service.get_summary()


@app.get("/paper/positions", tags=["08 Paper Trading"], summary="List open paper positions")
def get_paper_positions() -> dict[str, object]:
    return {
        "count": len(paper_trading_service.positions),
        "positions": paper_trading_service.positions,
    }


@app.get("/paper/trades", tags=["08 Paper Trading"], summary="List open and closed paper trades")
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
    payload: dict[str, object] = Body(examples=[{"symbol": "NIFTY24JUN22000CE", "exit_price": 115}])
) -> dict[str, object]:
    symbol = str(payload.get("symbol") or "")
    exit_price = payload.get("exit_price")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if exit_price is None:
        raise HTTPException(status_code=400, detail="exit_price is required")
    try:
        trade = paper_trading_service.close_trade(symbol=symbol, exit_price=float(exit_price))
        return {"status": "closed", "trade": trade, "summary": paper_trading_service.get_summary()}
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
        kite_provider = get_kite_provider()
        profile = kite_provider.profile()
        return profile
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/websocket/status", tags=["02 Kite Login"], summary="Inspect active Kite WebSocket price feed")
def kite_websocket_status() -> dict[str, object]:
    status = active_trade_price_feed.status()
    status["banknifty_option_prewarm"] = banknifty_option_prewarm_service.status()
    return status


@app.get("/kite/margins", tags=["02 Kite Login"], summary="Get Zerodha margins/funds")
def kite_margins() -> dict[str, object]:
    try:
        kite_provider = get_kite_provider()
        return kite_provider.margins()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/positions", tags=["02 Kite Login"], summary="Get Zerodha positions")
def kite_positions() -> dict[str, object]:
    try:
        kite_provider = get_kite_provider()
        return kite_provider.positions()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/callback", response_class=HTMLResponse, tags=["02 Kite Login"], summary="Handle Kite redirect callback")
@app.get("/login", response_class=HTMLResponse, tags=["02 Kite Login"], summary="Handle alternate Kite redirect callback")
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
            html = f"<h3>Login successful</h3><p>Access token saved.</p><p><a href=\"/\">Back to app</a></p>"
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
    payload: dict[str, str] = Body(examples=[{"request_token": "paste_request_token_here"}])
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
        profile = kite_provider.profile()
        return {
            "status": "ok" if access_token else "missing-access-token",
            "saved": bool(access_token),
            "profile": profile,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
