from __future__ import annotations


def render_command_center_dashboard() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Bank Nifty Operator Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      --bg: #f3f5f7; --card: #fff; --ink: #17212b; --muted: #667085;
      --line: #d9dee5; --ok: #087a55; --ok-bg: #eaf8f2; --bad: #b42318;
      --bad-bg: #fff0f0; --warn: #a15c00; --warn-bg: #fff6df;
      --work: #175cd3; --work-bg: #eef4ff; --shadow: 0 8px 28px rgba(23,33,43,.06);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); }
    header {
      position: sticky; top: 0; z-index: 10; display: flex; align-items: center;
      justify-content: space-between; gap: 16px; padding: 14px 24px;
      background: rgba(255,255,255,.96); border-bottom: 1px solid var(--line);
      backdrop-filter: blur(10px);
    }
    h1 { margin: 0; font-size: 20px; } h2 { margin: 0; font-size: 15px; }
    main { max-width: 1500px; margin: 0 auto; padding: 18px 24px 36px; display: grid; gap: 14px; }
    .eyebrow { color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    .muted { color: var(--muted); font-size: 12px; }
    .hero, .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); }
    .hero { padding: 18px 20px; border-left: 7px solid var(--work); display: grid; gap: 10px; }
    .hero.ok { border-left-color: var(--ok); } .hero.warn { border-left-color: var(--warn); }
    .hero.bad { border-left-color: var(--bad); } .hero.work { border-left-color: var(--work); }
    .hero-row { display: flex; justify-content: space-between; align-items: start; gap: 18px; }
    .hero h2 { font-size: 26px; line-height: 1.15; margin-top: 4px; }
    .hero p { margin: 6px 0 0; color: #344054; }
    .badge { border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; font-size: 12px; font-weight: 700; white-space: nowrap; }
    .badge.ok { color: var(--ok); background: var(--ok-bg); border-color: #a7dfcb; }
    .badge.bad { color: var(--bad); background: var(--bad-bg); border-color: #ffc4c4; }
    .badge.warn { color: var(--warn); background: var(--warn-bg); border-color: #efd38b; }
    .badge.work { color: var(--work); background: var(--work-bg); border-color: #b9cff8; }
    .chips { display: flex; flex-wrap: wrap; gap: 7px; }
    .chip { border: 1px solid #e2e6eb; background: #fafbfc; color: #344054; border-radius: 999px; padding: 5px 9px; font-size: 12px; overflow-wrap: anywhere; }
    .grid-2 { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(340px, .9fr); gap: 14px; align-items: start; }
    .card { padding: 15px; min-width: 0; overflow: hidden; }
    .card-head { display: flex; justify-content: space-between; align-items: start; gap: 12px; margin-bottom: 12px; }
    .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .tile { border: 1px solid #e4e7ec; background: #fafbfc; border-radius: 9px; padding: 10px; min-height: 76px; }
    .tile.ok { background: var(--ok-bg); border-color: #b8e5d4; }
    .tile.bad { background: var(--bad-bg); border-color: #ffcaca; }
    .tile.warn { background: var(--warn-bg); border-color: #efd99f; }
    .tile.work { background: var(--work-bg); border-color: #c6d7fb; }
    .tile label { display: block; color: var(--muted); font-size: 11px; margin-bottom: 5px; }
    .tile strong { display: block; font-size: 14px; overflow-wrap: anywhere; }
    .tile small { display: block; color: #596579; margin-top: 4px; line-height: 1.3; overflow-wrap: anywhere; }
    .metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
    .metric { border: 1px solid #e4e7ec; border-radius: 9px; padding: 10px; background: #fafbfc; min-height: 74px; }
    .metric span { display: block; color: var(--muted); font-size: 11px; }
    .metric strong { display: block; margin-top: 5px; font-size: 18px; overflow-wrap: anywhere; }
    .notice { border-radius: 9px; border: 1px solid #e4e7ec; background: #fafbfc; padding: 10px 12px; color: #344054; font-size: 13px; line-height: 1.45; }
    .notice.ok { background: var(--ok-bg); border-color: #b8e5d4; }
    .notice.bad { background: var(--bad-bg); border-color: #ffcaca; color: #8d1b14; }
    .notice.warn { background: var(--warn-bg); border-color: #efd99f; color: #7a4800; }
    .stack { display: grid; gap: 8px; }
    .table-wrap { overflow: auto; border: 1px solid #e4e7ec; border-radius: 9px; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; min-width: 760px; }
    th, td { text-align: left; padding: 9px; border-bottom: 1px solid #eaecf0; vertical-align: top; overflow-wrap: anywhere; }
    th { background: #f8f9fb; color: #475467; font-weight: 700; }
    tr:last-child td { border-bottom: 0; }
    .focus { border: 1px solid #d8e2f1; border-radius: 10px; padding: 12px; background: #f8fbff; display: grid; gap: 8px; }
    .focus-title { font-size: 17px; font-weight: 750; overflow-wrap: anywhere; }
    .kv { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .kv div { border-top: 1px solid #dce5ef; padding-top: 7px; }
    .kv span { color: var(--muted); display: block; font-size: 11px; }
    .kv strong { display: block; margin-top: 3px; overflow-wrap: anywhere; }
    .feed { display: grid; gap: 7px; max-height: 430px; overflow: auto; padding-right: 3px; }
    .event { border: 1px solid #e4e7ec; border-left: 4px solid #9aa4b2; border-radius: 8px; padding: 9px 10px; background: #fbfcfd; }
    .event.ok { border-left-color: var(--ok); } .event.bad { border-left-color: var(--bad); }
    .event.warn { border-left-color: var(--warn); } .event.work { border-left-color: var(--work); }
    .event-head { display: flex; justify-content: space-between; gap: 10px; }
    .event strong { font-size: 12px; overflow-wrap: anywhere; } .event time { color: var(--muted); font-size: 11px; white-space: nowrap; }
    .event p { margin: 5px 0 0; color: #475467; font-size: 12px; line-height: 1.35; overflow-wrap: anywhere; }
    .links { display: grid; grid-template-columns: repeat(8, minmax(100px, 1fr)); gap: 7px; }
    .links a { color: #344054; background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 9px; text-align: center; text-decoration: none; font-size: 12px; font-weight: 650; }
    .links a:hover { border-color: var(--work); color: var(--work); }
    .empty { color: var(--muted); padding: 12px 2px; font-size: 13px; }
    @media (max-width: 1050px) { .grid-2 { grid-template-columns: 1fr; } .tiles { grid-template-columns: repeat(2, 1fr); } .links { grid-template-columns: repeat(4, 1fr); } }
    @media (max-width: 650px) { header, main { padding-left: 12px; padding-right: 12px; } .hero-row { display: grid; } .tiles, .metrics, .kv { grid-template-columns: repeat(2, 1fr); } .links { grid-template-columns: repeat(2, 1fr); } }
  </style>
</head>
<body>
<header>
  <div><div class="eyebrow">Bank Nifty option buying</div><h1>Operator Dashboard</h1></div>
  <div class="muted" id="refreshText">Loading live state…</div>
</header>
<main>
  <section class="hero work" id="operatorBanner" aria-live="polite">
    <div class="hero-row">
      <div><div class="eyebrow">Operational truth</div><h2 id="operatorTitle">Checking the application…</h2><p id="operatorSummary">Waiting for verified runtime state.</p></div>
      <span class="badge work" id="operatorBadge">CHECKING</span>
    </div>
    <div class="chips" id="operatorChips"></div>
  </section>

  <section class="grid-2">
    <article class="card">
      <div class="card-head"><div><div class="eyebrow">Expected vs actual</div><h2>Critical Runtime</h2></div><span class="muted">15-second refresh</span></div>
      <div class="tiles" id="serviceGrid"></div>
    </article>
    <article class="card">
      <div class="card-head"><div><div class="eyebrow">Manual attention</div><h2>Action Required</h2></div></div>
      <div class="stack" id="actionPanel"><div class="empty">Checking for operational failures…</div></div>
    </article>
  </section>

  <article class="card">
    <div class="card-head"><div><div class="eyebrow">Actual executions, not saved ideas</div><h2>Today’s Risk &amp; Trading</h2></div><span class="badge work" id="riskBadge">CHECKING</span></div>
    <div class="metrics" id="riskMetrics"></div>
    <div id="riskNotice" style="margin-top:10px"></div>
  </article>

  <article class="card">
    <div class="card-head"><div><div class="eyebrow">Every open lifecycle position</div><h2>Open Trades &amp; Exit Alerts</h2></div><span class="badge work" id="tradeBadge">CHECKING</span></div>
    <div id="tradePanel"></div>
  </article>

  <section class="grid-2">
    <article class="card">
      <div class="card-head"><div><div class="eyebrow">Current, time-qualified evidence</div><h2>Setup &amp; Armed Entry</h2></div></div>
      <div class="stack" id="setupPanel"></div>
    </article>
    <article class="card">
      <div class="card-head"><div><div class="eyebrow">One canonical feed view</div><h2>Market Data Health</h2></div></div>
      <div class="tiles" id="dataGrid"></div>
    </article>
  </section>

  <section class="grid-2">
    <article class="card">
      <div class="card-head"><div><div class="eyebrow">Completion and evidence honesty</div><h2>Research &amp; Notifications</h2></div></div>
      <div class="stack" id="researchPanel"></div>
    </article>
    <article class="card">
      <div class="card-head"><div><div class="eyebrow">Last ten meaningful decisions</div><h2>Recent Decisions</h2></div></div>
      <div class="feed" id="decisionFeed"></div>
    </article>
  </section>

  <article class="card">
    <div class="card-head"><div><div class="eyebrow">Open detail only when needed</div><h2>Useful Details</h2></div></div>
    <nav class="links">
      <a href="/docs" target="_blank">API Docs</a>
      <a href="/trades?limit=100" target="_blank">Trades</a>
      <a href="/trades/exit-alerts" target="_blank">Exit Alerts</a>
      <a href="/broker/reconciliation/status" target="_blank">Reconciliation</a>
      <a href="/automation/status" target="_blank">Automation</a>
      <a href="/market-data/pipeline-status" target="_blank">Data Pipeline</a>
      <a href="/research/after-market/status" target="_blank">Research</a>
      <a href="/dashboard/decision-feed?limit=50" target="_blank">Decision Feed</a>
    </nav>
  </article>
</main>
<script>
const DASHBOARD_REFRESH_MS = 15000;
const DASHBOARD_SLOW_REFRESH_MS = 60000;
const state = { core: {}, slow: {}, coreBusy: false, slowBusy: false };

const esc = value => String(value ?? "—").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const cls = level => ["ok","bad","warn","work"].includes(level) ? level : "work";
const money = value => Number.isFinite(Number(value)) ? `₹${Number(value).toLocaleString("en-IN", {maximumFractionDigits: 2})}` : "—";
const number = (value, digits=1) => Number.isFinite(Number(value)) ? Number(value).toLocaleString("en-IN", {maximumFractionDigits: digits}) : "—";
const boolText = value => value ? "Yes" : "No";
const isError = value => !value || Boolean(value.__error);
const pick = (...values) => values.find(value => value !== undefined && value !== null && value !== "") ?? null;

async function getJson(path, timeoutMs=6000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {signal: controller.signal, cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } catch (error) {
    return {__error: error?.name === "AbortError" ? "request timed out" : String(error?.message || error)};
  } finally { clearTimeout(timer); }
}

function tile(label, level, value, detail="") {
  return `<div class="tile ${cls(level)}"><label>${esc(label)}</label><strong>${esc(value)}</strong>${detail ? `<small>${esc(detail)}</small>` : ""}</div>`;
}
function badge(element, level, text) { element.className = `badge ${cls(level)}`; element.textContent = text; }
function notice(level, text) { return `<div class="notice ${cls(level)}">${esc(text)}</div>`; }
function eventLevel(value) { return value === "error" || value === "bad" ? "bad" : value === "warn" || value === "warning" ? "warn" : value === "ok" ? "ok" : "work"; }
function ownerHasCore(ws) {
  const owners = ws?.subscription_owners || {};
  return Object.values(owners).some(items => Array.isArray(items) && items.includes("core_market"));
}
function maxTickAge(ws) {
  const values = Object.values(ws?.latest_tick_age || {}).map(Number).filter(Number.isFinite);
  return values.length ? Math.max(...values) : null;
}
function ageLabel(value) {
  if (!value) return "time unavailable";
  const normalized = String(value).replace(" IST", "+05:30").replace(" ", "T");
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const seconds = Math.max(0, Math.round((Date.now() - parsed.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds/60)}m ago`;
  return `${Math.floor(seconds/3600)}h ago`;
}

function renderOperator() {
  const {runtime, automation, risk, pipeline, reconciliation, exitAlerts} = state.core;
  const {db, kite, strategy} = state.slow;
  const session = runtime?.session || {};
  const operator = automation?.operator_state || {};
  const ws = pipeline?.websocket || {};
  const liveExpected = Boolean(session.should_run_live_modules);
  const problems = [];
  if (isError(runtime)) problems.push(`Runtime API unavailable: ${runtime?.__error || "unknown"}`);
  if (isError(automation)) problems.push(`Automation status unavailable: ${automation?.__error || "unknown"}`);
  if (db && db.status !== "ok") problems.push(`Database check failed: ${pick(db.message, db.error_type, db.__error) || "unknown"}`);
  if (strategy?.config_drift_detected) problems.push("Active strategy configuration drift was detected");
  if (operator.action_required) problems.push(operator.summary || "Automation requires attention");
  if (reconciliation?.blocked) problems.push(`Broker reconciliation blocked: ${reconciliation.reason || "mismatch"}`);
  if ((exitAlerts?.alerts || []).length) problems.push(`${exitAlerts.alerts.length} unresolved exit alert(s)`);
  if (liveExpected) {
    if (kite && kite.status !== "ok") problems.push(`Kite login is not healthy: ${pick(kite.message, kite.status, kite.__error)}`);
    if (!ws.websocket_connected) problems.push(`WebSocket is not connected: ${pick(ws.last_error_reason, ws.websocket_status, "unknown")}`);
    if (!ownerHasCore(ws)) problems.push("Bank Nifty core-market subscription is not verified");
    if (ws?.data_gap?.active_gap) problems.push("An active market-data gap is blocking entry");
    if (risk && risk.passed === false) problems.push(`Risk is blocking entry: ${(risk.reasons || []).join("; ") || "unknown"}`);
    if (operator.code !== "TRADING_ACTIVE") problems.push(`Trading automation state is ${operator.label || operator.code || "unknown"}`);
  }

  let level = "work", title = operator.label || "Checking runtime", summary = operator.summary || "Waiting for application state";
  if (problems.length) { level = "bad"; title = "Action required"; summary = problems[0]; }
  else if (operator.code === "DAY_COMPLETE") { level = "ok"; title = "Day complete — no trading is active"; summary = operator.summary; }
  else if (liveExpected) { level = "ok"; title = "Trading runtime is operational"; summary = "Required services, data and risk checks are currently passing."; }
  else if (operator.severity === "ok") { level = "ok"; }
  else if (operator.severity === "warning") { level = "warn"; }

  const banner = document.getElementById("operatorBanner"); banner.className = `hero ${level}`;
  document.getElementById("operatorTitle").textContent = title;
  document.getElementById("operatorSummary").textContent = summary;
  badge(document.getElementById("operatorBadge"), level, problems.length ? "CHECK NOW" : (operator.code || "VERIFYING").replaceAll("_", " "));
  document.getElementById("operatorChips").innerHTML = [
    `Mode: ${(automation?.config?.order_mode || "unknown").toUpperCase()}`,
    `Session: ${session.runtime_mode || automation?.runtime_mode || "unknown"}`,
    `Role: ${(automation?.execution_profile || "unknown").replaceAll("_", " ")}`,
    `Strategy: ${strategy?.version?.version || "loading"}`,
    `Supervisor heartbeat: ${ageLabel(automation?.last_cycle_at)}`
  ].map(value => `<span class="chip">${esc(value)}</span>`).join("");
  renderActions(problems);
}

function renderServices() {
  const {runtime, automation, pipeline} = state.core; const {db, kite} = state.slow;
  const session = runtime?.session || {}; const liveExpected = Boolean(session.should_run_live_modules);
  const services = automation?.services || {}; const ws = pipeline?.websocket || {};
  const supervisorHealthy = Boolean(automation?.running && automation?.lifecycle?.task_healthy);
  const idleOk = !liveExpected;
  document.getElementById("serviceGrid").innerHTML = [
    tile("API", isError(runtime) ? "bad" : "ok", isError(runtime) ? "Unavailable" : "Responding", runtime?.timestamp || runtime?.__error),
    tile("Database", !db ? "work" : db.status === "ok" ? "ok" : "bad", !db ? "Checking" : db.status === "ok" ? "Connected" : "Failed", db?.error_type || "durable state"),
    tile("Kite login", !kite ? "work" : kite.status === "ok" ? "ok" : "bad", !kite ? "Checking" : kite.status === "ok" ? "Verified" : "Failed", kite?.auth?.status || kite?.message),
    tile("WebSocket", liveExpected ? (ws.websocket_connected ? "ok" : "bad") : "ok", liveExpected ? (ws.websocket_connected ? "Connected" : "Disconnected") : "Idle as scheduled", liveExpected ? (ownerHasCore(ws) ? "core feed verified" : "core feed missing") : session.runtime_mode),
    tile("Supervisor", supervisorHealthy ? "ok" : "bad", automation?.operator_state?.label || (supervisorHealthy ? "Running" : "Unhealthy"), automation?.execution_profile || "unknown role"),
    tile("Scanner", services.auto_trader?.running ? "ok" : idleOk ? "ok" : "bad", services.auto_trader?.running ? "Running" : idleOk ? "Idle as scheduled" : "Stopped", ageLabel(services.auto_trader?.last_scan_at)),
    tile("Collector", services.collector?.running ? "ok" : idleOk ? "ok" : "bad", services.collector?.running ? "Running" : idleOk ? "Idle as scheduled" : "Stopped", `${services.collector?.error_count || 0} errors`),
    tile("Exit monitor", services.outcome_monitor?.running ? "ok" : idleOk ? "ok" : "bad", services.outcome_monitor?.running ? "Running" : idleOk ? "Idle as scheduled" : "Stopped", `${services.outcome_monitor?.error_count || 0} errors`)
  ].join("");
}

function renderActions(initialProblems) {
  const problems = [...initialProblems];
  const automation = state.core.automation || {}; const latest = state.core.latest || {};
  for (const item of (automation.recent_errors || []).slice(-3)) problems.push(`${item.source || "Automation"}: ${item.error || item.message || "failure"}`);
  for (const item of (latest.status?.recent_errors || []).slice(-2)) problems.push(`Scanner: ${item.error || item.message || "failure"}`);
  const unique = [...new Set(problems.filter(Boolean))];
  document.getElementById("actionPanel").innerHTML = unique.length
    ? unique.slice(0, 8).map(item => notice("bad", item)).join("")
    : notice("ok", "No current operational issue requires manual action.");
}

function renderRisk() {
  const risk = state.core.risk || {}; const summary = risk.summary || {}; const riskState = risk.risk_state || {};
  const level = isError(risk) || risk.passed === false ? "bad" : "ok";
  badge(document.getElementById("riskBadge"), level, isError(risk) ? "UNAVAILABLE" : risk.passed ? "WITHIN LIMITS" : "BLOCKED");
  document.getElementById("riskMetrics").innerHTML = [
    ["Trades today", summary.trades ?? "—"], ["Open trades", summary.open ?? "—"],
    ["Realized P&L", money(riskState.realized_daily_pnl ?? summary.pnl)], ["Unrealized P&L", money(riskState.unrealized_daily_pnl)],
    ["Open risk", `${number(riskState.total_open_risk_pct)}%`]
  ].map(([label,value]) => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
  const reasons = (risk.reasons || []).join("; ");
  document.getElementById("riskNotice").innerHTML = isError(risk)
    ? notice("bad", `Risk status unavailable: ${risk.__error}`)
    : risk.passed ? notice("ok", `Risk checks pass. Drawdown ${number(riskState.current_drawdown_pct)}%; planned risk today ${number(riskState.planned_risk_today_pct)}%.`)
    : notice("bad", reasons || "Risk management is blocking new entry.");
}

function renderTrades() {
  const trades = state.core.trades || {}; const exitAlerts = state.core.exitAlerts || {};
  const rows = trades.trades || []; const alerts = exitAlerts.alerts || [];
  badge(document.getElementById("tradeBadge"), alerts.length ? "bad" : "ok", alerts.length ? `${alerts.length} EXIT ALERTS` : `${rows.length} OPEN`);
  let html = rows.length ? `<div class="table-wrap"><table><thead><tr><th>Contract</th><th>Mode</th><th>Qty</th><th>Status</th><th>Entry</th><th>Executable exit</th><th>Stop</th><th>Target</th><th>P&amp;L</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(pick(row.tradingsymbol,row.symbol))}</td><td>${esc(pick(row.order_mode,row.mode))}</td><td>${esc(row.quantity)}</td><td>${esc(row.status)}</td><td>${esc(number(pick(row.entry_price,row.entry_fill_price),2))}</td><td>${esc(number(pick(row.exit_executable_price,row.current_price,row.ltp),2))}</td><td>${esc(number(pick(row.stop_loss,row.stop_price),2))}</td><td>${esc(number(pick(row.target_1,row.target_price),2))}</td><td>${esc(money(pick(row.unrealized_pnl,row.pnl)))}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No open trades.</div>`;
  if (alerts.length) html += `<div class="stack" style="margin-top:10px">${alerts.map(item => notice("bad", `${pick(item.tradingsymbol,item.symbol,"Trade")}: ${pick(item.message,item.reason,item.alert_type,"exit issue")}`)).join("")}</div>`;
  document.getElementById("tradePanel").innerHTML = html;
}

function renderSetup() {
  const latest = state.core.latest || {}; const armed = state.core.armed || {}; const status = latest.status || {};
  const active = armed.active || []; const opportunity = (latest.opportunities || [])[0];
  let html = "";
  if (active.length) {
    html += active.slice(0,3).map(item => `<div class="focus"><div class="chips"><span class="badge work">ARMED</span><span class="chip">${esc(ageLabel(pick(item.updated_at,item.armed_at,item.created_at)))}</span></div><div class="focus-title">${esc(pick(item.tradingsymbol,item.symbol,"Bank Nifty setup"))}</div><div class="kv"><div><span>Trigger</span><strong>${esc(number(pick(item.trigger_price,item.entry_trigger),2))}</strong></div><div><span>Bid / Ask</span><strong>${esc(`${number(item.bid,2)} / ${number(item.ask,2)}`)}</strong></div><div><span>State</span><strong>${esc(pick(item.subscription_state,item.state,"armed"))}</strong></div></div></div>`).join("");
  } else if (opportunity) {
    html = `<div class="focus"><div class="chips"><span class="badge warn">LATEST WATCH</span><span class="chip">${esc(ageLabel(pick(opportunity.created_at,opportunity.timestamp,status.last_scan_at)))}</span></div><div class="focus-title">${esc(pick(opportunity.tradingsymbol,opportunity.symbol,"Bank Nifty candidate"))}</div><div class="kv"><div><span>Action</span><strong>${esc(opportunity.action)}</strong></div><div><span>Entry</span><strong>${esc(number(opportunity.entry_price,2))}</strong></div><div><span>Primary reason</span><strong>${esc(pick(opportunity.primary_gate,opportunity.reason,"watching"))}</strong></div></div></div>`;
  } else {
    html = `<div class="empty">No active or armed setup. Last scan: ${esc(ageLabel(status.last_scan_at))}.</div>`;
  }
  html += notice(status.error_count ? "bad" : "ok", status.running ? `Scanner is active; ${status.latest_count || 0} current candidates.` : `Scanner is idle in ${status.mode || "paper"} mode.`);
  document.getElementById("setupPanel").innerHTML = html;
}

function renderData() {
  const pipeline = state.core.pipeline || {}; const runtime = state.core.runtime || {}; const session = runtime.session || {};
  const ws = pipeline.websocket || {}; const candle = pipeline.canonical_underlying_candles || {}; const raw = pipeline.raw_tick_capture || {};
  const liveExpected = Boolean(session.should_run_live_modules); const gap = Boolean(ws?.data_gap?.active_gap); const core = ownerHasCore(ws);
  const drops = Number(ws.event_queue_critical_drop_count || 0) + Number(raw.dropped_risk_count || 0);
  const lastCandle = pick(candle?.coverage?.["1minute"]?.last_completed_candle?.timestamp, candle?.last_completed?.timestamp);
  document.getElementById("dataGrid").innerHTML = [
    tile("WebSocket", liveExpected ? (ws.websocket_connected ? "ok" : "bad") : "ok", liveExpected ? (ws.websocket_connected ? "Connected" : "Disconnected") : "Idle as scheduled", ws.websocket_status || session.runtime_mode),
    tile("Core subscription", liveExpected ? (core ? "ok" : "bad") : "ok", liveExpected ? (core ? "Verified" : "Missing") : "Not required", core ? "core_market owner" : "outside market"),
    tile("Feed gap", gap ? "bad" : "ok", gap ? "ACTIVE" : "Clear", maxTickAge(ws) === null ? "no live tick age" : `max tick age ${number(maxTickAge(ws))}s`),
    tile("Critical drops", drops ? "bad" : "ok", drops ? number(drops,0) : "None", raw.capture_gap_critical ? "raw capture gap" : "queues healthy"),
    tile("1-minute candle", lastCandle ? "ok" : "warn", lastCandle || "Unavailable", lastCandle ? ageLabel(lastCandle) : "canonical coverage"),
    tile("Raw capture", raw.capture_gap_critical ? "bad" : "ok", raw.capture_gap_critical ? "Critical gap" : (raw.running ? "Running" : "Idle"), `${raw.persist_failure_count || 0} persist failures`),
    tile("Event queue", Number(ws.event_queue_critical_drop_count || 0) ? "bad" : "ok", `${ws.event_queue_size || 0} / ${ws.event_queue_capacity || 0}`, `${ws.event_queue_critical_drop_count || 0} critical drops`),
    tile("Data verdict", gap || drops ? "bad" : "ok", gap || drops ? "Entry blocked" : "No critical fault", liveExpected ? "live checks apply" : "market closed")
  ].join("");
}

function renderResearch() {
  const automation = state.core.automation || {}; const research = automation.services?.after_market_research || {};
  const readiness = state.slow.readiness || {}; const alerts = state.slow.alerts || {}; const attempt = alerts.last_attempt || {};
  const job = research.job_run || readiness.job || {}; const complete = automation.operator_state?.day_complete || research.next_action === "research_completed_for_today";
  const readinessLevel = readiness.ready_for_live ? "ok" : readiness.status === "not_ready" ? "warn" : "work";
  const telegramLevel = !alerts.configured ? "warn" : attempt.status === "error" ? "bad" : attempt.status === "sent" ? "ok" : "work";
  document.getElementById("researchPanel").innerHTML = [
    notice(complete ? "ok" : research.last_error ? "bad" : "work", complete ? `After-market research completed for ${job.trading_date || "today"}${job.completed_at ? ` at ${job.completed_at}` : ""}.` : research.running ? "After-market research is running." : research.last_error ? `Research failed: ${research.last_error}` : `Next action: ${research.next_action || "waiting"}.`),
    notice(readinessLevel, readiness.ready_for_live ? "Professional readiness evidence currently passes." : `Live readiness is NOT proven: ${readiness.message || readiness.status || "no completed report"}`),
    notice(telegramLevel, !alerts.configured ? "Telegram is not configured; remote alerts cannot be delivered." : attempt.status ? `Last Telegram attempt: ${attempt.kind || "operational"} — ${attempt.status} at ${attempt.attempted_at || "unknown time"}.` : "Telegram is configured; no delivery attempt is recorded in this process yet.")
  ].join("");
}

function renderDecisions() {
  const events = state.core.feed?.events || [];
  document.getElementById("decisionFeed").innerHTML = events.length ? events.map(item => `<div class="event ${eventLevel(item.severity)}"><div class="event-head"><strong>${esc(item.title || item.type)}</strong><time>${esc(item.time || "")}</time></div><p>${esc(item.message || item.primary_gate || "No detail")}</p></div>`).join("") : `<div class="empty">No recent decision events.</div>`;
}

function renderAll() {
  try { renderOperator(); renderServices(); renderRisk(); renderTrades(); renderSetup(); renderData(); renderResearch(); renderDecisions(); }
  catch (error) { document.getElementById("actionPanel").innerHTML = notice("bad", `Dashboard rendering failed: ${error?.message || error}`); }
  document.getElementById("refreshText").textContent = `Updated ${new Date().toLocaleTimeString("en-IN", {timeZone:"Asia/Kolkata"})} IST`;
}

async function refreshCore() {
  if (state.coreBusy || document.hidden) return; state.coreBusy = true;
  const paths = {
    runtime:"/runtime/status", automation:"/automation/status", risk:"/risk/status",
    trades:"/trades?status=open&limit=25", exitAlerts:"/trades/exit-alerts",
    reconciliation:"/broker/reconciliation/status", pipeline:"/market-data/pipeline-status",
    latest:"/auto-trader/latest", armed:"/scanner/armed-entries",
    feed:"/dashboard/decision-feed?limit=10"
  };
  const values = await Promise.all(Object.values(paths).map(path => getJson(path)));
  state.core = Object.fromEntries(Object.keys(paths).map((key,index) => [key,values[index]]));
  state.coreBusy = false; renderAll();
}

async function refreshSlow() {
  if (state.slowBusy || document.hidden) return; state.slowBusy = true;
  const paths = { db:"/db/health", kite:"/kite/health", strategy:"/strategy/versions/current", readiness:"/research/professional-readiness", alerts:"/alerts/status" };
  const values = await Promise.all(Object.values(paths).map(path => getJson(path)));
  state.slow = Object.fromEntries(Object.keys(paths).map((key,index) => [key,values[index]]));
  state.slowBusy = false; renderAll();
}

refreshCore(); refreshSlow();
setInterval(refreshCore, DASHBOARD_REFRESH_MS);
setInterval(refreshSlow, DASHBOARD_SLOW_REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshCore(); refreshSlow(); } });
</script>
</body>
</html>
"""
