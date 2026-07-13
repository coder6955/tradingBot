# Endpoints And Workflow

## Runtime Workflow

1. `GET /health`, `GET /db/health`
2. `GET /kite/health`, `GET /kite/websocket/status`
3. `POST /automation/start` in paper mode
4. `GET /automation/status`
5. Market opens: automation starts snapshot collector, outcome monitor, auto-trader scanner.
6. Scanner: `GET /scanner/opportunities` or automation loop.
7. Accepted opportunities saved; rejected opportunities saved with reasons.
8. Paper/live orders through `POST /orders/place`.
9. Exit monitor through `POST /trades/evaluate-exits` or automation outcome monitor.
10. After close: automation stops intraday services and runs after-market research.

## Endpoint Inventory

Protected means `dependencies=PROTECTED_ROUTE` in `app/api.py`.

| Method | Path | Function | Category | Safe live use |
|---|---|---|---|---|
| GET | `/health` | `health` | health | Yes |
| GET | `/db/health` | `db_health` | health | Yes |
| GET | `/runtime/status` | `runtime_status` | diagnostics | Yes |
| GET | `/market-data/cache/status` | `market_data_cache_status` | diagnostics | Yes |
| GET | `/strategy/versions/current` | `current_strategy_version` | research/config | Yes |
| GET | `/strategy/versions` | `list_strategy_versions` | research/config | Yes |
| GET | `/strategy/versions/{version}` | `get_strategy_version` | research/config | Yes |
| POST | `/strategy/versions/register` | `register_strategy_version` | config write | Caution |
| GET | `/` | `root` | docs | Yes |
| GET | `/dashboard` | `command_center_dashboard` | dashboard | Yes |
| GET | `/market/{symbol}` | `get_market_summary` | data | Yes |
| GET | `/data/ingest/status` | `get_ingestion_status` | data status | Yes |
| POST | `/data/ingest/candles` | `ingest_historical_candles` | heavy data | Protected; avoid market hours |
| POST | `/data/ingest/option-snapshots` | `ingest_option_snapshots` | data capture | Protected |
| POST | `/data/collector/start` | `start_option_snapshot_collector` | runtime data | Protected |
| POST | `/data/collector/stop` | `stop_option_snapshot_collector` | runtime data | Protected |
| GET | `/data/collector/status` | `get_option_snapshot_collector_status` | status | Yes |
| POST | `/automation/start` | `start_automation` | automation | Protected; paper only until proven |
| POST | `/automation/stop` | `stop_automation` | automation | Protected |
| POST | `/automation/run-once` | `run_automation_once` | automation | Protected |
| GET | `/automation/status` | `get_automation_status` | status | Yes |
| GET | `/runtime/trading-config` | `get_runtime_trading_config` | config status | Yes |
| GET | `/runtime/trading-config/preview` | `preview_runtime_trading_config` | config preview | Yes |
| POST | `/runtime/trading-config/apply` | `apply_runtime_trading_config` | runtime config | Protected |
| POST | `/data/ingest/option-candles` | `ingest_option_candles` | heavy data | Protected; avoid market hours |
| POST | `/data/ingest/all` | `ingest_all_market_research_data` | heavy data | Protected; avoid market hours |
| GET | `/research/settings` | `get_research_settings` | research config | Yes |
| POST | `/research/market-insights` | `get_market_insights` | research | Protected |
| GET | `/research/outcome-learning` | `get_outcome_learning` | research | Protected/heavy |
| GET | `/research/opportunity-analytics` | `get_opportunity_analytics` | research | Protected/heavy |
| GET | `/research/execution-analytics` | `get_execution_analytics` | research | Protected/heavy |
| GET | `/research/professional-insights` | `get_professional_insights` | research | Protected/heavy |
| GET | `/research/research-engine` | `get_research_engine_report` | research | Protected/heavy |
| GET | `/research/threshold-validation` | `get_threshold_validation_report` | research | Protected/heavy |
| GET | `/research/execution-realism` | `get_execution_realism_report` | research | Protected/heavy |
| GET | `/research/daily-review` | `get_daily_review` | research | Protected/heavy |
| GET | `/research/daily-banknifty-summary` | `get_daily_banknifty_summary` | research | Protected |
| GET | `/research/after-market/status` | `get_after_market_research_status` | status | Yes |
| POST | `/research/after-market/run` | `run_after_market_research` | after-market job | Protected |
| GET | `/research/trade-journal` | `get_trade_journal` | research | Protected |
| GET | `/research/data-completeness` | `get_data_completeness` | research | Protected |
| GET | `/research/shadow-comparison` | `get_shadow_comparison` | research | Protected |
| GET | `/research/professional-readiness` | `get_professional_readiness` | readiness | Protected |
| POST | `/research/greeks` | `estimate_greeks` | diagnostic | Protected |
| POST | `/research/option-quality` | `score_option_quality` | diagnostic | Protected |
| POST | `/research/backtest` | `run_research_backtest` | backtest | Protected/heavy |
| POST | `/research/backtest/options` | `run_option_premium_backtest` | backtest | Protected/heavy |
| POST | `/research/backtest/ablation` | `run_ablation_backtest` | backtest | Protected/heavy |
| POST | `/research/walk-forward` | `run_walk_forward_validation` | backtest | Protected/heavy |
| POST | `/research/strategy-edge/validate` | `validate_strategy_edge` | research write | Protected/heavy |
| GET | `/research/strategy-edge` | `list_strategy_edge` | research status | Yes |
| POST | `/research/strategy-ranking` | `rank_strategy_edge` | research | Protected/heavy |
| POST | `/research/option-history/import` | `import_option_history` | data import | Protected |
| GET | `/research/option-history` | `list_option_history` | data status | Yes |
| GET | `/signals` | `get_signals` | scanner | Caution: saves signals |
| GET | `/scanner/opportunities` | `get_opportunities` | scanner | Paper safe |
| GET | `/scanner/diagnostics` | `get_scanner_diagnostics` | scanner diagnostic | Paper safe |
| GET | `/scanner/armed-entries` | `get_scanner_armed_entries` | scanner status | Yes |
| POST | `/orders/place` | `place_order` | order | Protected; live requires flags |
| GET | `/risk/status` | `get_risk_status` | risk | Yes but broker call may be slow |
| GET | `/trades` | `list_trades` | trade status | Yes |
| GET | `/trades/exit-alerts` | `trade_exit_alerts` | trade safety | Yes |
| GET | `/trades/test-artifacts` | `trade_test_artifacts` | diagnostics | Yes |
| POST | `/trades/test-artifacts/quarantine` | `quarantine_trade_test_artifacts` | DB write | Protected |
| POST | `/trades/{trade_id}/close` | `close_trade` | trade write | Protected |
| POST | `/trades/sync` | `sync_live_trades` | broker sync | Protected |
| POST | `/broker/reconcile` | `reconcile_broker_positions` | broker sync | Protected |
| GET | `/broker/reconciliation/status` | `broker_reconciliation_status` | status | Yes |
| GET | `/broker/emergency-protection/status` | `broker_emergency_protection_status` | status | Yes |
| POST | `/trades/evaluate-exits` | `evaluate_trade_exits` | exit engine | Protected |
| POST | `/alerts/test` | `send_test_alert` | diagnostics | Yes |
| POST | `/auto-trader/start` | `start_auto_trader` | automation | Protected |
| POST | `/auto-trader/stop` | `stop_auto_trader` | automation | Protected |
| GET | `/auto-trader/status` | `get_auto_trader_status` | status | Yes |
| GET | `/auto-trader/latest` | `get_auto_trader_latest` | status | Yes |
| GET | `/auto-trader/executions` | `get_auto_trader_executions` | status | Yes |
| GET | `/dashboard/decision-feed` | `get_dashboard_decision_feed` | diagnostics | Yes |
| GET | `/opportunities` | `list_saved_opportunities` | journal | Yes |
| POST | `/opportunities/{opportunity_id}/outcome` | `update_opportunity_outcome` | journal write | Caution |
| GET | `/opportunities/performance` | `get_opportunity_performance` | analytics | Yes |
| GET | `/opportunities/failure-analysis` | `get_opportunity_failure_analysis` | analytics | Yes |
| GET | `/opportunities/rejections` | `get_rejected_opportunities` | analytics | Yes |
| POST | `/opportunities/rejections/{rejection_id}/outcome` | `update_rejected_opportunity_outcome` | journal write | Caution |
| POST | `/opportunities/evaluate-open` | `evaluate_open_opportunities` | outcome monitor | Caution |
| POST | `/opportunities/rejections/evaluate-open` | `evaluate_rejected_opportunities` | outcome monitor | Caution |
| POST | `/opportunity-monitor/start` | `start_opportunity_monitor` | automation | Caution |
| POST | `/opportunity-monitor/stop` | `stop_opportunity_monitor` | automation | Caution |
| GET | `/opportunity-monitor/status` | `get_opportunity_monitor_status` | status | Yes |
| POST | `/auto-trader/scan-once` | `scan_once_auto_trader` | scanner | Caution |
| GET | `/paper/summary` | `get_paper_summary` | paper status | Yes |
| GET | `/paper/positions` | `get_paper_positions` | paper status | Yes |
| GET | `/paper/trades` | `get_paper_trades` | paper status | Yes |
| POST | `/paper/close` | `close_paper_position` | paper write | Caution |
| GET | `/kite/login` | `kite_login` | auth | Yes |
| GET | `/kite/auth` | `kite_auth` | auth | Yes |
| GET | `/kite/health` | `kite_health` | auth/status | Yes |
| GET | `/kite/websocket/status` | `kite_websocket_status` | websocket status | Yes |
| GET | `/kite/margins` | `kite_margins` | broker status | Yes but broker call |
| GET | `/kite/positions` | `kite_positions` | broker status | Yes but broker call |
| GET | `/kite/callback`, `/login` | `kite_callback` | auth callback | Yes |
| POST | `/kite/session` | `kite_session` | auth token exchange | Caution |

## Example Requests

Paper automation start:

```json
POST /automation/start
{
  "symbols": "BANKNIFTY",
  "order_mode": "paper",
  "place_orders": true,
  "confirm_live": false
}
```

Scanner:

```text
GET /scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3&order_mode=paper
```

Exit monitor:

```json
POST /trades/evaluate-exits
{"limit": 100}
```

