# Shadow Entry Policy Promotion

This framework is advisory. It never changes runtime settings, active entry gates, order routing, or risk tiers.

## Promotion levels

1. `SHADOW_ONLY` — collection and deterministic replay only.
2. `CONTROLLED_PAPER` — explicit human approval, bounded paper allocation, active baseline unchanged as control.
3. `LIMITED_LIVE_TIER_1` — separate review after controlled-paper evidence and broker/reconciliation readiness.
4. `ELIGIBLE_FOR_TIER_2_REVIEW` — eligibility for a separate 2% risk review, not activation.
5. `ELIGIBLE_FOR_TIER_3_REVIEW` — eligibility for a separate 3% review after Tier 2 evidence.
6. `ELIGIBLE_FOR_TIER_4_REVIEW` — eligibility for a separate exceptional 5% review after every earlier level.

No level may be skipped. Promotion is never automatic.

## Required evidence

A policy must have positive after-cost expectancy using executable ask entry and bid exit; acceptable results in every major chronological fold; no catastrophic worst-fold degradation; an adequate number of unique episodes and sessions; acceptable executable-data completeness; stable worse-slippage and wider-spread stress results; acceptable drawdown, MAE, loss streak, and time to recovery; and a measurable opportunity-capture or latency improvement that justifies any false-positive change.

The result must not depend on one day, one regime, one direction, one DTE bucket, or a small number of trades. Concentration, missing intervals, undetermined outcomes, generated/backfilled provenance, and bar-only sessions must be reported. Thresholds must be frozen before the final untouched out-of-sample period. Replay must reproduce identical decision and output hashes and pass a look-ahead audit.

## Minimum review gates

- At least 100 unique out-of-sample episodes and 20 sessions for controlled-paper consideration; higher levels use the stricter risk-tier thresholds in configuration.
- At least three chronological folds with purge and embargo no shorter than the maximum evaluated position horizon.
- At least 90% required executable-data coverage, with no profitable classification for missing bid paths.
- Positive after-cost expectancy and an acceptable profit factor in combined and worst-fold results.
- Drawdown within the level-specific risk-policy bound and no hidden concentration in the five largest wins or best day.
- Stable results after worse slippage, wider spread, and one additional consecutive loss.
- Zero shadow reachability to `OrderService`, episode reservation, paper execution, or live execution during collection.

## Current disposition

All alternative entry policies remain `SHADOW_ONLY`. Active paper and live entry remain the current baseline at `TIER_1_BASE`. A later human review must explicitly authorize any controlled-paper experiment; no result in this phase authorizes live activation or higher risk.
