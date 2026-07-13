# Accuracy Gaps And Fix Plan

## Executive Verdict

The app is architecturally close to a serious paper/live-shadow trading system, but not close to a proven high-accuracy system because realized evidence is absent in the current database.

## Top Gaps

### 1. No local performance evidence

- Severity: Critical
- Type: evidence
- Evidence: read-only DB query returned 0 trades, 0 opportunities, 0 rejections, 0 strategy validations.
- Fix: collect 30 trading days in paper/live-shadow mode.

### 2. Premium confirmation dependency

- Severity: High
- Type: data/strategy
- Where: `OptionPremiumConfirmationService.evaluate`.
- Risk: if WebSocket premium candles are not warm or stored candles stale, good setups can be missed.
- Fix: run snapshot collector and WebSocket premium builder from open; review rejected winners.

### 3. Execution realism

- Severity: High
- Type: execution
- Where: `OrderService`, `TradeExitService`, `ExecutionRealismService`.
- Risk: BankNifty options move fast; market order slippage can erase edge.
- Fix: measure bid/ask, intended entry, actual fill, intended exit, actual exit.

### 4. Backtest/live outage mismatch

- Severity: High
- Type: evidence
- Where: `BacktestService` vs `KiteWebSocketPriceFeed`.
- Risk: backtests do not include real WebSocket gaps/reconnects/stale data.
- Fix: add outage simulation.

### 5. Overfiltering risk

- Severity: Medium
- Type: strategy
- Where: BankNifty intelligence, premium confirmation, option quality, entry timing.
- Risk: missed valid trades.
- Fix: rejected outcome analysis and threshold validation after enough data.

### 6. Production deployment hardening

- Severity: Medium
- Type: architecture/ops
- Where: single-process automation and runtime DB alters.
- Risk: duplicate work if multiple instances, migration risk.
- Fix: single-instance deployment or durable locks; formal migrations.

## 30-Day Measurement Plan

Track daily:

- Closed paper trades.
- Win rate.
- Average win/loss.
- Expectancy after charges/slippage.
- Max drawdown.
- Consecutive losses.
- Time-of-day performance.
- Setup family performance.
- CE vs PE performance.
- Expiry/DTE performance.
- Premium confirmation source performance.
- WebSocket uptime/gaps.
- Entry deviation.
- Exit deviation.
- Rejected winners by primary gate.

Minimum readiness thresholds before live discussion:

- 30-50 closed trades.
- Positive expectancy after realistic costs.
- No unresolved exit alerts.
- Walk-forward pass with real option candles.
- Rejected-opportunity analysis does not show major gate damage.

