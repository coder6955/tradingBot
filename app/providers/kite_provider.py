from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

try:
    from kiteconnect import KiteConnect
except ModuleNotFoundError:
    KiteConnect = None  # type: ignore

from app.config import settings
from app.providers.token_store import load_access_token


class KiteProvider:
    """Thin wrapper around the Kite Connect client for data and order access."""

    def __init__(self) -> None:
        if KiteConnect is None:
            self.client = None
            self.access_token = load_access_token() or settings.kite_access_token
            return
        self.client = self._kite_client()
        self.access_token = load_access_token() or settings.kite_access_token
        if self.access_token:
            self.client.set_access_token(self.access_token)

    def _kite_client(self) -> Any:
        try:
            return KiteConnect(api_key=settings.kite_api_key, timeout=max(1, int(settings.kite_api_timeout_seconds)))
        except TypeError:
            return KiteConnect(api_key=settings.kite_api_key)

    def generate_login_url(self) -> str:
        if self.client is None:
            raise RuntimeError("kiteconnect is not installed")
        return self.client.login_url()

    def set_access_token(self, token: Optional[str]) -> None:
        self.access_token = token
        if token and self.client is not None:
            self.client.set_access_token(token)

    def get_profile(self) -> Dict[str, Any]:
        return {"status": "not-configured", "token": bool(self.access_token)}

    def is_ready(self) -> bool:
        return bool(settings.kite_api_key and (self.access_token or load_access_token()))

    def generate_session(self, request_token: str) -> Dict[str, Any]:
        """Exchange a request_token for an access token using the API secret.

        Returns the raw session payload from Kite Connect on success.
        """
        if not settings.kite_api_secret:
            raise RuntimeError("KITE_API_SECRET is not configured")
        if self.client is None:
            raise RuntimeError("kiteconnect is not installed")
        data = self.client.generate_session(request_token, api_secret=settings.kite_api_secret)
        access_token = data.get("access_token")
        if access_token:
            # persist in memory for this process
            self.set_access_token(access_token)
            try:
                # some clients expose set_access_token
                self.client.set_access_token(access_token)  # type: ignore
            except Exception:
                pass
        return data

    def profile(self) -> Dict[str, Any]:
        """Return kite profile if available, else a not-configured payload."""
        if not self.access_token:
            return {"status": "not-configured"}
        if self.client is None:
            return {"status": "error", "message": "kiteconnect is not installed"}
        try:
            p = self.client.profile()  # type: ignore
            return {"status": "ok", "profile": p}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def instruments(self, exchange: str | None = None) -> List[Dict[str, Any]]:
        self._ensure_ready()
        return self.client.instruments(exchange)  # type: ignore

    def quote(self, instruments: Sequence[str]) -> Dict[str, Any]:
        self._ensure_ready()
        if not instruments:
            return {}
        return self.client.quote(list(instruments))  # type: ignore

    def ltp(self, instruments: Sequence[str]) -> Dict[str, Any]:
        self._ensure_ready()
        if not instruments:
            return {}
        return self.client.ltp(list(instruments))  # type: ignore

    def historical_data(self, instrument_token: int, from_dt: datetime, to_dt: datetime, interval: str) -> List[Dict[str, Any]]:
        self._ensure_ready()
        return self.client.historical_data(instrument_token, from_dt, to_dt, interval)  # type: ignore

    def margins(self) -> Dict[str, Any]:
        self._ensure_ready()
        return self.client.margins()  # type: ignore

    def positions(self) -> Dict[str, Any]:
        self._ensure_ready()
        return self.client.positions()  # type: ignore

    def orders(self) -> List[Dict[str, Any]]:
        self._ensure_ready()
        return self.client.orders()  # type: ignore

    def order_history(self, order_id: str) -> List[Dict[str, Any]]:
        self._ensure_ready()
        return self.client.order_history(order_id)  # type: ignore

    def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        product: str | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        validity: str = "DAY",
        variety: str = "regular",
    ) -> Dict[str, Any]:
        self._ensure_ready()
        order_id = self.client.place_order(  # type: ignore
            variety=variety,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product or settings.default_product,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            validity=validity,
        )
        return {"status": "submitted", "order_id": order_id}

    def cancel_order(self, order_id: str, variety: str = "regular") -> Dict[str, Any]:
        self._ensure_ready()
        cancelled = self.client.cancel_order(variety=variety, order_id=order_id)  # type: ignore
        return {"status": "cancelled", "order_id": cancelled or order_id}

    def _ensure_ready(self) -> None:
        if not settings.kite_api_key:
            raise RuntimeError("KITE_API_KEY is not configured")
        if not self.access_token:
            raise RuntimeError("KITE_ACCESS_TOKEN is not configured; complete /kite/auth first")
        if self.client is None:
            raise RuntimeError("kiteconnect is not installed")
