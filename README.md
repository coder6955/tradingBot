# AI Option Trader

This project is an AI-assisted options trading scanner for the Indian stock market.

## Current capabilities
- Scan Bank Nifty options for score-ranked option-buying setups; probability stays unavailable until calibrated evidence is sufficient.
- Use Kite Connect for NSE/NFO instruments, quotes, candles, profile, margins, positions, and orders.
- Fall back to deterministic mock data when Kite credentials are not configured.
- Hard-block entries only for unsafe, stale, unavailable, or untradable conditions: universal session/data safety, five-minute direction availability, valid contract/book, spread/depth, numerical chase/reward-risk, and risk preflight. Neutral or incompletely confirmed setups stay watching/armed instead of becoming permanent rejections.
- Treat one-minute opposition and extreme heavyweight opposition as temporary waits. Keep premium quality, ordinary constituent alignment, option quality, volume, OI and composite liquidity as timing/ranking evidence.
- Keep broader market regime, India VIX, price action, option-chain context, volatility, momentum, setup-family, and learned-edge analysis as shadow diagnostics until measured evidence justifies promotion.
- Build one immutable fast-scan context on the scheduled path. WebSocket rally events validate that cached context in memory without broker REST calls, database reads, synchronous fallback scans, or a second order-routing path.
- Rank executable contracts and candidates using ask/bid, spread, depth, OI/volume, Greeks/DTE when available, reward/risk, costs, uncertainty, and contract stickiness.
- Persist/recover armed entries with owner-based WebSocket subscriptions and expose whether each token is queued, subscribed, or fresh-tick verified.
- Use setup-family time/trailing/target exit profiles while preserving hard stops, and keep research/evidence promotion outside the live scanner path.
- Route orders to paper trading by default. Live Kite orders require `LIVE_TRADING_MODE=true`, `PAPER_TRADING_MODE=false`, and `confirm_live=true`.
- `/scanner/opportunities` returns only real executable option contracts when Kite market data is enabled. It does not return placeholder strikes or fallback prices.

## Setup

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set the Kite and risk values you need:

```bash
KITE_API_KEY=
KITE_API_SECRET=
# Deprecated after one-time migration to access_token.txt:
KITE_ACCESS_TOKEN=
KITE_AUTO_LOGIN_ENABLED=true
KITE_AUTO_LOGIN_HEADLESS=true
KITE_AUTO_LOGIN_TIMEOUT_SECONDS=60
KITE_USER_ID=
KITE_PASSWORD=
# Base32 authenticator setup key, not the current six-digit OTP:
KITE_TOTP_SECRET=
PAPER_TRADING_MODE=true
LIVE_TRADING_MODE=false
ACCOUNT_EQUITY=100000
MAX_RISK_PER_TRADE_PCT=4.0
MIN_SIGNAL_SCORE=80
MIN_OPTION_LIQUIDITY_SCORE=70
MIN_MARKET_REGIME_SCORE=55
MIN_PRICE_ACTION_SCORE=55
MIN_OPTION_CHAIN_SCORE=55
MIN_RISK_REWARD=1.2
MAX_BID_ASK_SPREAD_PCT=5.0
MIN_OPTION_VOLUME=500
MIN_OPTION_OI=5000
ACTIVE_DECISION_TIMEFRAMES=1minute,5minute
FAST_SCAN_CONTEXT_REFRESH_SECONDS=30
SCHEDULED_SCAN_MAX_REST_CALLS=3
ENFORCE_MARKET_HOURS=true
BLOCKED_EVENT_DATES=
BLOCKED_SYMBOLS=
```

## Kite setup

1. Put `KITE_API_KEY` and `KITE_API_SECRET` in `.env`.
2. For unattended daily login, set `KITE_AUTO_LOGIN_ENABLED=true`, then add `KITE_USER_ID`, `KITE_PASSWORD`, and `KITE_TOTP_SECRET`. The TOTP value is the Base32 setup key shown when authenticator TOTP is enabled, not the changing six-digit code.
3. Start the API. Before WebSocket, scanner, broker sync, or automation starts, the app validates or creates today's git-ignored `access_token.txt`.
4. If automatic login fails, open `/kite/auth`; `/kite/callback` and `/kite/session` save into the same dated token file.
5. Check `/kite/health`, `/kite/margins`, and `/kite/positions`.

Keep `PAPER_TRADING_MODE=true` while validating signals. Live orders are only submitted when the app is explicitly configured for live trading and the order request includes `confirm_live: true`.

## Run the API

```bash
uvicorn app.main:app --reload
```

### Unattended weekday startup on Windows

The production-style local launcher is `scripts/start_scheduled_trading_app.ps1`.
The registered Windows task starts it at 09:00 every Monday-Friday under the
configured Windows user, including while the machine is at the sign-in screen.
It starts when a scheduled run was missed, waits for network availability, can
wake the laptop, retries failures, permits battery operation, ignores duplicate
starts, waits for the after-market outcome and research pipeline to complete, and
then shuts Uvicorn down gracefully. A 20-hour Task Scheduler limit is retained
only as an emergency hung-process ceiling. Per-run output is written to the
git-ignored `logs/` directory.

For the final safe-to-power-off alert, configure `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` in `.env`. The scheduled runner sends the message only after
the after-market pipeline has completed and application shutdown has finished.

Install or update the task from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_trading_app_task.ps1
```

Useful endpoints:

- `GET /scanner/opportunities?side=BUY&limit=10`
- `GET /scanner/opportunities?side=SELL&symbols=NIFTY,BANKNIFTY,RELIANCE`
- `GET /signals?side=BUY`
- `POST /kite/session`
- `POST /orders/place`

For manual recovery, Kite does not issue an access token from API key and secret alone. Open `/kite/auth`, complete login, then exchange the redirected `request_token`:

```json
{
  "request_token": "paste_request_token_here"
}
```

Example paper order body using a signal returned by the scanner:

```json
{
  "confirm_live": false,
  "signal": {
    "symbol": "NIFTY",
    "action": "BUY_CE",
    "side": "BUY",
    "tradingsymbol": "NIFTY24JUN22000CE",
    "exchange": "NFO",
    "entry_price": 100,
    "stop_loss": 78,
    "quantity": 50,
    "score": 85
  }
}
```

Signals are score-ranked trade setups, not guaranteed-profit trades. Scores rank candidates but do not override the primary safety and execution gates. Heuristic confidence is not a calibrated probability. Keep paper mode enabled until replay, latency, slippage, and protective-order verification pass.

## Run tests

```bash
pytest -q
```

If `pytest` is unavailable but dependencies are installed:

```bash
python -m unittest discover -s tests -p "test_*.py"
```
