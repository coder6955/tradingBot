# Backtest Live Parity Audit

## Backtest Engines

Implemented in `app/services/backtest_service.py`:

- `run` - underlying candle directional replay.
- `run_option_premium` - historical option premium OHLC replay.
- `run_walk_forward` - train/test split validation.
- `run_ablation` - factor removal variants.
- `HistoricalScannerReplayFeed` - scanner-like historical feed.

## Parity Strengths

- Scanner parity mode exists and is tested in `tests/test_backtest_scanner_parity.py`.
- Backtest can use option premium candles, not just underlying approximation.
- Option premium backtest includes stop/target/slippage/charges concepts.
- Walk-forward and ablation exist.
- Strategy edge validations are persisted in `strategy_validations`.
- After-market research runs option backtest, ablation, walk-forward, readiness, strategy-edge validation.

## Parity Gaps

| Area | Live | Backtest | Gap |
|---|---|---|---|
| WebSocket freshness | tick age, exchange timestamp, reconnect, gaps | not fully simulated | Live outages can change outcomes |
| Premium candles | WebSocket builder + stored DB | stored candles | Builder warmup/gap behavior not fully replayed |
| Order execution | Kite market order, bid/ask, broker order history | simulated fill/slippage | Latency and order rejection not fully modeled |
| Exits | live WebSocket/polling + broker confirmation | candle-based simulation | Intracandle path/fill uncertainty |
| Rejected opportunities | stored and analyzed | partially included in analytics | Not all rejected trades replayed as alternate universe |
| Data quality | live quote freshness fail-closed | historical availability assumptions | Can overstate tradability |
| API/runtime load | queues, reconnect, status | not simulated | Production runtime risk omitted |

## Current Evidence

Read-only DB aggregate found:

- `strategy_validations`: 0
- `trades`: 0
- `opportunities`: 0
- `rejected_opportunities`: 0

Therefore parity infrastructure exists, but there is no local validation evidence in the current DB.

## Required Improvements Before Live Claims

1. Build a replay mode that injects WebSocket tick gaps, stale ticks, disconnects, reconnect latency, and polling fallback.
2. Add slippage/fill model from actual paper/live-shadow quotes.
3. Persist fill deviation and compare intended vs actual entry/exit.
4. Include rejected opportunity replay in readiness reports.
5. Require minimum sample gates in `ProfessionalReadinessService`: 30-50 closed trades minimum before "ready".

