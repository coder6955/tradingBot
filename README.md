# AI Option Trader

This project is an AI-assisted options trading scanner for the Indian stock market.

## Current capabilities
- Scan Bank Nifty options for score-ranked option-buying setups; probability stays unavailable until calibrated evidence is sufficient.
- Use Kite Connect for NSE/NFO instruments, quotes, candles, profile, margins, positions, and orders.
- Fall back to deterministic mock data when Kite credentials are not configured.
- Check technical score, option liquidity, premium risk, stop loss, targets, quantity, and risk/reward before a trade.
- Check market regime, India VIX, price action, CPR/pivots, previous-day levels, option-chain PCR, OI support/resistance, max-pain approximation, bid/ask spread, volume, OI, timing, and configured event blocks before a trade.
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
KITE_ACCESS_TOKEN=
PAPER_TRADING_MODE=true
LIVE_TRADING_MODE=false
ACCOUNT_EQUITY=100000
MAX_RISK_PER_TRADE_PCT=1.0
MIN_SIGNAL_SCORE=80
MIN_OPTION_LIQUIDITY_SCORE=70
MIN_MARKET_REGIME_SCORE=55
MIN_PRICE_ACTION_SCORE=55
MIN_OPTION_CHAIN_SCORE=55
MIN_RISK_REWARD=1.2
MAX_BID_ASK_SPREAD_PCT=5.0
MIN_OPTION_VOLUME=500
MIN_OPTION_OI=5000
ENFORCE_MARKET_HOURS=false
BLOCKED_EVENT_DATES=
BLOCKED_SYMBOLS=
```

## Kite setup

1. Put `KITE_API_KEY` and `KITE_API_SECRET` in `.env`.
2. Run the API and open `/kite/auth`.
3. After Kite redirects to `/kite/callback`, the access token is saved through the existing token store.
4. Check `/kite/health`, `/kite/margins`, and `/kite/positions`.

Keep `PAPER_TRADING_MODE=true` while validating signals. Live orders are only submitted when the app is explicitly configured for live trading and the order request includes `confirm_live: true`.

## Run the API

```bash
uvicorn app.main:app --reload
```

Useful endpoints:

- `GET /scanner/opportunities?side=BUY&limit=10`
- `GET /scanner/opportunities?side=SELL&symbols=NIFTY,BANKNIFTY,RELIANCE`
- `GET /signals?side=BUY`
- `POST /kite/session`
- `POST /orders/place`

Kite does not issue an access token from API key and secret alone. First open `/kite/auth`, complete login, then exchange the redirected `request_token`:

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

Signals are score-ranked trade setups, not guaranteed-profit trades. Heuristic score confidence is not a calibrated probability. Validate broker margins, slippage, spread, event risk, and risk limits before enabling live orders.

## Run tests

```bash
pytest -q
```

If `pytest` is unavailable but dependencies are installed:

```bash
python -m unittest discover -s tests -p "test_*.py"
```
