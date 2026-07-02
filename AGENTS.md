# Repository Guidance

## Project Scope

- This repository is a Bank Nifty NSE option-buying app.
- Keep the system focused on Bank Nifty option buying unless the user explicitly asks to expand scope.
- Never hardcode Bank Nifty expiry assumptions; use live instrument data from the broker/provider.

## Trading Engine Rules

- Keep hard gates and weighted scoring separate.
- Hard gates should block only unsafe, unavailable, or untradable opportunities.
- Do not convert weighted score inputs into hard gates without a clear risk reason.
- Keep live trading conservative.
- Never bypass risk checks.
- Always log rejected and accepted opportunities with reasons.

## Strategy Discipline

- Do not add indicators unless they improve tested expectancy.
- Prefer simple, testable, explainable trading logic over complex indicator stacking.
- Treat PCR, max pain, and similar context signals as supporting evidence unless tested results justify stronger use.
- Cap correlated trend and momentum evidence so confidence is not inflated by duplicate signals.

## Testing Expectations

- Always include tests for trading-logic changes, or explain clearly why tests cannot be added.
- When tests cannot run locally, report the exact command attempted and the failure reason.

@RTK.md
