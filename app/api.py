import json
from dataclasses import asdict

from fastapi import Body, FastAPI, HTTPException
from sqlalchemy import text

from app.config import settings
from app.services.market_data_service import MarketDataService
from app.services.scanner_service import ScannerService
from app.services.signal_repository import SignalRepository
from app.services.order_service import OrderService
from app.services.paper_trading_service import PaperTradingService
from app.services.auto_trader_service import AutoTraderService
from app.services.opportunity_repository import OpportunityRepository
from app.services.opportunity_outcome_service import OpportunityOutcomeService
from app.services.database import get_session
from app.providers.kite_provider import KiteProvider
from app.providers.token_store import save_access_token, load_access_token
from fastapi.responses import RedirectResponse, HTMLResponse

API_DESCRIPTION = """
AI Option Trader API workflow.

Recommended sequence:

1. System checks: `GET /health`, `GET /db/health`
2. Kite login: `GET /kite/auth`, then verify with `GET /kite/health`
3. Account checks: `GET /kite/margins`, `GET /kite/positions`
4. Manual scan: `GET /scanner/opportunities?side=BUY&symbols=NIFTY,BANKNIFTY&limit=3`
5. Review diagnostics: `GET /scanner/diagnostics?side=BUY&symbols=NIFTY`
6. Paper order test: `POST /orders/place` with `confirm_live=false`
7. Start continuous scanner: `POST /auto-trader/start`
8. Watch saved opportunities: `GET /opportunities`, `GET /opportunities/performance`
9. Study failures: `POST /opportunities/evaluate-open`, `GET /opportunities/failure-analysis`
10. Live orders only after validation: set `LIVE_TRADING_MODE=true`, `PAPER_TRADING_MODE=false`, and send `confirm_live=true`

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


def get_kite_provider() -> KiteProvider:
    provider = KiteProvider()
    existing_token = load_access_token()
    if existing_token:
        provider.set_access_token(existing_token)
    return provider


def get_scanner_service() -> ScannerService:
    return ScannerService()


def get_order_service() -> OrderService:
    return OrderService(kite_provider=get_kite_provider(), paper_trading_service=paper_trading_service)


auto_trader_service = AutoTraderService(
    scanner_factory=get_scanner_service,
    order_service_factory=get_order_service,
    opportunity_repository=opportunity_repository,
)
opportunity_outcome_service = OpportunityOutcomeService(
    repository=opportunity_repository,
    kite_provider_factory=get_kite_provider,
)


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
            "GET /scanner/opportunities?side=BUY&symbols=NIFTY,BANKNIFTY&limit=3",
            "GET /scanner/diagnostics?side=BUY&symbols=NIFTY&limit=10",
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
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #101828; }
    header { padding: 18px 24px; background: #ffffff; border-bottom: 1px solid #d0d5dd; display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    h1 { font-size: 22px; margin: 0; }
    h2 { font-size: 16px; margin: 0 0 12px; }
    main { padding: 18px 24px 32px; display: grid; gap: 16px; }
    .control-grid { display: grid; grid-template-columns: minmax(320px, 420px) minmax(520px, 1fr); gap: 16px; align-items: start; }
    .data-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px, 0.65fr); gap: 16px; align-items: start; }
    .panel { background: #ffffff; border: 1px solid #d0d5dd; border-radius: 8px; padding: 14px; min-width: 0; overflow: hidden; }
    .full { grid-column: 1 / -1; }
    .status { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
    .pill { border: 1px solid #d0d5dd; border-radius: 8px; padding: 10px; background: #f9fafb; min-height: 54px; }
    .pill.ok { border-color: #0f766e; background: #ccfbf1; color: #0f766e; }
    .pill.bad { border-color: #b42318; background: #fee4e2; color: #b42318; }
    .pill strong { display: block; font-size: 13px; } .pill span { display: block; font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; }
    label { display: block; font-size: 12px; color: #475467; margin: 10px 0 4px; }
    input, select { width: 100%; box-sizing: border-box; border: 1px solid #d0d5dd; border-radius: 6px; padding: 8px; font: inherit; background: #fff; }
    .row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
    button, .linkbtn { border: 1px solid #175cd3; background: #175cd3; color: white; padding: 9px 10px; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; text-align: center; display: inline-block; }
    button.secondary, .linkbtn.secondary { background: #fff; color: #344054; border-color: #d0d5dd; }
    button.danger { background: #b42318; border-color: #b42318; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .actions button { flex: 1 1 130px; }
    .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .metric { border: 1px solid #eaecf0; border-radius: 8px; padding: 10px; background: #fcfcfd; }
    .metric span { display: block; color: #667085; font-size: 12px; } .metric strong { font-size: 18px; display: block; margin-top: 4px; overflow-wrap: anywhere; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #eaecf0; padding: 8px; text-align: left; vertical-align: top; }
    th { color: #475467; background: #f9fafb; font-weight: 600; position: sticky; top: 0; }
    .tablewrap { min-height: 160px; max-height: 360px; overflow: auto; border: 1px solid #eaecf0; border-radius: 8px; }
    pre { max-height: 300px; overflow: auto; background: #101828; color: #f9fafb; border-radius: 8px; padding: 10px; font-size: 12px; }
    .links { display: grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); gap: 8px; }
    .muted { color: #667085; font-size: 12px; }
    @media (max-width: 1100px) { .control-grid,.data-grid { grid-template-columns: 1fr; } .full { grid-column: auto; } header { align-items: flex-start; flex-direction: column; } }
    @media (max-width: 620px) { main, header { padding-left: 12px; padding-right: 12px; } .status,.metrics,.links,.row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <div><h1>AI Option Trader Command Center</h1><div class="muted" id="refreshText">Loading...</div></div>
    <div class="actions">
      <a class="linkbtn secondary" href="/docs" target="_blank">Docs</a>
      <a class="linkbtn secondary" href="/kite/auth" target="_blank">Kite Login</a>
      <button class="secondary" onclick="refreshAll()">Refresh</button>
    </div>
  </header>
  <main>
    <section class="panel"><div class="status" id="statusCards"></div></section>
    <section class="control-grid">
      <div class="panel">
        <h2>Auto Trade Controls</h2>
        <div class="row"><div><label>Side</label><select id="side"><option>BUY</option><option>SELL</option></select></div><div><label>Limit</label><input id="limit" type="number" value="3" min="1" max="25"></div></div>
        <label>Symbols</label><input id="symbols" value="NIFTY,BANKNIFTY,HDFCBANK">
        <div class="row"><div><label>Scan seconds</label><input id="interval" type="number" value="5" min="3"></div><div><label>Outcome seconds</label><input id="outcomeInterval" type="number" value="30" min="10"></div></div>
        <div class="row"><label><input id="placeOrders" type="checkbox" style="width:auto"> Auto place orders</label><label><input id="confirmLive" type="checkbox" style="width:auto"> Confirm live</label></div>
        <div class="actions">
          <button onclick="startAuto()">Start</button><button class="danger" onclick="stopAuto()">Stop Auto</button><button class="secondary" onclick="stopMonitor()">Stop Monitor</button>
          <button class="secondary" onclick="scanOnce()">Run One Scan</button><button class="secondary" onclick="evaluateOpen()">Evaluate Open</button>
        </div>
        <pre id="actionResult">{}</pre>
      </div>
      <div class="panel">
        <h2>Live Activity</h2>
        <div class="metrics" id="metrics"></div>
        <pre id="activityJson">{}</pre>
      </div>
    </section>
    <section class="panel">
      <h2>Quick Modules</h2>
      <div class="links">
        <a class="linkbtn secondary" href="/scanner/opportunities?side=BUY&symbols=NIFTY,BANKNIFTY&limit=3" target="_blank">Scanner</a>
        <a class="linkbtn secondary" href="/scanner/diagnostics?side=BUY&symbols=NIFTY&limit=10" target="_blank">Diagnostics</a>
        <a class="linkbtn secondary" href="/opportunities?limit=20" target="_blank">Journal</a>
        <a class="linkbtn secondary" href="/opportunities/failure-analysis" target="_blank">Failures</a>
        <a class="linkbtn secondary" href="/paper/positions" target="_blank">Paper</a>
        <a class="linkbtn secondary" href="/kite/margins" target="_blank">Margins</a>
        <a class="linkbtn secondary" href="/kite/positions" target="_blank">Positions</a>
        <a class="linkbtn secondary" href="/db/health" target="_blank">DB</a>
      </div>
    </section>
    <section class="data-grid">
      <div class="panel"><h2>Latest Scan Opportunities</h2><div class="tablewrap"><table id="latestTable"></table></div></div>
      <div class="panel"><h2>Failure Analysis</h2><pre id="failureJson">{}</pre></div>
      <div class="panel"><h2>Saved Opportunity Journal</h2><div class="tablewrap"><table id="journalTable"></table></div></div>
      <div class="panel"><h2>Paper / Execution State</h2><pre id="ordersJson">{}</pre></div>
      <div class="panel full"><h2>Kite Account</h2><pre id="accountJson">{}</pre></div>
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
function pill(label, ok, detail) {
  return `<div class="pill ${ok ? "ok" : "bad"}"><strong>${label}</strong><span>${detail || ""}</span></div>`;
}
function metric(label, value) {
  return `<div class="metric"><span>${label}</span><strong>${value ?? "-"}</strong></div>`;
}
function table(el, rows) {
  const keys = ["id","symbol","action","tradingsymbol","entry_price","stop_loss","target_1","quantity","score","status","outcome"];
  if (!rows || !rows.length) { el.innerHTML = "<tr><td>No rows yet.</td></tr>"; return; }
  el.innerHTML = `<thead><tr>${keys.map(k => `<th>${k}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => `<tr>${keys.map(k => `<td>${r[k] ?? ""}</td>`).join("")}</tr>`).join("") + "</tbody>";
}
function payload() {
  return {
    side: document.getElementById("side").value,
    symbols: document.getElementById("symbols").value,
    interval_seconds: Number(document.getElementById("interval").value),
    limit: Number(document.getElementById("limit").value),
    place_orders: document.getElementById("placeOrders").checked,
    confirm_live: document.getElementById("confirmLive").checked,
    monitor_outcomes: true,
    outcome_interval_seconds: Number(document.getElementById("outcomeInterval").value)
  };
}
async function showAction(fn) {
  const out = document.getElementById("actionResult");
  try { const data = await fn(); out.textContent = JSON.stringify(data, null, 2); await refreshAll(); }
  catch (e) { out.textContent = e.message; }
}
function startAuto(){ showAction(() => postJson("/auto-trader/start", payload())); }
function stopAuto(){ showAction(() => postJson("/auto-trader/stop")); }
function stopMonitor(){ showAction(() => postJson("/opportunity-monitor/stop")); }
function scanOnce(){ showAction(() => postJson("/auto-trader/scan-once", {...payload(), place_orders:false})); }
function evaluateOpen(){ showAction(() => postJson("/opportunities/evaluate-open", {limit:100})); }
async function refreshAll() {
  const [health, db, kite, auto, monitor, perf, latest, journal, failures, execs, paper, margins, positions] = await Promise.allSettled([
    getJson("/health"), getJson("/db/health"), getJson("/kite/health"), getJson("/auto-trader/status"), getJson("/opportunity-monitor/status"),
    getJson("/opportunities/performance"), getJson("/auto-trader/latest"), getJson("/opportunities?limit=50"), getJson("/opportunities/failure-analysis"),
    getJson("/auto-trader/executions"), getJson("/paper/trades"), getJson("/kite/margins"), getJson("/kite/positions")
  ]);
  const val = r => r.status === "fulfilled" ? r.value : {error: r.reason.message};
  const h=val(health), d=val(db), k=val(kite), a=val(auto), m=val(monitor), p=val(perf), l=val(latest), j=val(journal), f=val(failures);
  document.getElementById("statusCards").innerHTML =
    pill("API", h.status === "ok", h.status || h.error) + pill("Database", d.status === "ok", d.status || d.error) +
    pill("Kite", k.status === "ok", k.status || k.error || "check") + pill("Auto Trader", !!a.running, a.running ? "running" : "stopped") +
    pill("Outcome Monitor", !!m.running, m.running ? "running" : "stopped");
  document.getElementById("metrics").innerHTML =
    metric("Last scan", a.last_scan_at || "-") + metric("Latest found", a.latest_count || 0) + metric("Executions", a.execution_count || 0) +
    metric("Open", p.open || 0) + metric("Closed", p.closed || 0) + metric("Win rate", `${((p.win_rate || 0) * 100).toFixed(1)}%`);
  document.getElementById("activityJson").textContent = JSON.stringify({auto_trader:a, monitor:m, performance:p}, null, 2);
  table(document.getElementById("latestTable"), l.opportunities || []);
  table(document.getElementById("journalTable"), j.opportunities || []);
  document.getElementById("failureJson").textContent = JSON.stringify(f, null, 2);
  document.getElementById("ordersJson").textContent = JSON.stringify({executions: val(execs), paper: val(paper)}, null, 2);
  document.getElementById("accountJson").textContent = JSON.stringify({margins: val(margins), positions: val(positions)}, null, 2);
  document.getElementById("refreshText").textContent = `Last refreshed ${new Date().toLocaleTimeString()}`;
}
refreshAll();
setInterval(refreshAll, 10000);
</script>
</body>
</html>
        """
    )


def opportunity_record_to_dict(record) -> dict[str, object]:
    try:
        failure_tags = json.loads(record.failure_tags_json or "[]")
    except json.JSONDecodeError:
        failure_tags = []
    return {
        "id": record.id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
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
        "closed_at": record.closed_at.isoformat() if record.closed_at else None,
        "pnl": record.pnl,
        "failure_tags": failure_tags,
        "review_notes": record.review_notes,
    }


@app.get("/market/{symbol}", tags=["09 Market Data"], summary="Get stored market summary for a symbol")
def get_market_summary(symbol: str) -> dict[str, object]:
    return market_data_service.get_market_summary(symbol)


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
    description="Use this for manual scanning. Example: `/scanner/opportunities?side=BUY&symbols=NIFTY,BANKNIFTY,HDFCBANK&limit=3`.",
)
def get_opportunities(side: str = "BUY", symbols: str | None = None, limit: int = 10) -> dict[str, object]:
    scanner_service = get_scanner_service()
    symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
    recommendations = scanner_service.scan_symbols(symbols=symbol_list, side=side.upper())
    saved_ids = [opportunity_repository.save_opportunity(signal).id for signal in recommendations[:limit]]
    return {
        "mode": "live" if settings.live_trading_mode else "paper",
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
def get_scanner_diagnostics(side: str = "BUY", symbols: str | None = None, limit: int = 25) -> dict[str, object]:
    scanner_service = get_scanner_service()
    symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
    diagnostics = scanner_service.scan_with_diagnostics(symbols=symbol_list, side=side.upper())
    rows: list[dict[str, object]] = []
    for item in diagnostics[:limit]:
        row = dict(item)
        signal = row.get("signal")
        row["signal"] = asdict(signal) if signal is not None else None
        rows.append(row)
    return {
        "mode": "live" if settings.live_trading_mode else "paper",
        "market_data": type(scanner_service.feed).__name__,
        "kite_access_token": bool(load_access_token()),
        "use_kite_market_data": settings.use_kite_market_data,
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
            signal_payload = {key: value for key, value in payload.items() if key != "confirm_live"}
        signal = Signal(**signal_payload)  # type: ignore[arg-type]
        confirm_live = bool(payload.get("confirm_live", False))
        return get_order_service().place_signal_order(signal, confirm_live=confirm_live)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
                "side": "BUY",
                "symbols": "NIFTY,BANKNIFTY,HDFCBANK",
                "interval_seconds": 5,
                "limit": 3,
                "place_orders": False,
                "monitor_outcomes": True,
                "outcome_interval_seconds": 30,
            },
            {
                "side": "BUY",
                "symbols": "NIFTY,BANKNIFTY",
                "interval_seconds": 5,
                "limit": 1,
                "place_orders": True,
                "confirm_live": False,
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

    status = auto_trader_service.start(
        side=str(payload.get("side") or "BUY"),
        symbols=symbols,
        interval_seconds=int(payload.get("interval_seconds") or settings.scanner_interval_seconds),
        limit=int(payload.get("limit") or 5),
        place_orders=bool(payload.get("place_orders", False)),
        confirm_live=bool(payload.get("confirm_live", False)),
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


@app.post(
    "/opportunities/evaluate-open",
    tags=["07 Outcome Monitor"],
    summary="Evaluate open opportunities once",
    description="Fetches current option prices and auto-marks stop/target/expired outcomes where possible.",
)
def evaluate_open_opportunities(
    payload: dict[str, object] | None = Body(default=None, examples=[{"limit": 100}])
) -> dict[str, object]:
    payload = payload or {}
    return opportunity_outcome_service.evaluate_once(limit=int(payload.get("limit") or 100))


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
                "symbols": "NIFTY,BANKNIFTY",
                "limit": 3,
                "place_orders": False,
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
            "confirm_live": bool(payload.get("confirm_live", False)),
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
