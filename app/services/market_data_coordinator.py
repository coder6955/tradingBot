from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Callable, Sequence

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.time_utils import ist_now_naive


KiteProviderFactory = Callable[[], KiteProvider]


class MarketDataCoordinator:
    """Shared short-TTL market-data cache for non-order-placement quote reads."""

    def __init__(self, kite_provider_factory: KiteProviderFactory, *, quote_ttl_seconds: int | None = None) -> None:
        self.kite_provider_factory = kite_provider_factory
        self.quote_ttl_seconds = quote_ttl_seconds if quote_ttl_seconds is not None else settings.kite_quote_cache_ttl_seconds
        self._quote_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._lock = RLock()
        self.quote_call_count = 0
        self.quote_cache_hits = 0
        self.quote_cache_misses = 0

    def quote(self, instruments: Sequence[str], *, provider: KiteProvider | None = None, bypass_cache: bool = False) -> dict[str, Any]:
        keys = list(dict.fromkeys(str(item) for item in instruments if item))
        if not keys:
            return {}
        if bypass_cache:
            return self._fetch_quotes(keys, provider=provider)

        now = ist_now_naive()
        ttl = timedelta(seconds=max(0, int(self.quote_ttl_seconds)))
        result: dict[str, Any] = {}
        missing: list[str] = []
        with self._lock:
            for key in keys:
                cached = self._quote_cache.get(key)
                if cached is not None and now - cached[0] <= ttl:
                    self.quote_cache_hits += 1
                    result[key] = dict(cached[1])
                else:
                    self.quote_cache_misses += 1
                    missing.append(key)

        if missing:
            fetched = self._fetch_quotes(missing, provider=provider)
            result.update(fetched)
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "quote_ttl_seconds": self.quote_ttl_seconds,
                "cached_quote_count": len(self._quote_cache),
                "quote_call_count": self.quote_call_count,
                "quote_cache_hits": self.quote_cache_hits,
                "quote_cache_misses": self.quote_cache_misses,
                "cached_instruments": sorted(self._quote_cache.keys())[:200],
            }

    def clear(self) -> None:
        with self._lock:
            self._quote_cache.clear()

    def _fetch_quotes(self, keys: list[str], *, provider: KiteProvider | None = None) -> dict[str, Any]:
        client = provider or self.kite_provider_factory()
        fetched = client.quote(keys)
        now = ist_now_naive()
        clean: dict[str, Any] = {}
        with self._lock:
            self.quote_call_count += 1
            for key, value in fetched.items():
                payload = dict(value) if isinstance(value, dict) else {"value": value}
                payload.setdefault("quote_timestamp", now.isoformat(sep=" "))
                payload.setdefault("quote_source", "market_data_coordinator")
                self._quote_cache[str(key)] = (now, payload)
                clean[str(key)] = dict(payload)
        return clean
