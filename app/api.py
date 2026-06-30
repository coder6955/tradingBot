from dataclasses import asdict

from fastapi import FastAPI, HTTPException

from app.config import settings
from app.services.market_data_service import MarketDataService
from app.services.scanner_service import ScannerService
from app.services.signal_repository import SignalRepository
from app.services.order_service import OrderService
from app.services.paper_trading_service import PaperTradingService
from app.services.auto_trader_service import AutoTraderService
from app.services.opportunity_repository import OpportunityRepository
from app.providers.kite_provider import KiteProvider
from app.providers.token_store import save_access_token, load_access_token
from fastapi.responses import RedirectResponse, HTMLResponse

app = FastAPI(title=settings.app_name, version="0.3.0")
market_data_service = MarketDataService()
signal_repository = SignalRepository()
paper_trading_service = PaperTradingService()
opportunity_repository = OpportunityRepository()


def get_kite_provider() -> KiteProvider:
    provider = KiteProvider()
    existing_token = load_access_token()
    if existing_token:
        provider.set_access_token(existing_token)
    return provider


def get_scanner_service() -> ScannerService:
    return ScannerService()


def get_order_service() -> OrderService:
    return OrderService(kite_provider=get_kite_provider(), paper_trading_service=paper_trading_service)


auto_trader_service = AutoTraderService(
    scanner_factory=get_scanner_service,
    order_service_factory=get_order_service,
    opportunity_repository=opportunity_repository,
)


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


def opportunity_record_to_dict(record) -> dict[str, object]:
    return {
        "id": record.id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "symbol": record.symbol,
        "action": record.action,
        "side": record.side,
        "tradingsymbol": record.tradingsymbol,
        "exchange": record.exchange,
        "expiry": record.expiry,
        "strike": record.strike,
        "entry_price": record.entry_price,
        "stop_loss": record.stop_loss,
        "target_1": record.target_1,
        "target_2": record.target_2,
        "target_3": record.target_3,
        "quantity": record.quantity,
        "lot_size": record.lot_size,
        "score": record.score,
        "probability": record.probability,
        "risk_reward": record.risk_reward,
        "status": record.status,
        "outcome": record.outcome,
        "exit_price": record.exit_price,
        "closed_at": record.closed_at.isoformat() if record.closed_at else None,
        "pnl": record.pnl,
        "review_notes": record.review_notes,
    }


@app.get("/market/{symbol}")
def get_market_summary(symbol: str) -> dict[str, object]:
    return market_data_service.get_market_summary(symbol)


@app.get("/signals")
def get_signals(side: str = "BUY", limit: int = 10) -> list[dict[str, object]]:
    scanner_service = get_scanner_service()
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
    scanner_service = get_scanner_service()
    symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
    recommendations = scanner_service.scan_symbols(symbols=symbol_list, side=side.upper())
    saved_ids = [opportunity_repository.save_opportunity(signal).id for signal in recommendations[:limit]]
    return {
        "mode": "live" if settings.live_trading_mode else "paper",
        "market_data": type(scanner_service.feed).__name__,
        "kite_access_token": bool(load_access_token()),
        "side": side.upper(),
        "count": len(recommendations),
        "saved_ids": saved_ids,
        "opportunities": [asdict(signal) for signal in recommendations[:limit]],
        "disclaimer": "Signals are probability-ranked trade setups with risk checks, not guaranteed profits.",
    }


@app.get("/scanner/diagnostics")
def get_scanner_diagnostics(side: str = "BUY", symbols: str | None = None, limit: int = 25) -> dict[str, object]:
    scanner_service = get_scanner_service()
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
        "market_data": type(scanner_service.feed).__name__,
        "kite_access_token": bool(load_access_token()),
        "use_kite_market_data": settings.use_kite_market_data,
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
        return get_order_service().place_signal_order(signal, confirm_live=confirm_live)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/auto-trader/start")
async def start_auto_trader(payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = payload or {}
    symbols_value = payload.get("symbols")
    symbols = None
    if isinstance(symbols_value, str):
        symbols = [item.strip().upper() for item in symbols_value.split(",") if item.strip()]
    elif isinstance(symbols_value, list):
        symbols = [str(item).strip().upper() for item in symbols_value if str(item).strip()]

    return auto_trader_service.start(
        side=str(payload.get("side") or "BUY"),
        symbols=symbols,
        interval_seconds=int(payload.get("interval_seconds") or settings.scanner_interval_seconds),
        limit=int(payload.get("limit") or 5),
        place_orders=bool(payload.get("place_orders", False)),
        confirm_live=bool(payload.get("confirm_live", False)),
    )


@app.post("/auto-trader/stop")
async def stop_auto_trader() -> dict[str, object]:
    return await auto_trader_service.stop()


@app.get("/auto-trader/status")
def get_auto_trader_status() -> dict[str, object]:
    return auto_trader_service.status()


@app.get("/auto-trader/latest")
def get_auto_trader_latest() -> dict[str, object]:
    return {
        "status": auto_trader_service.status(),
        "opportunities": auto_trader_service.latest_opportunities,
    }


@app.get("/auto-trader/executions")
def get_auto_trader_executions() -> dict[str, object]:
    return {
        "count": len(auto_trader_service.executions),
        "executions": auto_trader_service.executions,
        "errors": auto_trader_service.errors,
    }


@app.get("/opportunities")
def list_saved_opportunities(status: str | None = None, limit: int = 50) -> dict[str, object]:
    records = opportunity_repository.list_opportunities(status=status, limit=limit)
    return {
        "count": len(records),
        "opportunities": [opportunity_record_to_dict(record) for record in records],
    }


@app.post("/opportunities/{opportunity_id}/outcome")
def update_opportunity_outcome(opportunity_id: int, payload: dict[str, object]) -> dict[str, object]:
    outcome = str(payload.get("outcome") or "")
    if not outcome:
        raise HTTPException(status_code=400, detail="outcome is required")
    exit_price_value = payload.get("exit_price")
    exit_price = float(exit_price_value) if exit_price_value is not None else None
    review_notes = payload.get("review_notes")
    try:
        record = opportunity_repository.update_outcome(
            opportunity_id,
            outcome=outcome,
            exit_price=exit_price,
            review_notes=str(review_notes) if review_notes is not None else None,
        )
        return {"opportunity": opportunity_record_to_dict(record)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/opportunities/performance")
def get_opportunity_performance() -> dict[str, object]:
    return opportunity_repository.summarize_performance()


@app.post("/auto-trader/scan-once")
def scan_once_auto_trader(payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = payload or {}
    if not auto_trader_service.config:
        symbols_value = payload.get("symbols")
        symbols = None
        if isinstance(symbols_value, str):
            symbols = [item.strip().upper() for item in symbols_value.split(",") if item.strip()]
        elif isinstance(symbols_value, list):
            symbols = [str(item).strip().upper() for item in symbols_value if str(item).strip()]
        auto_trader_service.config = {
            "side": str(payload.get("side") or "BUY").upper(),
            "symbols": symbols,
            "interval_seconds": int(payload.get("interval_seconds") or settings.scanner_interval_seconds),
            "limit": int(payload.get("limit") or 5),
            "place_orders": bool(payload.get("place_orders", False)),
            "confirm_live": bool(payload.get("confirm_live", False)),
        }
    return auto_trader_service.scan_once()


@app.get("/paper/summary")
def get_paper_summary() -> dict[str, object]:
    return paper_trading_service.get_summary()


@app.get("/paper/positions")
def get_paper_positions() -> dict[str, object]:
    return {
        "count": len(paper_trading_service.positions),
        "positions": paper_trading_service.positions,
    }


@app.get("/paper/trades")
def get_paper_trades() -> dict[str, object]:
    return {
        "open_positions": paper_trading_service.positions,
        "closed_trades": paper_trading_service.closed_trades,
        "summary": paper_trading_service.get_summary(),
    }


@app.post("/paper/close")
def close_paper_position(payload: dict[str, object]) -> dict[str, object]:
    symbol = str(payload.get("symbol") or "")
    exit_price = payload.get("exit_price")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if exit_price is None:
        raise HTTPException(status_code=400, detail="exit_price is required")
    try:
        trade = paper_trading_service.close_trade(symbol=symbol, exit_price=float(exit_price))
        return {"status": "closed", "trade": trade, "summary": paper_trading_service.get_summary()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc




@app.get("/kite/login")
def kite_login() -> dict[str, str]:
    try:
        kite_provider = get_kite_provider()
        url = kite_provider.generate_login_url()
        return {"login_url": url}
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/kite/auth")
def kite_auth() -> RedirectResponse:
    """Redirect the browser to the Kite Connect login URL."""
    try:
        kite_provider = get_kite_provider()
        url = kite_provider.generate_login_url()
        return RedirectResponse(url)
    except Exception as exc:
        return RedirectResponse(f"/kite/health?error={exc}")


@app.get("/kite/health")
def kite_health() -> dict[str, object]:
    try:
        kite_provider = get_kite_provider()
        profile = kite_provider.profile()
        return profile
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/margins")
def kite_margins() -> dict[str, object]:
    try:
        kite_provider = get_kite_provider()
        return kite_provider.margins()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/positions")
def kite_positions() -> dict[str, object]:
    try:
        kite_provider = get_kite_provider()
        return kite_provider.positions()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/kite/callback", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
def kite_callback(request_token: str | None = None, error: str | None = None):
    """Handle Kite redirect: exchange request_token for access token and show a simple page."""
    if error:
        return HTMLResponse(f"<h3>Kite login error: {error}</h3>")
    if not request_token:
        return HTMLResponse("<h3>No request_token provided in callback.</h3>")

    try:
        kite_provider = get_kite_provider()
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
        kite_provider = get_kite_provider()
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
