import os
import sys
from pathlib import Path
from typing import Any

import requests
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.time_utils import ist_now


API_BASE_URL = os.getenv("DASHBOARD_API_BASE_URL", "http://localhost:8000").rstrip("/")


st.set_page_config(page_title="AI Option Trader", layout="wide")


def api_get(path: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    try:
        response = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=12)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, str(exc)


def api_post(path: str, payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    try:
        response = requests.post(f"{API_BASE_URL}{path}", json=payload or {}, timeout=20)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, str(exc)


def money(value: Any) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def number(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)


def status_pill(label: str, good: bool, detail: str = "") -> None:
    color = "#0f766e" if good else "#b42318"
    background = "#ccfbf1" if good else "#fee4e2"
    st.markdown(
        f"""
        <div class="status-pill" style="border-color:{color};background:{background};color:{color};">
            <strong>{label}</strong>{f"<span>{detail}</span>" if detail else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_signal_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.info("No rows to show yet.")
        return
    table_rows = []
    for item in rows:
        table_rows.append(
            {
                "ID": item.get("id"),
                "Symbol": item.get("symbol"),
                "Action": item.get("action"),
                "Contract": item.get("tradingsymbol"),
                "Entry": item.get("entry_price"),
                "SL": item.get("stop_loss"),
                "T1": item.get("target_1"),
                "Qty": item.get("quantity"),
                "Score": item.get("score"),
                "Status": item.get("status"),
                "Outcome": item.get("outcome"),
            }
        )
    st.dataframe(table_rows, use_container_width=True, hide_index=True)


st.markdown(
    """
    <style>
    .block-container { padding-top: 1.2rem; }
    .status-pill {
        min-height: 54px;
        border: 1px solid;
        border-radius: 8px;
        padding: 10px 12px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 2px;
        font-size: 0.9rem;
    }
    .status-pill span { font-size: 0.78rem; opacity: 0.85; }
    .small-note { color: #475467; font-size: 0.85rem; }
    div[data-testid="stMetricValue"] { font-size: 1.45rem; }
    .quick-link a {
        display: block;
        padding: 9px 10px;
        border: 1px solid #d0d5dd;
        border-radius: 8px;
        color: #344054;
        text-decoration: none;
        margin-bottom: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


st.title("AI Option Trader Command Center")
st.caption(f"Connected API: {API_BASE_URL} | Refreshed: {ist_now().strftime('%H:%M:%S IST')}")

health, health_error = api_get("/health")
db_health, db_error = api_get("/db/health")
kite_health, kite_error = api_get("/kite/health")
auto_status, auto_error = api_get("/auto-trader/status")
monitor_status, monitor_error = api_get("/opportunity-monitor/status")
performance, performance_error = api_get("/opportunities/performance")

top_cols = st.columns(5)
with top_cols[0]:
    status_pill("API", health_error is None and isinstance(health, dict) and health.get("status") == "ok", health_error or "online")
with top_cols[1]:
    status_pill("Database", db_error is None and isinstance(db_health, dict) and db_health.get("status") == "ok", db_error or "connected")
with top_cols[2]:
    status_pill("Kite", kite_error is None and isinstance(kite_health, dict) and kite_health.get("status") == "ok", kite_error or str((kite_health or {}).get("status", "unknown")))
with top_cols[3]:
    status_pill("Auto Trader", bool((auto_status or {}).get("running")), "running" if (auto_status or {}).get("running") else "stopped")
with top_cols[4]:
    status_pill("Outcome Monitor", bool((monitor_status or {}).get("running")), "running" if (monitor_status or {}).get("running") else "stopped")

st.divider()

control_col, metrics_col, links_col = st.columns([1.15, 1.25, 0.8], gap="large")

with control_col:
    st.subheader("Auto Trade Controls")
    with st.form("auto_trader_form"):
        side = st.selectbox("Side", ["BUY", "SELL"], index=0)
        symbols = st.text_input("Symbols", value="BANKNIFTY")
        interval_seconds = st.number_input("Scan interval seconds", min_value=3, max_value=300, value=5, step=1)
        limit = st.number_input("Max opportunities per scan", min_value=1, max_value=25, value=3, step=1)
        place_orders = st.toggle("Auto place orders", value=False)
        confirm_live = st.toggle("Confirm live orders", value=False, disabled=not place_orders)
        monitor_outcomes = st.toggle("Monitor outcomes", value=True)
        outcome_interval_seconds = st.number_input("Outcome check seconds", min_value=10, max_value=600, value=30, step=5)
        start_clicked = st.form_submit_button("Start Auto Trader", use_container_width=True)

    if start_clicked:
        payload = {
            "side": side,
            "symbols": symbols,
            "interval_seconds": int(interval_seconds),
            "limit": int(limit),
            "place_orders": bool(place_orders),
            "confirm_live": bool(confirm_live),
            "monitor_outcomes": bool(monitor_outcomes),
            "outcome_interval_seconds": int(outcome_interval_seconds),
        }
        result, error = api_post("/auto-trader/start", payload)
        if error:
            st.error(error)
        else:
            st.success("Auto trader started.")
            st.json(result)

    stop_cols = st.columns(2)
    with stop_cols[0]:
        if st.button("Stop Auto Trader", use_container_width=True):
            result, error = api_post("/auto-trader/stop")
            st.error(error) if error else st.success("Auto trader stopped.")
    with stop_cols[1]:
        if st.button("Stop Monitor", use_container_width=True):
            result, error = api_post("/opportunity-monitor/stop")
            st.error(error) if error else st.success("Outcome monitor stopped.")

    manual_cols = st.columns(2)
    with manual_cols[0]:
        if st.button("Run One Scan", use_container_width=True):
            result, error = api_post(
                "/auto-trader/scan-once",
                {"side": side, "symbols": symbols, "limit": int(limit), "place_orders": False},
            )
            st.error(error) if error else st.json(result)
    with manual_cols[1]:
        if st.button("Evaluate Open", use_container_width=True):
            result, error = api_post("/opportunities/evaluate-open", {"limit": 100})
            st.error(error) if error else st.json(result)

with metrics_col:
    st.subheader("Live Activity")
    status = auto_status or {}
    monitor = monitor_status or {}
    perf = performance or {}
    metric_cols = st.columns(3)
    metric_cols[0].metric("Last scan", status.get("last_scan_at") or "-")
    metric_cols[1].metric("Latest found", status.get("latest_count", 0))
    metric_cols[2].metric("Auto executions", status.get("execution_count", 0))
    metric_cols_2 = st.columns(3)
    metric_cols_2[0].metric("Open ideas", perf.get("open", 0))
    metric_cols_2[1].metric("Closed", perf.get("closed", 0))
    metric_cols_2[2].metric("Win rate", f"{float(perf.get('win_rate', 0)) * 100:.1f}%")
    st.caption("Live order guard: real Zerodha orders require live env flags plus `confirm_live=true`.")
    st.json({"auto_trader": status, "outcome_monitor": monitor}, expanded=False)

with links_col:
    st.subheader("Modules")
    links = [
        ("Swagger Docs", "/docs"),
        ("Kite Login", "/kite/auth"),
        ("Kite Health", "/kite/health"),
        ("Scanner", "/scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3"),
        ("Diagnostics", "/scanner/diagnostics?side=BUY&symbols=BANKNIFTY&limit=10"),
        ("Opportunities", "/opportunities?limit=20"),
        ("Failure Analysis", "/opportunities/failure-analysis"),
        ("Paper Positions", "/paper/positions"),
    ]
    for label, path in links:
        st.markdown(f'<div class="quick-link"><a href="{API_BASE_URL}{path}" target="_blank">{label}</a></div>', unsafe_allow_html=True)

tab_latest, tab_journal, tab_failures, tab_orders, tab_account = st.tabs(
    ["Latest Scan", "Opportunity Journal", "Failure Analysis", "Orders & Paper", "Account"]
)

with tab_latest:
    st.subheader("Latest Auto-Trader Opportunities")
    latest, latest_error = api_get("/auto-trader/latest")
    if latest_error:
        st.error(latest_error)
    else:
        opportunities = (latest or {}).get("opportunities", [])
        render_signal_table(opportunities)
        with st.expander("Raw latest payload"):
            st.json(latest)

with tab_journal:
    st.subheader("Saved Opportunities")
    filter_cols = st.columns([0.4, 0.4, 0.2])
    status_filter = filter_cols[0].selectbox("Status filter", ["all", "open", "closed"], index=0)
    journal_limit = filter_cols[1].number_input("Rows", min_value=5, max_value=500, value=50, step=5)
    params = {"limit": int(journal_limit)}
    if status_filter != "all":
        params["status"] = status_filter
    journal, journal_error = api_get("/opportunities", params=params)
    if journal_error:
        st.error(journal_error)
    else:
        render_signal_table((journal or {}).get("opportunities", []))
        with st.expander("Raw journal payload"):
            st.json(journal)

    st.markdown("#### Manually Mark Outcome")
    outcome_cols = st.columns([0.2, 0.25, 0.25, 0.3])
    opportunity_id = outcome_cols[0].number_input("ID", min_value=1, value=1, step=1)
    outcome = outcome_cols[1].selectbox("Outcome", ["stop_loss", "target_1", "target_2", "target_3", "false_signal", "expired"])
    exit_price = outcome_cols[2].number_input("Exit price", min_value=0.0, value=0.0, step=0.05)
    review_notes = outcome_cols[3].text_input("Notes", value="")
    if st.button("Save Outcome", use_container_width=True):
        payload = {"outcome": outcome, "exit_price": exit_price, "review_notes": review_notes}
        result, error = api_post(f"/opportunities/{int(opportunity_id)}/outcome", payload)
        st.error(error) if error else st.success("Outcome saved.")

with tab_failures:
    st.subheader("Failure Analysis")
    failures, failures_error = api_get("/opportunities/failure-analysis")
    if failures_error:
        st.error(failures_error)
    else:
        tag_counts = (failures or {}).get("top_failure_tags", {})
        if tag_counts:
            st.bar_chart(tag_counts)
        else:
            st.info("No failed opportunities have been tagged yet.")
        st.json(failures)

with tab_orders:
    st.subheader("Auto Executions")
    executions, executions_error = api_get("/auto-trader/executions")
    st.error(executions_error) if executions_error else st.json(executions, expanded=False)

    st.subheader("Paper Trading")
    paper_positions, paper_error = api_get("/paper/trades")
    st.error(paper_error) if paper_error else st.json(paper_positions, expanded=False)

with tab_account:
    st.subheader("Kite Account")
    account_cols = st.columns(3)
    margins, margins_error = api_get("/kite/margins")
    positions, positions_error = api_get("/kite/positions")
    account_cols[0].metric("DB", "OK" if (db_health or {}).get("status") == "ok" else "Check")
    account_cols[1].metric("Kite", (kite_health or {}).get("status", "unknown"))
    account_cols[2].metric("API", (health or {}).get("status", "unknown"))
    st.markdown("##### Margins")
    st.error(margins_error) if margins_error else st.json(margins, expanded=False)
    st.markdown("##### Positions")
    st.error(positions_error) if positions_error else st.json(positions, expanded=False)

st.caption("Tip: keep this dashboard open while auto-trader runs. Use the status cards and Last scan value to confirm activity.")
