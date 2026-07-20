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

**Status:** Accepted  
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
