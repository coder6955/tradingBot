# Architecture and Trading Decisions

## How to use this log

This file records decisions that should survive individual conversations and coding sessions. Each decision describes the current rule and its consequence. Amend the relevant record when the rationale changes; add a new record when a materially different choice replaces it.

## D001 — Keep the executable scope Bank Nifty option buying only

**Status:** Accepted  
**Decision:** `OrderService` accepts only Bank Nifty NFO `BUY_CE` and `BUY_PE` scanner signals. Expiry, strike intervals, lot size, and instrument tokens come from current broker instruments.  
**Why:** A narrow scope makes liquidity, expiry, risk, and execution assumptions testable and prevents equity/futures/general-options behavior from leaking into the engine.  
**Consequence:** Expanding to another underlying or option-selling requires an explicit design and risk review, not a symbol-list change.

## D002 — Separate hard safety gates from weighted evidence

**Status:** Accepted  
**Decision:** Hard gates block only unsafe, unavailable, or untradable entries. Directional and contextual observations normally affect a weighted score.  
**Why:** Turning every imperfect signal into a veto caused valid momentum/breakout opportunities to be missed and made the strategy difficult to calibrate.  
**Consequence:** New indicators require expectancy evidence. PCR, max pain, expected move, nearby levels, and similar context remain supporting evidence unless testing proves a risk reason for a hard gate.

## D003 — Make level and expected-move evidence breakout-aware

**Status:** Accepted  
**Decision:** Nearby resistance/support and expected-move context do not automatically reject a confirmed breakout. `ENABLE_DIRECTIONAL_ROOM_HARD_GATE` defaults to false.  
**Why:** Static room calculations behave poorly during rapid price discovery, especially on directional Bank Nifty afternoons.  
**Consequence:** These signals can reduce confidence or block only when an explicitly enabled, tested safety gate applies.

## D004 — Prewarm nearest-expiry ATM ±3 CE and PE contracts

**Status:** Accepted  
**Decision:** The default prewarm band contains the ATM strike plus three strikes on each side for both CE and PE, selected from live nearest-expiry instruments.  
**Why:** Waiting to subscribe after a rally starts loses premium history and delays trigger confirmation.  
**Consequence:** The app consumes more WebSocket subscriptions in exchange for ready prices/candles. Depth remains configurable and must stay within broker limits.

## D005 — Give every WebSocket subscription an owner

**Status:** Accepted  
**Decision:** Core market data, prewarm, armed setups, and active trades own their subscriptions independently. A token is unsubscribed only when no owner remains.  
**Why:** Direct subscribe/unsubscribe calls let one feature accidentally remove a token still needed by another.  
**Consequence:** New consumers must use a stable owner and release that owner when finished. ATM rotation subscribes the new band before retiring the old band.

## D006 — Confirm entry with bid and last price; execute against ask

**Status:** Accepted  
**Decision:** Trigger confirmation uses `min(last, bid)`, while the ask is used as the executable buy estimate when available.  
**Why:** A last-price spike without bid support can be stale or non-executable; using last price as the buy price understates slippage.  
**Consequence:** Some fast moves will be deliberately skipped when the order book does not confirm them.

## D007 — Use adaptive tick-quality confirmation

**Status:** Accepted  
**Decision:** Normal flow requires multiple above-trigger ticks, hold time, bid behavior, and stable spread. Dense flow may confirm after four qualifying ticks with a shorter 0.25-second hold.  
**Why:** A fixed long confirmation misses fast rallies, while no confirmation overreacts to one-tick noise.  
**Consequence:** Thresholds must be calibrated through deterministic replay and paper results; they are not to be removed merely for higher trade frequency.

## D008 — Normalize premium chase instead of relying only on a fixed percentage

**Status:** Accepted  
**Decision:** Entry chase is measured against recent premium range and spread-derived scale, while remaining target room, reward-to-risk, and spread remain independent checks.  
**Why:** The same percentage move can be ordinary noise for one option and severe chasing for another.  
**Consequence:** The system can react to a volatile but healthy breakout without accepting compressed reward or a widened market.

## D009 — Keep the selected contract sticky

**Status:** Accepted  
**Decision:** A Bank Nifty contract is retained for the configured stickiness period unless its spread becomes unsafe or a replacement has a material score advantage.  
**Why:** Repeatedly switching between adjacent strikes breaks candle continuity, wastes subscriptions, and can leave the armed tracker watching a moving target.  
**Consequence:** The selected contract may not be the top-scored option on every individual scan, by design.

## D010 — Treat token inactivity as diagnostic, not a global data failure

**Status:** Accepted  
**Decision:** A long gap between ticks for one option is non-blocking diagnostic information. Connection loss is entry-blocking; recovered gaps are non-blocking; token-scoped blocking events affect only that token.  
**Why:** Quiet options do not prove the WebSocket connection failed, and cancelling unrelated setups creates false negatives.  
**Consequence:** Entry still requires fresh confirming ticks, so diagnostic-only inactivity does not weaken execution safety.

## D011 — Prioritize order and risk-sensitive WebSocket events

**Status:** Accepted  
**Decision:** The WebSocket event queue processes order updates first, then gaps, then armed/active ticks, then warm ticks. Broker/database entry work runs off the callback thread.  
**Why:** Warm-market traffic should not delay order reconciliation, gap protection, entry confirmation, or exits.  
**Consequence:** Queue-pressure tests must verify priority ordering and report dropped events.

## D012 — Build premium volume from cumulative-volume deltas

**Status:** Accepted  
**Decision:** Cumulative exchange volume is converted into non-negative per-tick increments. Missing minutes receive flat zero-volume continuity candles.  
**Why:** Summing cumulative day volume on every tick massively inflates candle volume and corrupts VWAP/volume-expansion evidence. Gapped candle series also break rolling calculations.  
**Consequence:** A counter reset contributes zero for that tick; continuity candles express missing trading activity without inventing volume.

## D013 — Fast-rally events request a scan; they do not place trades

**Status:** Accepted  
**Decision:** A short-window Bank Nifty acceleration requests a serialized immediate scan with a cooldown. It never bypasses the scanner, gates, score, entry confirmation, or risk checks.  
**Why:** The system needs lower detection latency without creating a second, inconsistent strategy path.  
**Consequence:** A rally can still produce no trade when the contract, data, score, confirmation, or risk conditions are unsafe.

## D014 — Keep armed event execution paper-only until explicitly redesigned

**Status:** Accepted  
**Decision:** `ArmedEntryTrackerService` currently executes paper trades only. `ENABLE_EVENT_DRIVEN_LIVE_ENTRY` remains false, and live-mode armed ticks are blocked. The direct `OrderService` live path remains separately guarded.  
**Why:** Tick-triggered live entry requires observed latency, fill, slippage, duplicate-order, disconnect, and replay evidence before production authorization.  
**Consequence:** Enabling a setting alone must not be assumed to make armed live entry operational. Implementing it requires an explicit code change, tests, paper/shadow evidence, and updated documentation.

## D015 — Preserve deterministic tick replay

**Status:** Accepted  
**Decision:** Captured ticks are replayed in deterministic timestamp/token order without wall-clock sleeps.  
**Why:** Entry timing and race-sensitive logic must be reproducible in tests.  
**Consequence:** Exact incident reproduction depends on retaining raw tick data, including bid, ask, exchange time, and receive time.

## D016 — Keep architecture knowledge in the repository

**Status:** Accepted  
**Decision:** `docs/ARCHITECTURE.md`, `docs/TRADING_FLOW.md`, and this file are the maintained source of truth for AI and human contributors. Material changes update them in the same task.  
**Why:** Conversation memory and uploaded ChatGPT files are incomplete snapshots and cannot reliably represent the current code.  
**Consequence:** A change is not complete when it materially changes architecture or trading behavior but leaves the corresponding document stale.

## D017 — Make exchange-timestamped underlying candles canonical

**Status:** Accepted  
**Decision:** Current-session `BANKNIFTY` 1-minute WebSocket candles and completed 5-minute aggregates are the underlying analysis authority. Receive time is diagnostic, never substitute market time.  
**Why:** Quote-only caches and local receipt timestamps can fabricate indicator evidence or freshness.  
**Consequence:** Missing/stale exchange provenance fails closed for live scans; generated continuity rows are marked and only created inside the NSE session.

## D018 — Use two-stage fast candidate promotion

**Status:** Superseded by D032  
**Decision:** Tick acceleration promotes only Bank Nifty against an immutable, versioned, bounded-age slow context; stage B uses the normal scanner primitives under the shared scan lock.  
**Why:** A full-universe rescan is slow, while a separate fast strategy would diverge from scheduled decisions.  
**Consequence:** Stale or config-mismatched context rejects the fast candidate, and event-driven live entry stays disabled.

## D019 — Retain relevant raw ticks for exact ordered replay

**Status:** Accepted  
**Decision:** Underlying, prewarmed, armed, and active-trade ticks are written asynchronously with capture sequence and configurable retention.  
**Why:** Candles cannot reconstruct bid/ask order, first touch, queue pressure, or trigger timing.  
**Consequence:** Warm ticks are expendable before risk-sensitive ticks; any lost armed/active tick is visible as a critical capture gap.

## D020 — Count independent setup episodes and censor incomplete outcomes

**Status:** Accepted  
**Decision:** Repeated scan observations share a stable time-bucketed contract/direction/strategy episode. Outcomes require chronological first touch; incomplete paths are censored, and legacy current-quote outcomes remain stored but excluded.  
**Why:** Repeated polling is dependent data, and a later quote does not prove which threshold was touched first.  
**Consequence:** Gate reports distinguish observations, independent episodes, primary/co-occurring/isolated evidence, duplicates, and avoid causal claims.

## D021 — Require rolling purged out-of-sample evidence

**Status:** Accepted  
**Decision:** Validation uses multiple anchored chronological folds with an embargo no shorter than the trade horizon, per-fold results, combined out-of-sample metrics, and deterministic confidence intervals.  
**Why:** One 70/30 split is unstable and can leak overlapping outcome horizons.  
**Consequence:** Cautious-live readiness requires at least 100 independent out-of-sample trades across 20 sessions and three folds by default; paper trading remains available below that bar.

## D022 — Do not label heuristic score confidence as probability

**Status:** Accepted  
**Decision:** `probability` remains null until a separately evaluated chronological calibration model has sufficient independent data. Score-derived confidence is labeled `heuristic_score_confidence`.  
**Why:** A weighted score is not a calibrated event likelihood.  
**Consequence:** API/dashboard language must retain the distinction and store the probability source/calibration version.

## D023 — Fail live routing closed on strategy-config drift

**Status:** Accepted  
**Decision:** Strategy/config lineage is recorded on decision and execution artifacts. Mixed hashes are reported rather than silently combined; live routing blocks when the active registered version has config drift.  
**Why:** Evidence from materially different policies is not interchangeable.  
**Consequence:** Paper mode can continue with a warning. Operators must register/activate the next version after reviewing `.env.example`; the machine-specific `.env` is never overwritten.

## D024 — Use a reviewed official constituent snapshot, never a hot-path download

**Status:** Accepted
**Decision:** Bank Nifty participation uses all 14 members and official weights from a locally versioned NSE Indices snapshot with effective date, source, lineage, staleness and available-weight coverage.
**Why:** Hardcoded legacy weights and count-only coverage can overstate alignment, while network/PDF retrieval in the entry path is slow and fragile.
**Consequence:** Stale snapshots remain diagnostic but lose hard-gate authority. Refresh tooling validates totals and requires manual review for membership changes.

## D025 — Exit long options against executable bids

**Status:** Accepted
**Decision:** Stops, targets, time exits and invalidations use a conservative sell-side executable price. Full bid depth is quantity-weighted; partial depth cannot prove a target; LTP-only spikes never fill exits.
**Why:** A last trade is not necessarily available when selling, especially in fast or thin option markets.
**Consequence:** Paper and live decisions retain LTP for diagnostics but persist bid/depth evidence. Live software exits fail closed without full quantity-safe depth.

## D026 — Give overlapping exit rules deterministic priority and honest attribution

**Status:** Accepted
**Decision:** Priority is stop, time/near-close, trailing, invalidation, then targets. The first rule and every simultaneous trigger are stored; analytics do not infer causal improvement without counterfactual paths.
**Why:** Iteration order otherwise changes outcomes and makes multiple-trigger trades look attributable to the most convenient label.
**Consequence:** Optional exits remain disabled, shadow-only or unchanged until purged out-of-sample evidence supports them.

## D027 — Require broker-side disaster protection for confirmed live fills

**Status:** Accepted
**Decision:** Live entry is blocked when broker protection is required but disabled. Complete or partial fills must receive a reconciled broker SL-M order; remaining partial-entry quantity is cancelled first. Missing IDs, rejection, cancellation or placement failure persistently blocks new live entries.
**Why:** A process, network or WebSocket failure must not leave a live long-option position without a broker-resident loss boundary.
**Consequence:** `ENABLE_BROKER_EMERGENCY_SL=false` keeps live trading blocked by default. Software/protective exits coordinate to prevent duplicate sells.

## D028 — Make professional readiness multi-fold, after-cost and regime-aware

**Status:** Accepted
**Decision:** Readiness requires at least 100 independent out-of-sample trades, 20 sessions, three purged/embargoed folds, positive after-cost expectancy, minimum profit factor, bounded drawdown, and sufficient stable evidence in traded trend/range/volatile/event/expiry regimes. Zero-trade evidence fails explicitly.
**Why:** Scanner observations are dependent, and one aggregate split can conceal leakage, regime concentration or costs.
**Consequence:** Missing regimes or insufficient samples keep readiness and live scaling blocked; thresholds are not optimized on evaluation folds.

## D029 — Use hierarchical state, phase, and setup policies

**Status:** Superseded for active entry by D032; retained as shadow research
**Decision:** Structure, volatility, participation, location and execution form a confidence/uncertainty-aware market state. Multi-timeframe responsibilities are fixed, momentum has an explicit lifecycle, and each setup family owns its entry, invalidation and exit policy.
**Why:** One aggregate score cannot distinguish formation from exhaustion, a trend from a transition, or price momentum from option-buying suitability.
**Consequence:** Only unsafe or untradable conditions become hard gates. Weighted inputs stay capped, every abstention has a code, and setup-family score adjustments are bounded.

## D030 — Persist armed intent and report subscription truthfully

**Status:** Accepted
**Decision:** Armed setups are durable, versioned records. Recovery rehydrates valid setups and owner subscriptions. Registration exposes queued, subscribed-awaiting-tick, live-verified and failed subscription states.
**Why:** An in-memory armed label can survive neither restart nor subscription failure and can falsely imply that a rally trigger is being watched.
**Consequence:** Subscription failure cancels arming; disconnected queues are visibly degraded; owner-based overlap and stickiness remain responsible for token lifecycle.

## D031 — Keep promotion advisory and research off the hot path

**Status:** Accepted
**Decision:** Heavy readiness runs only in the after-market worker lane; ordinary readiness requests serve completed cache. Evidence is segmented by setup, regime, time, DTE, direction, volatility, execution and participation, and promotion never changes runtime configuration.
**Why:** Research work can delay trigger/order processing, and a self-modifying live strategy destroys lineage and invites overfitting.
**Consequence:** Rejected observations never enter trade expectancy, insufficient cells fail visibly, and a human must register any new strategy version.

## D032 — Use a two-timeframe cache-first experimental entry path

**Status:** Accepted as experimental v5  
**Decision:** Active entry evaluation uses completed 5-minute candles for setup direction, regime, day/opening structure and candle confirmation, completed 1-minute candles for entry timing, and WebSocket ticks for execution. Scheduled scans refresh one immutable slow context from a bulk 1m/5m read. Fast-rally validation consumes only that context and prewarmed in-memory option bid/ask/depth.  
**Why:** The former fast-rally path validated a cache and then reran the complete scanner, causing avoidable REST, database and persistence latency. Higher-timeframe and overlapping policy layers added complexity without independent evidence of incremental expectancy, and replay dependencies could observe future database state.  
**Consequence:** Daily/15m/30m strategy candles are unsupported. Fast validation has a zero-REST/zero-database pre-confirmation budget and rejects stale evidence without fallback. Active gates are limited to session/data/feed safety, 1m/5m agreement, constituent and premium participation, executable quote quality, chase/remaining reward-risk and account risk. Hierarchical state, momentum, setup-family adjustments, candidate utility, volatility/learned edge and duplicate regimes are shadow-only. Replay is timestamp-bounded, repeated rejections are episode-deduplicated, live event entry remains disabled, and promotion remains manual.

## D033 — Remove legacy indicator and score authority from v5 eligibility

**Status:** Accepted as a v5 correctness fix  
**Decision:** Current-session analysis becomes ready after at least six completed 5-minute candles and a fresh LTP. Direction is classified from the completed multi-candle price sequence and must then agree with completed 1-minute structure. EMA, MACD, RSI and ADX remain diagnostic-only. `MIN_SIGNAL_SCORE` remains a legacy/manual safety guard, but current-version scanner signals may pass signal creation and order validation below it only when they carry explicit proof that all primary gates passed and score is ranking-only.  
**Why:** The implementation incorrectly required 26 current-session 5-minute candles solely to calculate EMA/MACD, suppressing every morning candidate until roughly 11:25, and hidden score checks contradicted D032 even after the scanner gates passed.  
**Consequence:** With uninterrupted canonical data, structural analysis can start after the sixth completed 5-minute candle (normally about 09:45 IST) without allowing one candle or an indicator crossover to select CE/PE. Neutral structure still abstains, 1m/5m disagreement still blocks, all execution and account-risk gates remain intact, and untrusted/manual low-score signals remain blocked.

## D034 — Give WebSocket reconnect ownership exclusively to KiteTicker

**Status:** Accepted  
**Decision:** KiteTicker's retry factory is the only component allowed to schedule reconnects. Application error/close callbacks record state and gaps but do not call `reconnect()`. The SDK receives bounded retry/delay settings. Authentication, market-close and broker-rate-limit callbacks call `stop_retry()`, and a broker 429 blocks all newly requested WebSocket starts for a configurable cooldown.  
**Why:** KiteTicker already automatically retries a lost connection and invokes both error and close callbacks for one unclean loss. Manually reconnecting from both callbacks created competing attempts; setting only the application `running` flag on 429 did not stop the SDK factory or later subscription-driven starts.  
**Consequence:** One `1006` produces one SDK-managed exponential retry sequence. A `429 TooManyRequests` produces no further upgrade attempts during the default 120-second cooldown, desired subscriptions remain queued, and runtime status identifies `kite_sdk` as reconnect owner plus the cooldown deadline/remaining seconds.

## D035 — Make runtime continuity evidence-driven and observable

**Status:** Accepted
**Decision:** A boot-managed automation supervisor persists one lifecycle run per process, survives the after-market pipeline, repairs stale child-task state, and records unclean prior runs. A WebSocket connection gap clears only on a confirmed connection or verified fresh tick. Every fast-rally threshold crossing records dispatch and cached-validation outcomes, and cached validation fails closed on any measured REST or database I/O.
**Why:** A boolean `running` flag can outlive a dead task, an after-market self-stop can leave the next session unattended, reconnect-attempt callbacks do not prove data resumed, and silent cooldown/error suppression makes missed rallies impossible to diagnose.
**Consequence:** Mid-session process replacement is auditable, intraday workers restart without manual intervention, stale feed gaps self-heal only from positive evidence, and the decision feed explains every detected rally from dispatch through validation. Live risk and order gates remain unchanged.

## D036 — Replace average direction with explicit price structure and an opening policy

**Status:** Accepted as experimental v6  
**Decision:** CE/PE direction is based on completed-candle swing progression, multi-candle impulse breadth, controlled pullback retention and breakout acceptance. Until 09:45 IST, opening readiness requires five completed 1-minute and three completed 5-minute candles; normal-session readiness remains six on both frames.  
**Why:** Average relationships obscure whether movement is impulse, pullback or accepted breakout, and the v5 six-by-five-minute requirement could not act before roughly 09:45.  
**Consequence:** A single candle still cannot choose direction, 1m/5m agreement remains mandatory, and the app can evaluate a real opening-drive setup from about 09:30 when canonical data is continuous.

## D037 — Use one normalized opportunity model and promote prepared fast plans

**Status:** Accepted as experimental v6  
**Decision:** Scheduled timing, fast-rally validation and armed-tick execution use `EntryOpportunityService` for normalized chase, target room and remaining reward/risk. Expected-move and nearby-level context cease being unconditional vetoes only after breakout acceptance. Scheduled scans cache a complete paper plan; fast validation may promote it into the normal durable armed-entry path after zero-I/O validation.  
**Why:** Three inconsistent late-entry calculations could reject the same price differently, while a validator that only observed an already-armed setup could not recover a fast rally missed between scans.  
**Consequence:** The actual ask is still rechecked, account risk and subscription health still fail closed, validation remains zero-I/O, promotion/persistence occurs outside that budget, and live event entry remains disabled.

## D038 — Prefer executable DTE/delta contracts and manage a whole-lot runner

**Status:** Accepted as experimental v6  
**Decision:** Expiry-day buying policy is applied during selection, so a blocked same-day expiry moves selection to the next broker-listed expiry. Ranking prefers executable liquidity, configured DTE and delta without requiring unavailable Greeks. Exit profiles book one exchange-valid whole-lot partial at a configured R multiple only when at least two lots exist, then trail the runner from premium high-watermark using option ATR and original risk. Trend/expansion setups receive a longer conditional time stop.  
**Why:** Selecting a contract that later fails the expiry gate wastes the setup; fixed full-target exits truncate trend days; fractional-lot partials are not executable; and one universal 15-minute stop ignores setup behavior.  
**Consequence:** One-lot trades remain indivisible, normal/reversal time stops remain tighter, live partials stay disabled until broker-fill confirmation is built, and chronological after-cost evidence is still required before any live promotion.
