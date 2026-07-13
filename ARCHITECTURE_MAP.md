# Architecture Map

## Classification

This is a modular monolith. `app/api.py` is the composition root and all services run in one Python/FastAPI process. There are microservice-like boundaries in code, but no separate service deployment boundaries.

## Folder Structure

- `app/api.py` - HTTP API, dashboard, startup/shutdown, service wiring.
- `app/config.py` - environment-backed settings.
- `app/models.py` - `Signal` dataclass.
- `app/providers/`
  - `kite_provider.py` - Kite REST wrapper.
  - `kite_auth_state.py` - shared auth failure state.
  - `kite_feed.py` - market snapshot/instruments/quotes adapter with caches/fallback.
  - `token_store.py` - access token persistence.
- `app/services/`
  - scanner and strategy: `scanner_service.py`, `decision_engine_service.py`, `indicator_scoring_service.py`, `banknifty_intelligence_service.py`, `banknifty_regime_filter_service.py`, `entry_timing_service.py`.
  - options: `trade_setup_service.py`, `option_quality_service.py`, `option_premium_confirmation_service.py`, `option_chain_service.py`, `greeks_service.py`, `volatility_edge_service.py`.
  - data/runtime: `kite_websocket_price_feed.py`, `active_price_feed.py`, `market_data_coordinator.py`, `data_freshness_service.py`, `market_session_service.py`, `market_data_runtime_service.py`.
  - trading: `order_service.py`, `trade_exit_service.py`, `paper_trading_service.py`, `risk_management_service.py`, `broker_sync_service.py`.
  - storage/repositories: `database.py`, `trade_repository.py`, `opportunity_repository.py`, `rejected_opportunity_repository.py`, `option_history_repository.py`, `runtime_job_repository.py`.
  - automation: `automation_supervisor_service.py`, `auto_trader_service.py`, `option_snapshot_collector_service.py`, `after_market_research_service.py`.
  - research/analytics: `backtest_service.py`, `professional_insights_service.py`, `professional_readiness_service.py`, `execution_analytics_service.py`, `opportunity_analytics_service.py`, `strategy_edge_service.py`.
- `tests/` - pytest suite.
- `scripts/` - smoke and reset utilities.

## Main Runtime Objects

- `ScannerService`: full opportunity pipeline.
- `TradeSetupService`: expiry/strike/contract selection, SL/target, position size, contract risk checks.
- `DecisionEngineService`: weighted final score.
- `OrderService`: paper/live routing.
- `TradeExitService`: active trade exit monitor.
- `KiteWebSocketPriceFeed`: tick ingestion, subscriptions, reconnect, premium candle builder.
- `ActiveTradePriceFeed`: WebSocket-first active price source with polling fallback.
- `AutomationSupervisorService`: market-hours automation lifecycle.
- `AfterMarketResearchService`: staged end-of-day research/learning pipeline.

## Database Tables

Defined in `app/services/database.py`:

- `candles`
- `option_quote_snapshots`
- `signals`
- `opportunities`
- `rejected_opportunities`
- `trades`
- `strategy_validations`
- `strategy_versions`
- `runtime_job_runs`

## Key Flows

### Scanner

`GET /scanner/opportunities` -> `api._start_scanner_refresh` -> `ScannerService.scan_with_diagnostics` -> `TradeSetupService.select_contract` -> all gates/scoring -> `OpportunityRepository.save_opportunity` or `RejectedOpportunityRepository.save_rejection`.

### Paper Order

`POST /orders/place` -> `OrderService.place_signal_order` -> validation -> `PaperTradingService.execute_trade` -> `TradeRepository.create_trade` -> active token subscribe.

### Live Order

`POST /orders/place` with live config -> `OrderService.place_signal_order` -> live flags -> broker reconciliation -> risk guard -> affordable quantity -> `KiteProvider.place_order` -> `TradeRepository.create_trade` -> optional broker emergency SL.

### Exit

`TradeExitService.evaluate_once` -> open trades -> active price -> outcome rules -> paper close or live squareoff -> confirmation -> DB close/failure status.

### WebSocket

`MarketDataRuntimeService.start` / `ActiveTradePriceFeed.start` -> `KiteWebSocketPriceFeed.start` -> KiteTicker -> callbacks -> ticks/candles/gap events/status.

### After Market

`AutomationSupervisorService.run_once` after close -> stop intraday services -> evaluate open opportunities once -> `AfterMarketResearchService.maybe_run_after_market` -> staged reports -> `runtime_job_runs`.

