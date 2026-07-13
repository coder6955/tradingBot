# App Audit For High Accuracy

Scope: read-only audit of the BankNifty option-buying app. No application code, config, migration, or runtime behavior was modified for this audit. The only database access was a read-only aggregate evidence check.

## 1. Architecture Overview

The app is a modular monolith. It is not a microservice system: one FastAPI app in `app/api.py` wires services, repositories, broker adapters, scanner logic, WebSocket runtime, order placement, exits, automation, and research endpoints in one process.

Main structure:

- `app/api.py` - FastAPI composition root, endpoints, startup/shutdown hooks, dashboard HTML, dependency wiring.
- `app/config.py` - frozen `Settings` dataclass populated from `.env`.
- `app/models.py` - in-memory/dataclass `Signal`.
- `app/providers/` - Kite REST wrapper, Kite auth state, token store, Kite feed adapter.
- `app/services/` - scanner, strategy gates, scoring, data freshness, WebSocket, order/trade lifecycle, automation, repositories, backtest, analytics, readiness.
- `tests/` - focused tests for scanner, WebSocket, exits, order safety, backtest parity, runtime services, analytics.
- `scripts/` - smoke/reset helper scripts.

Main execution paths:

- Startup: `app/api.py:342 startup_automation`, `_run_startup_maintenance`, `_run_startup_broker_sync`.
- Kite auth/session: `app/providers/kite_provider.py:16 KiteProvider`, `app/providers/token_store.py`, endpoints `/kite/auth`, `/kite/callback`, `/kite/session`.
- Market data: `app/providers/kite_feed.py:20 KiteFeed`, `app/services/market_data_coordinator.py`, `app/services/data_freshness_service.py`.
- WebSocket: `app/services/kite_websocket_price_feed.py:43 KiteWebSocketPriceFeed`, `app/services/active_price_feed.py:100 ActiveTradePriceFeed`.
- Scanner/opportunity generation: `app/services/scanner_service.py:35 ScannerService`, `scan_with_diagnostics`.
- Contract/price/risk plan: `app/services/trade_setup_service.py:28 TradeSetupService`.
- Order placement: `app/services/order_service.py:14 OrderService`.
- Trade records: `app/services/trade_repository.py`.
- Exit monitoring: `app/services/trade_exit_service.py:19 TradeExitService`.
- Automation: `app/services/automation_supervisor_service.py:18 AutomationSupervisorService`, `app/services/auto_trader_service.py:22 AutoTraderService`.
- Research/backtest: `app/services/backtest_service.py`, `professional_insights_service.py`, `professional_readiness_service.py`, `after_market_research_service.py`.

## 2. Complete Runtime Workflow

### Startup/config loading

- `app/config.py:19 Settings` loads `.env` at import time.
- `app/api.py` constructs singleton service instances: market data, scanner dependencies, WebSocket feed, active trade price feed, order service, trade exit service, automation supervisor, after-market research service.
- Startup hook `app/api.py:342 startup_automation` starts background maintenance and broker sync in threads. If `AUTOMATION_ENABLED=true`, it starts automation automatically.
- Failure handling: startup tasks catch and log exceptions. Broker sync skips if `kite_auth_state.relogin_required`.

### Kite authentication/session

- `app/providers/kite_provider.py:16 KiteProvider` wraps Kite Connect.
- `set_access_token` clears auth failure state on a new token.
- REST calls are wrapped by `_call`; Kite token exceptions mark `AUTH_FAILED`.
- `app/providers/kite_auth_state.py` exposes `relogin_required`.
- Failure handling: auth failure blocks later Kite-dependent calls via `_ensure_ready`, and WebSocket start returns `kite_relogin_required`.

### Market data loading

- Scanner chooses `KiteFeed` if `USE_KITE_MARKET_DATA=true` and a token exists; otherwise `MockMarketFeed` in `ScannerService.__init__`.
- `KiteFeed` has cached instruments/quotes and stored candle fallback.
- `DataFreshnessService.validate_scan_inputs` blocks stale/missing live data in live mode only.
- In paper mode, stale/fallback data can pass more easily; this is acceptable for shadow learning but not live accuracy claims.

### WebSocket connection/subscription

- `KiteWebSocketPriceFeed.start` refreshes credentials, checks market session, prevents duplicate starts, creates KiteTicker, starts worker queues.
- WebSocket subscribes selected option tokens and BankNifty underlying token via active trade/exits/order flow.
- `status` reports connected state, tick age, subscription errors, premium candle builder counts, queue drops, reconnect data, auth state.
- Auth failure stops reconnect attempts; rate limit stops reconnect; ordinary disconnect starts data gap and reconnect with backoff.

### Scanner/opportunity generation

- Endpoint `/scanner/opportunities` calls `_start_scanner_refresh`, and cached results are returned to avoid heavy repeated scans.
- `ScannerService.scan_with_diagnostics`:
  - Loads option instruments.
  - Focuses universe to `BANKNIFTY`.
  - Fetches snapshot and option-chain quotes.
  - Selects contract through `TradeSetupService.select_contract`.
  - Evaluates freshness, premium confirmation, option quality, market regime, price action, option chain, day type, time bucket, strategy edge, BankNifty intelligence, volatility edge, entry timing.
  - Builds weighted score via `DecisionEngineService`.
  - Applies hard failures and stores accepted/rejected decisions.

### Hard gates

Hard failures are collected in scanner and helper services. Examples:

- Real Kite data unavailable when Kite data is required.
- No NFO instruments/no matching contract.
- Data quality/freshness failure.
- Option premium confirmation failure.
- Option quality failure.
- BankNifty intelligence hard reasons.
- BankNifty regime filter failure.
- Volatility hard gate if enabled.
- Trade setup risk checks: liquidity, premium minimum, expiry-day block, spread, volume, OI, budget.
- Minimum final score.
- Entry timing `TOO_LATE` or `NO_TRADE`.

Rejected opportunities are persisted through `RejectedOpportunityRepository.save_rejection`.

### Scoring and signal selection

- Raw technical score: `IndicatorScoringService.score_symbol`.
- Weighted final score: `DecisionEngineService.score_breakdown` called by `ScannerService._score_breakdown`.
- Weights:
  - trend_momentum 0.14
  - market_regime 0.11
  - price_action 0.19
  - option_chain_context 0.07
  - liquidity 0.13
  - option_quality 0.16
  - banknifty_intelligence 0.20
- Trend/momentum is capped by `MAX_TREND_MOMENTUM_SCORE`.
- Minimum score is `MIN_SIGNAL_SCORE`.

### Paper/live order placement

- `OrderService.place_signal_order` validates scanner metadata and BankNifty-only option-buying restrictions.
- Paper mode uses `PaperTradingService.execute_trade` and `TradeRepository.create_trade`.
- Live mode requires `LIVE_TRADING_MODE=true`, `PAPER_TRADING_MODE=false`, `confirm_live=true`, broker reconciliation not blocked, risk guard pass, affordable quantity, Kite order submission.
- Broker emergency SL can be placed after entry fill confirmation when enabled.

### Active trade monitoring and exits

- `TradeExitService.evaluate_once` loads open trades, syncs WebSocket subscriptions, fetches current option price, records MFE/MAE, determines exit outcome, then closes paper or submits live exit.
- Exit types include stop loss, target 1/2/3, trailing stop, time exit, underlying invalidation, premium invalidation, partial booking at target 1 if enabled.
- Live exit uses `try_mark_closing` idempotency, submits order, confirms via order history, and alerts on failure.

### Logging/analytics/rejected storage

- Accepted opportunities go to `opportunities`.
- Rejections go to `rejected_opportunities` with reasons and factor scores.
- Trades go to `trades` with price source/timestamp/age, PnL, broker statuses, protective order state, MFE/MAE.
- Analytics endpoints read these tables.

## 3. Strategy Logic Audit

The app implements a multi-factor directional BankNifty option-buying strategy, not a single compact strategy rule.

Used in live/paper scanner:

- Technical momentum: `IndicatorScoringService.score_symbol`.
- Market regime: `MarketRegimeService.evaluate`.
- Price action: `PriceActionService.evaluate`.
- Option chain context: `OptionChainService.analyze`.
- Option quality: `OptionQualityService.evaluate`.
- Premium confirmation: `OptionPremiumConfirmationService.evaluate`.
- BankNifty-specific intelligence: `BankNiftyIntelligenceService.evaluate`.
- BankNifty regime filter: `BankNiftyRegimeFilterService.evaluate`.
- Entry timing: `EntryTimingService.evaluate`.
- Volatility edge: `VolatilityEdgeService.evaluate`.
- Strategy edge optional guard: `StrategyEdgeService.evaluate`.
- Outcome learning optional guard: `OutcomeLearningService`.
- Time bucket/day type guards.

Entry conditions:

- BankNifty only in order validation.
- BUY_CE for bullish, BUY_PE for bearish.
- Fresh enough live data for live mode.
- Real data when Kite market data required.
- Selected option contract has acceptable liquidity, volume, OI, spread, premium, non-expired expiry.
- Premium confirmation must show option premium participation if enabled.
- BankNifty top banks, relative strength, opening range, major zone, expected move, day type must not produce hard reasons.
- Entry timing must be `ENTER_NOW`, or can register armed entry if `ARMED_FOR_ENTRY`.

Exit conditions:

- Stop loss, target 1/2/3.
- Time stop after configured minutes if move is insufficient.
- Near-close exit.
- Trailing stop after target 1.
- Underlying invalidation.
- Premium invalidation.
- Optional partial booking.

BankNifty-specific coverage:

- Top bank alignment, private/PSU split, HDFC/ICICI impact, SBI impact.
- BankNifty vs Nifty relative strength.
- Opening range breakout/breakdown.
- Round-number zone risk.
- Near-ATM OI pressure.
- Expected move coverage.
- DTE/event-day handling.
- Day type/range/choppy detection.

Missing or weak:

- No fully proven calibration from live/paper history in current DB.
- No measured high-accuracy claim because local evidence count is zero.
- PCR/max pain are not clearly first-class hard/soft factors in the inspected live scanner path.
- News/event handling is config-date based, not live news-aware.
- Backtest does not fully simulate WebSocket outages, queue drops, and latency.

## 4. Gates And Rejection Logic

Key hard gates:

| Gate | Path | Condition | Risk |
|---|---|---|---|
| Real Kite data required | `scanner_service.py` | Kite data requested but snapshot not real | Correct for live, can stop paper tests if token/feed broken |
| Contract availability | `scanner_service.py`, `trade_setup_service.py` | no NFO instruments or matching CE/PE expiry | Correct; depends on instrument cache freshness |
| Data freshness | `data_freshness_service.py` | live quote/candle/chain/option age above thresholds | Correct live fail-closed |
| Quote quality | `scanner_service.py` selected option data quality | missing/bad quote, spread etc. | Correct but may miss trades when data sparse |
| Premium confirmation | `option_premium_confirmation_service.py` | insufficient/failing option premium candles | Strong quality gate; can be too strict early in session |
| Option quality | `option_quality_service.py` | delta/theta/IV/DTE/spread/premium score below threshold | Good professional filter, but Greek estimates depend on approximate IV |
| BankNifty intelligence | `banknifty_intelligence_service.py` | mixed top banks, opening range not complete, zone trap, choppy day | Good BankNifty context; can overfilter if constituent data unavailable |
| Entry timing | `entry_timing_service.py` | too late/chase/reward compressed | Good late-entry prevention |
| Risk checks | `trade_setup_service.py` | liquidity score, min premium, expiry day, spread, volume, OI | Good hard safety |
| Score threshold | `config.MIN_SIGNAL_SCORE` | weighted score below threshold | Necessary; calibration not proven |
| Order validation | `order_service.py` | not BankNifty, not BUY_CE/BUY_PE, expired, missing metadata | Strong live safety |
| Account risk | `risk_management_service.py` | daily loss, max trades, open trades, exposure, cooldown | Strong live safety |

Most rejected opportunities are logged and stored. `ScannerService._save_rejection` persists reasons and factor data. Some operational endpoints and paper close paths are less protected than live order endpoints.

## 5. Scoring System Audit

The scoring system is explainable and capped in one important area:

- Raw technical score can be inflated by trend, EMA, VWAP, MACD, volume, RSI, ADX, market context.
- Weighted final score caps raw technical trend/momentum at `MAX_TREND_MOMENTUM_SCORE`.
- Final score includes BankNifty intelligence at the largest weight, which is appropriate for a BankNifty-specific app.
- Diagnostics include `score_breakdown`, components, weights, contributions, caps.

Main weakness: the score is rule-designed, not evidence-calibrated. The current configured DB has zero trades/opportunities/rejections/strategy validations, so there is no realized calibration evidence.

## 6. Market Data And Freshness Audit

Data sources:

- Kite REST quote/instruments/historical: `kite_provider.py`, `kite_feed.py`, `market_data_coordinator.py`.
- Kite WebSocket: `kite_websocket_price_feed.py`.
- Stored candles: `database.Candle`.
- Option quote snapshots: `database.OptionQuoteSnapshot`.
- Mock feed: `mock_market_feed.py`.
- Premium candles from WebSocket builder and stored DB candles.

Freshness:

- Live quote/chain/option/candle thresholds in `DataFreshnessService`.
- WebSocket tick freshness uses `WEBSOCKET_PRICE_STALE_SECONDS`.
- Premium candle freshness checks current session date and age.

Safety:

- Live mode is fail-closed for stale/missing data.
- Paper mode can fall back more freely.
- WebSocket status exposes failures, tick age, data gaps, queue drops.
- Active trade exits record price source and timestamp.

Weakness:

- Paper-mode fallback can create misleading paper evidence if tagged data source is ignored in analysis.
- Backtests do not fully simulate live data freshness constraints.

## 7. WebSocket And Live Price Safety

Strengths:

- Credential refresh before start.
- Market-session gating.
- Duplicate start prevention.
- Auth failure detection and `relogin_required`.
- Rate-limit stop.
- Reconnect backoff.
- Active token tracking.
- Tick age/status visibility.
- Premium candle builder with queue capacity/drop counters.
- Data gap events and cancellation hook to armed entries.

Exit price:

- `TradeExitService` uses `ActiveTradePriceFeed.latest_price`.
- WebSocket preferred.
- Paper mode can poll fallback.
- Live fallback is controlled by `WEBSOCKET_LIVE_GAP_POLLING_FALLBACK`; otherwise stale WebSocket blocks.
- Exit records `price_source`, `price_timestamp`, `price_age_seconds`.

Risk:

- Polling fallback for live is powerful but dangerous if enabled too broadly. Keep it conservative until live evidence exists.
- If broker/Kite delays order history, live exit confirmation can remain in `closing`/`exit_failed`; alerting exists, but operational response is still required.

## 8. Order Placement And Trade Lifecycle

Paper:

- Validated signal -> paper trade -> DB trade record -> active token subscribe.

Live:

- Validated signal -> live flags/confirm -> reconciliation check -> risk guard -> affordable quantity -> Kite market order -> trade record -> optional broker emergency SL -> active token subscribe.

Strengths:

- BankNifty-only order validation.
- Scanner metadata required.
- Live mode double switch required.
- Broker reconciliation block exists.
- Emergency SL exists but disabled by default.
- Idempotent live exits via closing state.
- Exit failure states and alerting exist.

Weaknesses:

- Market orders are used; slippage can be large in fast BankNifty options.
- Partial booking is optional and disabled by default.
- Emergency SL is optional and disabled by default.
- Live order placement still depends on API responsiveness and order history confirmation.

## 9. Backtest Vs Live Scanner Parity

Backtest exists in `app/services/backtest_service.py`.

Parity strengths:

- `HistoricalScannerReplayFeed` replays scanner-like context.
- Option premium backtest uses stored option OHLC candles.
- Walk-forward and ablation exist.
- `tests/test_backtest_scanner_parity.py` covers scanner parity.
- Backtest has scanner parity mode and execution realism modules.

Parity gaps:

- Live WebSocket gaps, queue drops, exchange timestamp requirements, and reconnect behavior are not fully simulated.
- Broker order latency/order-history uncertainty is not fully modeled.
- Rejected opportunities are analyzed, but not fully simulated as alternate trade universe in every backtest path.
- Backtest evidence depends heavily on stored option candles/snapshots, which the current DB did not have.

## 10. Accuracy Evidence

Read-only aggregate query against the currently configured DB returned:

- trades_total: 0
- trades_closed: 0
- wins: 0
- losses: 0
- win_rate_pct: None
- avg_win: None
- avg_loss: None
- expectancy: None
- opportunities: 0
- rejections: 0
- strategy_validations: 0

Therefore there is no local evidence to claim high accuracy. The app has tooling to measure accuracy, but not evidence in the current DB.

## 11. Production Readiness Audit

Strengths:

- Modular services and tests.
- Protected dangerous endpoints when API token configured.
- Good diagnostics endpoints.
- WebSocket observability.
- Auth failure handling.
- Runtime automation and after-market job idempotency.
- Trade lifecycle DB state is detailed.
- Alerts via notification service.
- Readiness and research endpoints.

Weaknesses:

- Secrets are currently in `.env`; protect the machine and never commit `.env`.
- No formal Alembic migrations; `database.py` uses runtime `ALTER TABLE` helpers.
- Single-process background tasks; no distributed lock if multiple server instances run.
- API auth currently optional in local config.
- No production metrics stack (Prometheus, structured log shipping, process supervisor) visible.
- Accuracy evidence absent.

## 12. Biggest Accuracy Weaknesses

1. Critical - no realized evidence in DB.
   - Why: cannot claim accuracy without trades.
   - Where: DB aggregate query, analytics services depend on empty tables.
   - Type: evidence.
   - Fix: collect 30 trading days of paper/live-shadow evidence.

2. High - premium confirmation can be unavailable early/session gaps.
   - Where: `OptionPremiumConfirmationService.evaluate`.
   - Type: data/strategy.
   - Fix: ensure WebSocket premium candles and snapshot collector are running from open; analyze missed winners.

3. High - market orders and slippage risk.
   - Where: `OrderService.place_signal_order`, `TradeExitService._squareoff`.
   - Type: execution.
   - Fix: measure fill deviation; consider guarded limit/market-protection logic after evidence.

4. High - backtest/live mismatch around WebSocket/freshness/latency.
   - Where: `backtest_service.py` vs WebSocket/active feed.
   - Type: evidence/execution.
   - Fix: add latency/freshness outage replay.

5. Medium - many filters may overfit or overblock.
   - Where: BankNifty intelligence, premium confirmation, option quality, regime filters.
   - Type: strategy.
   - Fix: rejected-outcome analysis over real paper data.

## 13. Missing BankNifty-Specific Factors

Implemented:

- Opening range.
- Top bank alignment.
- Relative strength vs Nifty.
- Premium breakout/option VWAP.
- Expected move.
- Major round zones.
- Range/choppy day handling.
- DTE/event-day config.
- Liquidity/spread/volume/OI.
- IV/theta/delta.
- Late entry prevention.
- Option premium structure stop/target.

Still missing or weak:

- Live news/event calendar integration.
- More robust PCR/max-pain use in live scanner.
- Explicit false-breakout retest acceptance model.
- Measured trend continuation vs exhaustion model.
- 30-day time-of-day calibration.
- BankNifty constituent data quality scoring when some top banks are missing.

## 14. Endpoint Inventory

Full endpoint inventory is in `ENDPOINTS_AND_WORKFLOW.md`. There are 100+ endpoints across system, Kite, scanner, orders, automation, research, data ingestion, opportunity journal, paper trading, and dashboard.

Recommended market-hour sequence:

1. `GET /health`
2. `GET /db/health`
3. `GET /kite/health`
4. `GET /kite/websocket/status`
5. `GET /runtime/status`
6. `POST /automation/start` in paper mode
7. `GET /automation/status`
8. `GET /scanner/opportunities?side=BUY&symbols=BANKNIFTY&limit=3&order_mode=paper`
9. `GET /trades`, `/trades/exit-alerts`, `/broker/reconciliation/status`
10. After close, `GET /research/after-market/status`

## 15. Tests

Test coverage is broad:

- API integration and dashboard runtime config.
- WebSocket connection, reconnect, auth failure, stale ticks, candle builder.
- Order service validation, live safety, execution quality.
- Trade exits, live exit safety, MFE/MAE.
- Scanner data quality, real contracts, indicators, signal engine.
- Backtest scanner parity.
- Market session/runtime service.
- After-market automation.

Most urgent missing tests:

- Full market-day soak simulation.
- WebSocket outage during active trade with broker polling disabled/enabled.
- Strategy calibration tests from real paper/live-shadow data.
- Multi-process duplicate automation protection.
- Latency/slippage models tied to actual fill logs.

## 16. Final Verdict

The app is more than a scanner: it is a semi-automated modular monolith trading system with paper/live lifecycle, WebSocket runtime, risk guards, exits, backtests, analytics, and after-market learning.

It is not yet a production-grade, high-accuracy live trading system.

Confidence level now: Medium for paper/live-shadow testing; Low for live-money accuracy claims.

Before serious paper trading:

- Start automation before market.
- Verify Kite health and WebSocket status.
- Ensure snapshot/premium candle collection.
- Run full day paper automation and after-market pipeline.

Before live trading:

- 30-50+ closed paper/live-shadow trades minimum.
- Positive expectancy after slippage/charges.
- Passing walk-forward with real option premium data.
- No stale exit alerts/reconciliation mismatches.
- Emergency SL and live exit procedures tested.

Before claiming high accuracy:

- Measure 30 trading days: win rate, expectancy, drawdown, time-of-day, setup family, rejected winners, fill deviation, exit quality, WebSocket uptime, and day-type segmentation.

