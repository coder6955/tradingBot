# Shadow Entry Validation Implementation Report

## 1. Foundation audit

The foundation audit found two serious correctness defects and corrected both before this phase began:

- Expired reservation reclaim compared only `state=RESERVED`, allowing two stale reclaimers to overwrite one another. Reclaim now compares prior state, token, and expiry as one CAS predicate.
- A paper fill followed by durable trade-write failure released the episode while the in-memory paper position remained. The exact unpersisted position is now rolled back; a durable trade whose episode transition fails remains locked in `ORDER_PENDING`.

All entry routes converge on `OrderService.place_signal_order()`. Direct broker calls outside it are protective, exit, or reconciliation operations. No entry route can directly create an open trade, shadow quantities never authorize an active signal, and the process risk ceiling remains hard-limited to 5%.

- Foundation implementation commit: `6c6cd99 Add centralized risk policy and decision evidence tracking`
- Foundation audit correction commit: `3235fe8 Harden episode recovery and paper persistence`
- Research branch: `codex/shadow-entry-validation`
- Research-layer working tree: intentionally uncommitted on the research branch; active/live configuration is unchanged.

## 2. Files changed

| File | Purpose |
|---|---|
| `.env.example` | Documents bounded evidence/outcome queue, retry, horizon, cutoff, coverage, and gap settings. |
| `app/config.py` | Loads the new research/queue settings. |
| `app/api.py` | Wires workers, executable-bid equity, shadow comparison, tick dispatch, lifecycle flush/recovery, status, dataset, policy-report, and equity endpoints. |
| `app/services/database.py` | Adds crash-safe intent, episode path, policy decision, replay, and equity snapshot schema; moves schema inspection to initialization and disables commit expiration. |
| `app/services/decision_evidence_repository.py` | Makes recovered evidence idempotent by decision ID. |
| `app/services/episode_reservation_service.py` | Atomically reserves directly into `ORDER_PENDING` with minimum intent, persists broker/evidence state, exposes recovery, and uses insert-first unique-key arbitration. |
| `app/services/evidence_persistence_queue.py` | Bounded background evidence writer with explicit full behavior, retries, metrics, dead letters, and durable-intent recovery. |
| `app/services/pre_order_risk_service.py` | Separates safety-critical intent from large evidence, records component latency, and registers outcome paths. |
| `app/services/order_service.py` | Persists broker acknowledgement before local live trade creation and preserves recoverable locks across interruption windows. |
| `app/services/broker_sync_service.py` | Treats unresolved live order intents as startup reconciliation mismatches that block automation. |
| `app/services/episode_outcome_collector.py` | Implements asynchronous unique-episode executable market paths, horizons, coverage, retries/dead letters, and worker-side policy evaluation. |
| `app/services/entry_policy_shadow_service.py` | Defines baseline, transition-preparation, continuous-transmission, and structurally isolated comparison policies. |
| `app/services/event_time_replay_service.py` | Implements deterministic event-time replay, lifecycle/provenance rules, instrument lineage, and bar-only separation. |
| `app/services/account_equity_state_service.py` | Implements reproducible executable-bid equity, P&L, drawdown, exposure, and distinct risk state. |
| `app/services/risk_management_service.py` | Uses the authoritative equity snapshot without adding a non-safety write to final pre-order. |
| `app/services/risk_policy_service.py` | Adds unrealized-loss defense for higher-tier requests. |
| `app/services/stop_exit_liquidity_service.py` | Adds current-book, historical adverse-move, and stress exit research estimates. |
| `app/services/policy_evaluation_service.py` | Adds timing waterfall, missed-reason taxonomy, unique-episode metrics, segments, chronological folds, risk simulations, and promotion checks. |
| `app/services/research_reporting_service.py` | Builds read-only dataset completeness and persisted shadow-policy reports. |
| `app/services/latency_metrics_service.py` | Adds component/outcome metrics and the 25 ms p95 final-pre-order target. |
| `app/services/scanner_service.py` | Registers scheduled accepted/rejected episode paths and immutable shadow-policy contexts. |
| `scripts/benchmark_shadow_validation.py` | Runs the strict isolated SQLite performance acceptance benchmark. |
| `tests/test_shadow_entry_validation.py` | Adds 15 outcome, replay, isolation, recovery, equity, liquidity, fold, simulation, and performance tests. |
| `tests/test_risk_tier_foundation.py` | Extends legacy migration and unrealized-loss defensive-tier coverage. |
| `docs/ARCHITECTURE.md` | Maintains component/runtime source of truth. |
| `docs/TRADING_FLOW.md` | Maintains critical-path and shadow/outcome behavior. |
| `docs/DECISIONS.md` | Records D044–D048 rationale and trade-offs. |
| `docs/SHADOW_ENTRY_PROMOTION.md` | Defines strict, staged, manual promotion criteria. |
| `docs/SHADOW_ENTRY_VALIDATION_REPORT.md` | Records this implementation and validation result. |

## 3. Outcome collector

The collector receives normalized WebSocket ticks before strategy dispatch, but the callback performs only bounded `put_nowait()`. A worker maps each token event to registered episodes and persists results outside the callback.

One episode owns one primary path with first observed/prepared/armed/triggered/policy-eligible/executable/order/invalidation/terminal timestamps. Fixed horizons are 30 seconds, 1, 3, 5, and 15 minutes plus session cutoff and original stop-or-target resolution. Long-option entry is executable ask plus modeled slippage and costs; exit, MFE, MAE, target, and stop use executable bid less exit slippage and costs.

The collector does not fill gaps. It records bid coverage, missing intervals, feed gaps, reconnects, queue drops, and LTP-only target/stop appearances. Insufficient coverage, dropped events, or no executable exit produces `UNDETERMINABLE_DATA`. Repeated ticks and scanner cycles deduplicate by episode and event identity. Multiple policies reference the same stored path.

## 4. Replay architecture

Replay sorts by exchange timestamp, receive timestamp, then sequence and rejects duplicate ordering keys. Completed candles require the actual completion timestamp; building candles remain building; genuine, generated, backfilled, and WebSocket provenance is preserved. Every event requires instrument-master lineage, and a tick cannot precede contract availability.

Bar-only runs reject tick events and disable continuous-transmission conclusions. Replay never infers tick order from OHLC. The policy service has no `OrderService` dependency. Input data, data version, configuration hash, strategy version, policy versions, and output are hashed; identical inputs reproduce the same run ID and output hash.

## 5. Policy definitions and differences

| Behavior | Current baseline | Transition preparation | Continuous transmission |
|---|---|---|---|
| Directional completed 5m | Required | Directional or neutral/transition, never strongly opposed | Uses prepared immutable context |
| Exact 1m/5m agreement | Required where active route requires it | 1m directional; 5m may be non-opposed transition | Not replaced by a composite score |
| Premium confirmation | Retained | Not entry authority | Recorded against separate transmission fields |
| Fast promotion | Reproduced for fast route | No order promotion | State/evidence continues across promotion |
| Armed second confirmation | Retained for armed/fast routes | Never enters | Recorded but no second reset/hold timer |
| Entry authority | Shadow reproduction only | Never enterable | Shadow enterable only |
| Tick evidence | Active route semantics | Preparation diagnostics | Underlying crossing, option bid/ask progression, spread/depth, response latency, chase/room/RR |

The continuous policy records underlying, bid, ask, and diagnostic mid movements separately; progression counts; non-regressing duration; spread/depth; response latency; premium/underlying ratio; recent consistency; executable bid breakout; and second-confirmation counterfactual. It creates no arbitrary confidence score.

## 6. Shadow isolation proof

`ShadowEntryPolicyComparisonService` accepts only policies, immutable `EntryPolicyContext`, and `MarketPathEvent` values. It has no `OrderService`, `EpisodeReservationService`, `PaperTradingService`, broker provider, signal mutation, or quantity authorization dependency. Every result has `shadow_only=true` and `can_invoke_order_service=false`. Replay constructs the same isolated comparison service. Tests assert the absence of routing authority. No active entry gate, premium confirmation, second confirmation, quantity, risk tier, paper mode, or live activation setting changed.

## 7. Latency investigation

The reported 92/111/117 ms p50/p95/p99 baseline was dominated by `_ensure_*` schema inspection and legacy `ALTER` checks on every `get_session()` acquisition, compounded by synchronous evidence insertion. Migrations now run only in `init_db()`. Minimum order intent remains synchronous and atomic; large evidence is queued.

Final isolated SQLite benchmark:

| Metric | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| Final safe async pre-order | 11.945 | 14.791 | 15.678 |
| Risk-tier evaluation | 0.816 | 1.016 | 1.127 |
| Episode identity | 0.065 | 0.097 | 0.114 |
| Atomic intent reservation | 10.315 | 13.091 | 15.909 |
| Evidence enqueue | 0.031 | 0.050 | 0.068 |
| Synchronous evidence comparison | 23.868 | 25.873 | 28.372 |
| Trade persistence (post-submission lane) | 10.854 | 13.497 | 30.462 |
| Empty SQLite commit | 0.037 | 0.060 | 0.084 |
| Outcome callback enqueue | 0.003 | 0.003 | 0.004 |
| Outcome worker processing | 0.009 | 0.025 | 0.035 |

The strict 25 ms p95 target passes. Windows/SQLite scheduling produced occasional tail outliers in earlier runs; the atomic reservation remains the correct and largest synchronous component. Fast validation remains protected by its existing zero-database/zero-REST call-budget tests. The WebSocket path only enqueues outcome work; armed tick processing retains its existing measured runtime instrumentation and tests rather than being simulated by this benchmark.

## 8. Initial dataset audit

| Measure | Observed |
|---|---:|
| Sessions | 6 |
| Unique legacy episodes | 363 |
| New episode observations | 0 |
| Shadow policy decisions | 0 |
| Raw ticks | 1,795,259 |
| Ticks with bid and ask | 0.0515% |
| Underlying ticks | 343,769 (19.1487%) |
| Option ticks | 1,451,475 (80.8505%) |
| In-session missing intervals over 5 seconds | 611 |
| Missing-interval seconds | 101,950 |
| Worst interval | 7,354 seconds |
| Candles | 44,540 |
| Generated candles | 0.2784% |
| Backfilled candles identified by stored provenance | 0.0% |

Label: `INSUFFICIENT DATA`. Although the raw tick count is large, the database has only six sessions, almost no bid/ask-bearing ticks, and no observations collected by the new executable outcome schema. It cannot support preliminary policy or risk conclusions.

## 9. Policy comparison

All requested policy metrics and segments are `INSUFFICIENT DATA` because there are zero persisted shadow decisions and zero executable episode observations. Entries, rejections, expectancy, profit factor, win rate, payoff, MAE, MFE, drawdown, latency, capture, false positives, correct rejections, and missed-valid opportunities are therefore not estimated. No repeated scanner rows are substituted as independent samples, and no legacy LTP outcomes are promoted into executable results.

The reporting endpoint will produce `IN-SAMPLE`, `VALIDATION`, and `OUT-OF-SAMPLE` folds only after enough observations exist. Session phase, regime, setup family, direction, DTE, expiry day, premium, spread, and liquidity segmentation are implemented.

## 10. Risk-tier simulations

The 1%, 2%, 3%, and 5% counterfactual engine includes whole lots, affordability, stop risk, daily planned/realized limits, open risk, overlap prohibition, consecutive-loss and drawdown defense, recovery time, and worse-slippage/wider-spread/additional-loss sensitivities. It cannot produce meaningful account results from the current dataset because no executable outcomes exist. All higher tiers remain hypothetical and disabled.

## 11. Tests and checks

- Foundation before research: 404 passed.
- New focused suite: 15 passed.
- Focused research/scanner/migration/risk run: 67 passed.
- Final complete inventory, split to avoid the 10-minute buffered command limit: 263 passed + 156 passed = 419 passed.
- Post-optimization critical order/risk/recovery/shadow run: 50 passed with clean exit.
- Migration/legacy schema: passed, including new intent columns and research tables.
- Deterministic replay/building/provenance/bar-only tests: passed.
- Strict local performance benchmark: passed, final pre-order p95 14.791 ms.
- Compilation: `python -m compileall -q app tests scripts` passed.
- `ruff` was attempted as `python -m ruff check app tests scripts`; no Ruff package or lint configuration is installed.
- Warnings: four existing FastAPI `on_event` deprecation warnings in the API test group.
- Runner note: one complete N–Z invocation returned wrapper exit code 1 after pytest printed `156 passed`; the same inventory had already exited cleanly before the reservation micro-optimization, and the affected 50 critical tests then exited cleanly after it. No pytest test failure remains.

## 12. Recommendation

`CONTINUE COLLECTING DATA`.

Keep the current active policy and Tier 1 risk unchanged while the new collector accumulates executable bid/ask episode paths. Do not move either shadow policy to controlled paper until chronological validation and the documented promotion gates have adequate samples.

## 13. Remaining limitations

- Only six stored sessions and 0.0515% raw-tick bid/ask coverage are currently available.
- No new episode outcome or shadow-policy rows existed at audit time.
- Cost, slippage, partial-fill, and stop-liquidity values are models, not fill guarantees.
- Current-book liquidity remains the active conservative proxy; historical/stress estimates need setup/regime samples before validation.
- Replay can reach tick conclusions only for genuinely tick-capable sessions with instrument lineage.
- The scanner-derived shadow context exposes only evidence recorded at decision time; absent constituent/account fields remain false or unknown rather than being reconstructed later.
- Policy thresholds are frozen first-version research settings and have not been tuned or validated.
- No profitability, accuracy, causality, or higher-risk claim can be made from unit tests or the current dataset.
