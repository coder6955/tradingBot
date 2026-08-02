from __future__ import annotations

from datetime import datetime, timedelta
from threading import Event, RLock
from typing import Any, Callable, Sequence

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.time_utils import ist_now_naive


KiteProviderFactory = Callable[[], KiteProvider]


class MarketDataCoordinator:
    """Shared short-TTL market-data cache for non-order-placement quote reads."""

    def __init__(
        self,
        kite_provider_factory: KiteProviderFactory,
        *,
        quote_ttl_seconds: int | None = None,
    ) -> None:
        self.kite_provider_factory = kite_provider_factory
        self.quote_ttl_seconds = (
            quote_ttl_seconds
            if quote_ttl_seconds is not None
            else settings.kite_quote_cache_ttl_seconds
        )
        self._quote_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._instrument_cache: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
        self._quote_inflight: dict[str, Event] = {}
        self._instrument_inflight: dict[str, Event] = {}
        self._lock = RLock()
        self.quote_call_count = 0
        self.quote_cache_hits = 0
        self.quote_cache_misses = 0
        self.quote_inflight_reused = 0
        self.instrument_call_count = 0
        self.instrument_cache_hits = 0
        self.instrument_cache_misses = 0
        self.instrument_inflight_reused = 0
        self.last_quote_error: str | None = None
        self.last_instrument_error: str | None = None

    def quote(
        self,
        instruments: Sequence[str],
        *,
        provider: KiteProvider | None = None,
        bypass_cache: bool = False,
    ) -> dict[str, Any]:
        keys = list(dict.fromkeys(str(item) for item in instruments if item))
        if not keys:
            return {}
        if bypass_cache:
            return self._fetch_quotes(keys, provider=provider)

        ttl = timedelta(seconds=max(0, int(self.quote_ttl_seconds)))
        result: dict[str, Any] = {}
        pending = list(keys)
        wait_timeout = max(1.0, float(settings.kite_api_timeout_seconds) + 2.0)

        while pending:
            now = ist_now_naive()
            to_fetch: list[str] = []
            wait_events: list[Event] = []
            still_pending: list[str] = []
            with self._lock:
                for key in pending:
                    cached = self._quote_cache.get(key)
                    if cached is not None and now - cached[0] <= ttl:
                        self.quote_cache_hits += 1
                        result[key] = dict(cached[1])
                        continue
                    self.quote_cache_misses += 1
                    inflight = self._quote_inflight.get(key)
                    if inflight is not None:
                        self.quote_inflight_reused += 1
                        wait_events.append(inflight)
                        still_pending.append(key)
                        continue
                    inflight = Event()
                    self._quote_inflight[key] = inflight
                    to_fetch.append(key)

            if to_fetch:
                try:
                    result.update(self._fetch_quotes(to_fetch, provider=provider))
                    self.last_quote_error = None
                except Exception as exc:
                    self.last_quote_error = str(exc)
                    raise
                finally:
                    with self._lock:
                        for key in to_fetch:
                            inflight = self._quote_inflight.pop(key, None)
                            if inflight is not None:
                                inflight.set()

            if wait_events:
                for event in wait_events:
                    event.wait(timeout=wait_timeout)
                pending = [key for key in still_pending if key not in result]
            else:
                pending = []
        return result

    def instruments(
        self,
        exchange: str | None = None,
        *,
        provider: KiteProvider | None = None,
        bypass_cache: bool = False,
    ) -> list[dict[str, Any]]:
        key = str(exchange or "ALL").upper()
        if bypass_cache:
            return self._fetch_instruments(exchange, provider=provider)

        ttl = timedelta(seconds=max(1, int(settings.kite_instrument_cache_ttl_seconds)))
        wait_timeout = max(1.0, float(settings.kite_api_timeout_seconds) + 2.0)
        while True:
            now = ist_now_naive()
            with self._lock:
                cached = self._instrument_cache.get(key)
                if cached is not None and now - cached[0] <= ttl:
                    self.instrument_cache_hits += 1
                    return [dict(item) for item in cached[1]]
                self.instrument_cache_misses += 1
                inflight = self._instrument_inflight.get(key)
                if inflight is not None:
                    self.instrument_inflight_reused += 1
                    should_fetch = False
                else:
                    inflight = Event()
                    self._instrument_inflight[key] = inflight
                    should_fetch = True

            if should_fetch:
                try:
                    rows = self._fetch_instruments(exchange, provider=provider)
                    self.last_instrument_error = None
                    return rows
                except Exception as exc:
                    self.last_instrument_error = str(exc)
                    raise
                finally:
                    with self._lock:
                        event = self._instrument_inflight.pop(key, None)
                        if event is not None:
                            event.set()

            inflight.wait(timeout=wait_timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "quote_ttl_seconds": self.quote_ttl_seconds,
                "cached_quote_count": len(self._quote_cache),
                "quote_call_count": self.quote_call_count,
                "quote_cache_hits": self.quote_cache_hits,
                "quote_cache_misses": self.quote_cache_misses,
                "quote_inflight_reused": self.quote_inflight_reused,
                "quote_inflight_count": len(self._quote_inflight),
                "last_quote_error": self.last_quote_error,
                "instrument_call_count": self.instrument_call_count,
                "instrument_cache_hits": self.instrument_cache_hits,
                "instrument_cache_misses": self.instrument_cache_misses,
                "instrument_inflight_reused": self.instrument_inflight_reused,
                "instrument_inflight_count": len(self._instrument_inflight),
                "last_instrument_error": self.last_instrument_error,
                "cached_instrument_exchanges": sorted(self._instrument_cache.keys()),
                "cached_instruments": sorted(self._quote_cache.keys())[:200],
            }

    def clear(self) -> None:
        with self._lock:
            self._quote_cache.clear()
            self._instrument_cache.clear()

    def _fetch_quotes(
        self, keys: list[str], *, provider: KiteProvider | None = None
    ) -> dict[str, Any]:
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

    def _fetch_instruments(
        self, exchange: str | None, *, provider: KiteProvider | None = None
    ) -> list[dict[str, Any]]:
        client = provider or self.kite_provider_factory()
        rows = client.instruments(exchange)
        now = ist_now_naive()
        key = str(exchange or "ALL").upper()
        clean = [dict(item) for item in rows or [] if isinstance(item, dict)]
        with self._lock:
            self.instrument_call_count += 1
            self._instrument_cache[key] = (now, clean)
        return [dict(item) for item in clean]
