from dataclasses import asdict

from fastapi import FastAPI, HTTPException

from app.config import settings
from app.services.market_data_service import MarketDataService
from app.services.scanner_service import ScannerService
from app.services.signal_repository import SignalRepository
from app.services.order_service import OrderService
from app.providers.kite_provider import KiteProvider
from app.providers.token_store import save_access_token, load_access_token
from fastapi.responses import RedirectResponse, HTMLResponse

app = FastAPI(title=settings.app_name, version="0.3.0")
market_data_service = MarketDataService()
scanner_service = ScannerService()
signal_repository = SignalRepository()
kite_provider = KiteProvider()
order_service = OrderService(kite_provider=kite_provider)

# If an access token is present in .env, ensure provider picks it up
existing_token = load_access_token()
if existing_token:
    try:
        kite_provider.set_access_token(existing_token)
        try:
            kite_provider.client.set_access_token(existing_token)  # type: ignore
        except Exception:
            pass
    except Exception:
        pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/")
def root() -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "docs": "/docs",
        "health": "/health",
        "scanner": {
            "buy": "/scanner/opportunities?side=BUY",
            "sell": "/scanner/opportunities?side=SELL",
        },
        "kite": {
            "login": "/kite/auth",
            "session": "/kite/session",
            "health": "/kite/health",
        },
    }


@app.get("/market/{symbol}")
def get_market_summary(symbol: str) -> dict[str, object]:
    return market_data_service.get_market_summary(symbol)


@app.get("/signals")
def get_signals(side: str = "BUY", limit: int = 10) -> list[dict[str, object]]:
    recommendations = scanner_service.scan_symbols(
        side=side.upper(),
    )
    for signal in recommendations:
        signal_repository.save_signal(
            symbol=signal.symbol,
            action=signal.action,
            score=signal.score,
            confidence=signal.confidence,
            trend="bullish" if signal.action == "BUY_CE" else "bearish",
            explanation=signal.explanation,
        )
    return [asdict(signal) for signal in recommendations[:limit]]


@app.get("/scanner/opportunities")
def get_opportunities(side: str = "BUY", symbols: str | None = None, limit: int = 10) -> dict[str, object]:
    symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
    recommendations = scanner_service.scan_symbols(symbols=symbol_list, side=side.upper())
    return {
        "mode": "live" if settings.live_trading_mode else "paper",
        "side": side.upper(),
        "count": len(recommendations),
        "opportunities": [asdict(signal) for signal in recommendations[:limit]],
        "disclaimer": "Signals are probability-ranked trade setups with risk checks, not guaranteed profits.",
    }


@app.get("/scanner/diagnostics")
def get_scanner_diagnostics(side: str = "BUY", symbols: str | None = None, limit: int = 25) -> dict[str, object]:
    symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
    diagnostics = scanner_service.scan_with_diagnostics(symbols=symbol_list, side=side.upper())
    rows: list[dict[str, object]] = []
    for item in diagnostics[:limit]:
        row = dict(item)
        signal = row.get("signal")
        row["signal"] = asdict(signal) if signal is not None else None
        rows.append(row)
    return {
        "mode": "live" if settings.live_trading_mode else "paper",
        "side": side.upper(),
        "count": len(rows),
        "diagnostics": rows,
    }


@app.post("/orders/place")
def place_order(payload: dict[str, object]) -> dict[str, object]:
    from app.models import Signal

    try:
        signal_payload = payload.get("signal")
        if not isinstance(signal_payload, dict):
            signal_payload = {key: value for key, value in payload.items() if key != "confirm_live"}
        signal = Signal(**signal_payload)  # type: ignore[arg-type]
        confirm_live = bool(payload.get("confirm_live", False))
        return order_service.place_signal_order(signal, confirm_live=confirm_live)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc




@app.get("/kite/login")
def kite_login() -> dict[str, str]:
    try:
        url = kite_provider.generate_login_url()
        return {"login_url": url}
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/kite/auth")
def kite_auth() -> RedirectResponse:
    """Redirect the browser to the Kite Connect login URL."""
    try:
        url = kite_provider.generate_login_url()
        return RedirectResponse(url)
    except Exception as exc:
        return RedirectResponse(f"/kite/health?error={exc}")


@app.get("/kite/health")
def kite_health() -> dict[str, object]:
    try:
        profile = kite_provider.profile()
        return profile
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/margins")
def kite_margins() -> dict[str, object]:
    try:
        return kite_provider.margins()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/positions")
def kite_positions() -> dict[str, object]:
    try:
        return kite_provider.positions()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/callback", response_class=HTMLResponse)
def kite_callback(request_token: str | None = None, error: str | None = None):
    """Handle Kite redirect: exchange request_token for access token and show a simple page."""
    if error:
        return HTMLResponse(f"<h3>Kite login error: {error}</h3>")
    if not request_token:
        return HTMLResponse("<h3>No request_token provided in callback.</h3>")

    try:
        data = kite_provider.generate_session(request_token)
        access_token = data.get("access_token")
        if access_token:
            save_access_token(access_token)
            html = f"<h3>Login successful</h3><p>Access token saved.</p><p><a href=\"/\">Back to app</a></p>"
        else:
            html = f"<h3>Login completed but no access token returned.</h3><pre>{data}</pre>"
        return HTMLResponse(html)
    except Exception as exc:
        return HTMLResponse(f"<h3>Error exchanging token: {exc}</h3>")


@app.post("/kite/session")
def kite_session(payload: dict[str, str]) -> dict[str, object]:
    """Exchange a Kite request_token for an access token and persist it locally."""
    request_token = payload.get("request_token")
    if not request_token:
        raise HTTPException(status_code=400, detail="request_token is required")

    try:
        data = kite_provider.generate_session(request_token)
        access_token = data.get("access_token")
        if access_token:
            save_access_token(access_token)
        profile = kite_provider.profile()
        return {
            "status": "ok" if access_token else "missing-access-token",
            "saved": bool(access_token),
            "profile": profile,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
