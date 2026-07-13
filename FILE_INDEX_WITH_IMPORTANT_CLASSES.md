# File Index With Important Classes

## API/Composition

- `app/api.py`
  - `startup_automation`
  - `_run_startup_maintenance`
  - `_run_startup_broker_sync`
  - `require_api_auth`
  - all FastAPI endpoint functions

## Config/Models

- `app/config.py`
  - `Settings`
- `app/models.py`
  - `Signal`

## Providers

- `app/providers/kite_provider.py`
  - `KiteProvider`
- `app/providers/kite_auth_state.py`
  - `KiteAuthState`
  - `is_kite_token_exception`
- `app/providers/kite_feed.py`
  - `KiteFeed`
- `app/providers/token_store.py`
  - `save_access_token`
  - `load_access_token`

## Scanner/Strategy

- `app/services/scanner_service.py`
  - `ScannerService`
  - `scan_with_diagnostics`
  - `_score_breakdown`
  - `_save_rejection`
- `app/services/decision_engine_service.py`
  - `DecisionEngineService`
- `app/services/indicator_scoring_service.py`
  - `IndicatorScoringService`
- `app/services/trade_setup_service.py`
  - `OptionContract`
  - `TradeSetupService`
- `app/services/banknifty_intelligence_service.py`
  - `BankNiftyIntelligenceService`
- `app/services/banknifty_regime_filter_service.py`
  - `BankNiftyRegimeFilterService`
- `app/services/entry_timing_service.py`
  - `EntryTimingService`
- `app/services/option_quality_service.py`
  - `OptionQualityService`
- `app/services/option_premium_confirmation_service.py`
  - `OptionPremiumConfirmationService`
- `app/services/volatility_edge_service.py`
  - `VolatilityEdgeService`
- `app/services/data_freshness_service.py`
  - `DataFreshnessService`

## Trading Runtime

- `app/services/order_service.py`
  - `OrderService`
- `app/services/trade_exit_service.py`
  - `TradeExitService`
- `app/services/trade_repository.py`
  - `TradeRepository`
- `app/services/paper_trading_service.py`
  - `PaperTradingService`
- `app/services/risk_management_service.py`
  - `RiskManagementService`
- `app/services/broker_sync_service.py`
  - `BrokerSyncService`

## WebSocket/Market Data

- `app/services/kite_websocket_price_feed.py`
  - `KiteWebSocketPriceFeed`
  - `WebSocketTick`
  - `WebSocketPremiumCandle`
- `app/services/active_price_feed.py`
  - `ActiveTradePriceFeed`
  - `KitePollingPriceFeed`
  - `PriceTick`
- `app/services/market_data_coordinator.py`
  - `MarketDataCoordinator`
- `app/services/market_data_runtime_service.py`
  - `MarketDataRuntimeService`
- `app/services/market_session_service.py`
  - `MarketSessionService`

## Automation/After Market

- `app/services/automation_supervisor_service.py`
  - `AutomationSupervisorService`
- `app/services/auto_trader_service.py`
  - `AutoTraderService`
- `app/services/after_market_research_service.py`
  - `AfterMarketResearchService`
- `app/services/runtime_job_repository.py`
  - `RuntimeJobRepository`
- `app/services/option_snapshot_collector_service.py`
  - `OptionSnapshotCollectorService`

## Research/Analytics

- `app/services/backtest_service.py`
  - `BacktestService`
  - `HistoricalScannerReplayFeed`
  - `BacktestTrade`
  - `OptionBacktestTrade`
- `app/services/professional_insights_service.py`
  - `ProfessionalInsightsService`
- `app/services/professional_readiness_service.py`
  - `ProfessionalReadinessService`
- `app/services/execution_analytics_service.py`
  - `ExecutionAnalyticsService`
- `app/services/opportunity_analytics_service.py`
  - `OpportunityAnalyticsService`
- `app/services/strategy_edge_service.py`
  - `StrategyEdgeService`
- `app/services/strategy_validation_repository.py`
  - `StrategyValidationRepository`

## Database Tables

Defined in `app/services/database.py`:

- `Candle` -> `candles`
- `OptionQuoteSnapshot` -> `option_quote_snapshots`
- `SignalRecord` -> `signals`
- `OpportunityRecord` -> `opportunities`
- `RejectedOpportunityRecord` -> `rejected_opportunities`
- `TradeRecord` -> `trades`
- `StrategyValidationRecord` -> `strategy_validations`
- `StrategyVersionRecord` -> `strategy_versions`
- `RuntimeJobRunRecord` -> `runtime_job_runs`

