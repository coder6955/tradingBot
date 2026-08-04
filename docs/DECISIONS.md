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

## D039 — Separate eligibility, evidence classification, risk, exposure, and execution

**Status:** Accepted  
**Decision:** `RiskPolicyService` is the only tier and stop-risk sizing authority. Scanner score, indicator agreement, and heuristic confidence cannot select a tier. Active paper/live default to base risk; higher tiers require explicit policy enablement and versioned chronological after-cost evidence.  
**Why:** A qualified setup can still be risk-infeasible, and a high uncalibrated score is not evidence that loss size should increase.  
**Consequence:** The supported 1/2/3/5% spectrum is infrastructure, not an aggressive active policy. Invalid configured ceilings fail closed, 5% is a hard process limit, and one-lot infeasibility returns quantity zero.

## D040 — Put every paper and live entry behind one final pre-order authority

**Status:** Accepted  
**Decision:** Every `OrderService` route invokes `PreOrderRiskService` immediately before episode reservation and submission. It validates account state, active tier, modeled stop loss, daily/open risk, quantity, liquidity, session, duplicates, and conditional live protections.  
**Why:** Paper bypasses and route-specific risk implementations corrupt research and can diverge from live behavior.  
**Consequence:** Paper and live share strategy/account eligibility and differ only in execution mechanics and broker protections. Scanner quantity is provisional and cannot authorize an order.

## D041 — Reserve setup episodes transactionally

**Status:** Accepted  
**Decision:** Strategy/risk-policy/contract/setup/trigger/date/window identity owns an atomic `AVAILABLE -> RESERVED -> ORDER_PENDING -> OPEN -> CLOSED` persistence lifecycle. `AVAILABLE` maps to canonical `PREPARED`, `RESERVED` maps to `TRIGGERED`, and the full canonical transition vocabulary also includes `UNAVAILABLE`, `OBSERVE`, `ARMED`, `EXITING`, `INVALIDATED`, and `EXPIRED`.  
**Why:** In-memory or live-only duplicate sets cannot prevent concurrent scanner, fast, WebSocket, paper, manual, and live routes from entering the same opportunity.  
**Consequence:** A failed pre-submission attempt releases its reservation; an accepted order holds the episode until closure. Terminal episode identity cannot be reused; a new pullback/breakout requires a new trigger identity or episode window.

## D042 — Keep risk research immutable and shadow-only

**Status:** Accepted  
**Decision:** Scanner, fast and pre-order decisions append versioned context/gate/risk records. Outcomes append separately. Shadow policies calculate 1/2/3/5% counterfactual quantities and outcomes but expose `counterfactual_can_reach_order_router=false`.  
**Why:** Reconstructing risk decisions from combined confidence or mutable rows cannot establish independent expectancy or safe tier promotion.  
**Consequence:** Fast validation retains zero database and REST I/O; persistence occurs after its measured critical section. Risk-of-ruin remains null unless independent sample size and distribution assumptions are disclosed.

## D043 — Make reservation recovery and paper persistence fail safely

**Status:** Accepted as a foundation audit correction  
**Decision:** Reclaiming an expired reservation uses the prior state, reservation token, and expiry timestamp as one compare-and-swap predicate. If paper execution succeeds but durable trade creation fails, the exact in-memory paper position is rolled back before the episode is released. If a durable paper trade exists but the episode cannot move to `OPEN`, the episode remains locked in `ORDER_PENDING` for reconciliation.  
**Why:** Checking only `state=RESERVED` allowed two contenders to overwrite the same expired reservation, and releasing an episode after a persistence failure could permit a duplicate while an untracked paper position remained open.  
**Consequence:** Exactly one concurrent worker can reclaim an expired episode. A failed, non-durable paper entry leaves neither a position nor a locked episode; a durable entry never releases its duplicate lock merely because the final state transition needs repair.

## D044 â€” Commit safety intent synchronously and write large evidence asynchronously

**Status:** Accepted  
**Decision:** Final authorization atomically creates `ORDER_PENDING` plus a minimum order intent. Large immutable evidence uses a bounded retrying worker and is reconstructable from that intent. Schema inspection runs only during initialization.  
**Why:** Per-session legacy-schema inspection and synchronous JSON insertion dominated the reported 111 ms p95 without contributing to order safety.  
**Consequence:** Queue saturation fails closed before submission; broker-accepted/local-write interruptions remain locked and visible to reconciliation. The same SQLite benchmark now meets the 25 ms p95 engineering target without removing a risk or duplicate gate.

## D045 â€” Judge research outcomes only from executable episode paths

**Status:** Accepted  
**Decision:** One collector follows each unique episode using ask for hypothetical long entry and bid for exit/MFE/MAE/target/stop, after modeled costs. Missing quotes, gaps, dropped events, and insufficient coverage remain undetermined.  
**Why:** Repeated scans and LTP-only moves overstate sample size and tradable expectancy.  
**Consequence:** Policies share one market path; no profitable classification exists without sufficient executable coverage.

## D046 â€” Replay event time deterministically and isolate shadow policies structurally

**Status:** Accepted  
**Decision:** Replay orders exchange time, receive time, then sequence; preserves building/completed candle state, provenance, gaps, and instrument lineage; and disables tick conclusions for bar-only sessions. Shadow comparison has no order-routing dependency.  
**Why:** Future bar values, fabricated OHLC tick order, or a reachable order service invalidate research and create operational risk.  
**Consequence:** Identical data/config/strategy produces identical hashes and decisions. Shadow policies cannot reserve, mutate active quantity, or submit paper/live orders.

## D047 â€” Use one audited executable-bid account state

**Status:** Accepted  
**Decision:** Risk reads start/current/peak equity, realized P&L, executable-bid unrealized P&L, planned/open-stop risk, exposure, drawdown, and losses from one calculation. Missing bids are valued at zero and block entry.  
**Why:** Daily realized P&L divided by current cash is not reproducible drawdown and ignores open option losses.  
**Consequence:** Higher requests remain defensive and evidence-gated; audited snapshots are durable outside the critical order path.

## D048 â€” Keep entry and risk promotion manual and chronological

**Status:** Accepted  
**Decision:** Compare unique episode-policy pairs using chronological training, validation, and untouched out-of-sample folds with purge/embargo at least as long as the maximum horizon. Simulate 1/2/3/5% only as counterfactuals and require staged promotion.  
**Why:** Aggregate return, random folds, repeated scanner rows, or one strong day cannot justify an entry-policy or risk increase.  
**Consequence:** This phase cannot activate a shadow policy or higher risk. Insufficient executable data returns `INSUFFICIENT DATA`, not a promotion.

## D049 — Isolate paper capital from the connected broker account

**Status:** Accepted  
**Decision:** Paper scanner sizing, account percentage caps, armed-entry preflight and final pre-order risk use the fixed configured `ACCOUNT_EQUITY`, which defaults to ₹1,00,000. Only live trading uses Zerodha funds and audited broker equity for risk sizing and affordability.  
**Why:** Paper research must remain reproducible and must not stop, resize or become more aggressive merely because cash is deposited into or withdrawn from the connected live broker account.  
**Consequence:** Paper still enforces realized/unrealized paper P&L, premium exposure, planned/open-stop risk, loss streak, trade limits and executable-bid coverage, but calculates percentage budgets against the fixed simulation capital. Live continues to fail closed when broker equity is unavailable.

## D050 — Use an explicit operator-selected four-percent active budget

**Status:** Accepted by explicit operator direction  
**Decision:** `ACTIVE_PAPER_RISK_BUDGET_PERCENT` and `ACTIVE_LIVE_RISK_BUDGET_PERCENT` are both 4%. Paper applies this to the fixed ₹1,00,000 daily capital base; live applies it to current Zerodha equity. The active per-trade ceiling, realized daily-loss cap and simultaneous Bank Nifty/open-risk caps are 4%. Cumulative daily planned risk is 8%, allowing another qualified trade after a winner or a smaller-risk attempt while one full-budget realized loss still ends new entries for the day.  
**Why:** A one-percent budget rejected otherwise qualified one-lot opportunities when modeled stop risk marginally exceeded ₹1,000. The operator explicitly accepts a higher loss budget and wants quantity reduced before rejecting a qualified setup.  
**Consequence:** Risk sizing floors quantity to exchange-valid whole lots and never widens the strategy stop to consume budget. If one lot including modeled slippage and allocated costs exceeds ₹4,000 in paper, or 4% of actual equity in live, the trade remains rejected. The 1/2/3/5% research tier spectrum and evidence requirements remain separate; the 4% operator budget is versioned as `banknifty_risk_v2` and must not be described as validated expectancy. Live mode, broker affordability, protective-stop and reconciliation gates remain unchanged.

## D051 — Bootstrap one dated Kite token before runtime startup

**Status:** Accepted by explicit operator direction  
**Decision:** Repository-root `access_token.txt` is the sole runtime access-token store. It is git-ignored, written atomically with India trading date and creation metadata, and rejected when stale or malformed. Before starting any broker, WebSocket, scanner or automation work, startup validates today's file token, migrates a valid legacy `.env` token once, or performs configured headless Kite login with credentials read only from `.env`.  
**Why:** Daily manual request-token exchange delays unattended startup, while an undated plaintext token or hardcoded password/TOTP secret creates leakage and stale-session risk.  
**Consequence:** `KITE_USER_ID`, `KITE_PASSWORD` and the Base32 `KITE_TOTP_SECRET` require one-time local configuration. The current six-digit OTP is not stored. Token values and profile data are not logged. A failed login cannot fall back to yesterday's token; Kite-dependent modules remain unavailable and manual `/kite/auth` remains a recovery route. Selenium/Chrome availability and broker login-page changes are operational dependencies, so startup exposes a redacted bootstrap status.

## D052 — End Windows scheduled runs on durable after-market completion

**Status:** Accepted  
**Decision:** The Windows scheduled runner exits only after the supervisor records `after_market_pipeline_completed`, then requests graceful Uvicorn shutdown and sends a Telegram safe-to-power-off notification. Continuous/manual app runs still retain their overnight supervisor. A 20-hour Task Scheduler limit remains only as a stuck-process circuit breaker.  
**Why:** A fixed eight-hour window can terminate unfinished research after a late start, while leaving every boot-managed process alive overnight prevents a fresh next-day token bootstrap.  
**Consequence:** Normal scheduled shutdown is driven by completed outcome/research work and flushes application shutdown hooks. Telegram credentials are optional but required for remote notification; notification failure does not prevent safe process shutdown.

## D053 — Count only qualified underlying coverage as a research session

**Status:** Accepted  
**Decision:** A research date counts toward session-based strategy readiness only when regular-market `BANKNIFTY`/`NIFTY BANK` underlying ticks satisfy configurable start/end tolerance, minimum coverage, and maximum internal-gap requirements. The audit retains incomplete dates as `PARTIAL` and reports their reasons.  
**Why:** Starting the app late, shutting it early, or losing the feed creates a tick-bearing date but not an independent full trading session. Counting such dates overstates regime and time-of-day evidence.  
**Consequence:** The 20-session mechanical evidence gate uses only qualified complete sessions. Option activity cannot repair missing underlying coverage. Partial dates may contribute only independently complete episode paths and cannot justify full-session strategy conclusions.

## D054 — Restrict hard gates to safety, availability, tradability, and numerical opportunity

**Status:** Accepted by explicit operator direction; active as experimental v7  
**Decision:** Permanent rejection gates are limited to universal session enforcement, canonical/fresh/gap-safe data, valid five-minute direction, valid current contract and executable book, spread/depth, expiry/minimum premium, valid stop/target geometry, numerical chase/target-room/remaining-RR, account/duplicate risk, and live broker safety. Neutral five-minute structure remains `WATCHING_SETUP`. One-minute opposition and extreme opposing-heavyweight participation pause entry. Premium momentum/breakout readiness, ordinary bank alignment, option quality, composite liquidity, volume, OI, expected-move and nearby-level context are timing/ranking/warning evidence rather than permanent rejection gates. Missing or stale premium evidence remains an unavailable-data failure.  
**Why:** The accumulated evidence contains zero qualified complete sessions. Unvalidated confirmation gates were discarding potentially useful setups and conflating alpha hypotheses with safety. Retaining safe setups as watching/armed preserves opportunity while keeping execution and account protections intact.  
**Consequence:** `banknifty_option_buying_v7` records watching states without inserting rejected-opportunity rows. A premium trigger can later promote the same setup; weak contextual scores cannot authorize an unsafe trade. The universal pre-order session switch defaults to and is configured `true`. Re-promotion of any soft signal to a rejection gate requires gate-specific independent chronological evidence across qualified sessions.
