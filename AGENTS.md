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

## Documentation Source of Truth

- Treat `docs/ARCHITECTURE.md`, `docs/TRADING_FLOW.md`, and `docs/DECISIONS.md` as the maintained source of truth for the current system.
- Before changing architecture, trading logic, configuration, state transitions, WebSocket behavior, order routing, exits, or risk management, read the relevant maintained documents.
- After a material change to any of those areas, update the affected maintained documents in the same task.
- Use `docs/ARCHITECTURE.md` for current components and runtime wiring, `docs/TRADING_FLOW.md` for current trading behavior and states, and `docs/DECISIONS.md` for rationale and trade-offs.
- Treat root-level architecture maps, audits, and fix plans as supplemental historical reports when they disagree with the maintained documents or current code.
- Do not rely on chat history, AI memory, or uploaded document snapshots as the only record of current application behavior.

## Testing Expectations

- Always include tests for trading-logic changes, or explain clearly why tests cannot be added.
- When tests cannot run locally, report the exact command attempted and the failure reason.
- Do not leave manual test/demo trades open in the real app database. If a test creates a trade through the running app, close it through the app flow before finishing.
- Prefer isolated temporary databases and mocked broker providers for tests. Never create fake live broker rows such as `broker_order_id=test-order` in the real MySQL database.
- Before reporting manual API/order testing as complete, verify `/trades`, `/trades/exit-alerts`, and `/broker/reconciliation/status` do not show stale test rows or live reconciliation mismatches.

@RTK.md
