from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from app.config import settings
from app.providers.token_store import load_access_token
from app.services.database import Candle, get_session
from app.services.completed_structure_service import classify_completed_structure
from app.services.indicator_service import compute_ema, compute_macd, compute_rsi
from app.services.time_utils import ist_now_naive, to_ist_naive
from app.services.io_call_metrics_service import io_call_metrics

try:
    from kiteconnect import KiteConnect
except Exception:  # KiteConnect may not be installed in test env
    KiteConnect = None  # type: ignore


class KiteFeed:
    """A thin feed adapter that provides a scanner-friendly snapshot using Kite Connect.

    This adapter is intentionally conservative: it attempts to fetch LTP and
    historical candles when possible and falls back to safe defaults so the
    application remains functional in development environments.
    """

    SYMBOL_ALIASES = {
        "NIFTY": "NIFTY 50",
        "BANKNIFTY": "NIFTY BANK",
        "FINNIFTY": "NIFTY FIN SERVICE",
        "INDIAVIX": "INDIA VIX",
        "VIX": "INDIA VIX",
    }

    def __init__(self) -> None:
        self.market_data_coordinator: Any | None = None
        self._instrument_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._instrument_cache_at: Dict[str, datetime] = {}
        # Analysis snapshots are candle-derived. Broad context quotes must never
        # populate this cache or they can masquerade as calculated indicators.
        self._snapshot_cache: Dict[str, tuple[datetime, Dict[str, Any]]] = {}
        self._context_snapshot_cache: Dict[str, tuple[datetime, Dict[str, Any]]] = {}
        self._quote_cache: Dict[str, tuple[datetime, Dict[str, Any]]] = {}
        self._call_counts: Dict[str, int] = {"quote": 0, "historical_data": 0, "instruments": 0, "snapshot_cache_hits": 0, "quote_cache_hits": 0}
        if KiteConnect is None:
            self.client = None
        else:
            self.client = self._kite_client()
            access_token = load_access_token() or settings.kite_access_token
            if access_token:
                try:
                    # Some kite client versions provide set_access_token, others expect direct header.
                    self.client.set_access_token(access_token)  # type: ignore
                except Exception:
                    # ignore if unavailable
                    pass

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _kite_client(self) -> Any:
        try:
            return KiteConnect(api_key=settings.kite_api_key, timeout=max(1, int(settings.kite_api_timeout_seconds)))
        except TypeError:
            return KiteConnect(api_key=settings.kite_api_key)

    def get_snapshots(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Return lightweight quote-only snapshots for broad market context."""
        if self.client is None or not symbols:
            return {symbol.upper(): self._stored_snapshot(symbol, self._default_snapshot(symbol)) for symbol in symbols}

        now = ist_now_naive()
        unique_symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols if symbol))
        instruments_by_symbol = {
            symbol: f"NSE:{self._kite_symbol(symbol)}" if ":" not in self._kite_symbol(symbol) else self._kite_symbol(symbol)
            for symbol in unique_symbols
        }
        snapshots = {symbol: self._default_snapshot(symbol) for symbol in unique_symbols}
        try:
            if self.market_data_coordinator is not None:
                self._call_counts["quote"] += 1
                quotes = self.market_data_coordinator.quote(list(instruments_by_symbol.values()))
            else:
                self._record_broker_call("quote")
                quotes = self.client.quote(list(instruments_by_symbol.values()))  # type: ignore
        except Exception:
            quotes = {}

        for symbol, instrument in instruments_by_symbol.items():
            payload = quotes.get(instrument) if isinstance(quotes, dict) else {}
            payload = payload if isinstance(payload, dict) else {}
            price = self._float(payload.get("last_price"))
            ohlc = payload.get("ohlc") if isinstance(payload.get("ohlc"), dict) else {}
            previous_close = self._float(ohlc.get("close"))
            day_open = self._float(ohlc.get("open"))
            day_high = self._float(ohlc.get("high"))
            day_low = self._float(ohlc.get("low"))
            volume = self._float(payload.get("volume"))
            quote_timestamp, timestamp_source = self._exchange_quote_timestamp(payload)
            if price is not None and price > 0:
                snapshots[symbol].update(
                    {
                        "is_real_data": True,
                        "price": price,
                        "instrument_token": self._safe_int(payload.get("instrument_token"), None),  # type: ignore
                        "quote_timestamp": quote_timestamp.isoformat(sep=" ") if quote_timestamp else None,
                        "receive_timestamp": now.isoformat(sep=" "),
                        "quote_timestamp_source": timestamp_source,
                        "context_quote_only": True,
                        "previous_day_close": previous_close or 0.0,
                        "day_open": day_open or 0.0,
                        "day_high": day_high or price,
                        "day_low": day_low or price,
                        "trend_bullish": price >= (previous_close or price),
                        "volume_confirmed": bool(volume and volume > 0),
                    }
                )
                if symbol == "BANKNIFTY":
                    snapshots[symbol] = self._stored_snapshot(symbol, snapshots[symbol])
                self._context_snapshot_cache[symbol] = (now, snapshots[symbol])
            else:
                snapshots[symbol] = self._stored_snapshot(symbol, snapshots[symbol])
        return snapshots

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        cached = self._cached_snapshot(symbol)
        if cached is not None:
            return cached

        started_at = ist_now_naive()
        snapshot = self._default_snapshot(symbol)

        if self.client is None:
            return self._stored_snapshot(symbol, snapshot)

        # Attempt to fetch LTP via quote() — this expects instrument token or "exchange:tradingsymbol"
        try:
            # try common NSE format
            kite_symbol = self._kite_symbol(symbol)
            instrument = f"NSE:{kite_symbol}" if ":" not in kite_symbol else kite_symbol
            if self.market_data_coordinator is not None:
                self._call_counts["quote"] += 1
                q = self.market_data_coordinator.quote([instrument])
            else:
                self._record_broker_call("quote")
                q = self.client.quote([instrument])  # type: ignore
            # q structure may vary; try to extract last_price
            last_price = None
            quote_payload: dict[str, Any] = {}
            for key in (instrument, "last_price"):
                if isinstance(q, dict) and key in q and isinstance(q[key], dict) and "last_price" in q[key]:
                    last_price = q[key]["last_price"]
                    quote_payload = dict(q[key])
                    break
            if last_price is None and isinstance(q, dict) and "last_price" in q:
                last_price = q["last_price"]
                quote_payload = dict(q)
            if last_price is not None:
                snapshot["price"] = float(last_price)
                snapshot["is_real_data"] = snapshot["price"] > 0
                exchange_timestamp, timestamp_source = self._exchange_quote_timestamp(quote_payload)
                snapshot["quote_timestamp"] = exchange_timestamp.isoformat(sep=" ") if exchange_timestamp else None
                snapshot["receive_timestamp"] = started_at.isoformat(sep=" ")
                snapshot["quote_timestamp_source"] = timestamp_source
                snapshot["instrument_token"] = self._safe_int(quote_payload.get("instrument_token"), None)  # type: ignore
        except Exception:
            # ignore network / key errors
            pass

        stored_snapshot = self._stored_snapshot(symbol, snapshot)
        if stored_snapshot.get("analysis_ready"):
            self._snapshot_cache[symbol.upper()] = (ist_now_naive(), stored_snapshot)
            return stored_snapshot
        if symbol.upper() == "BANKNIFTY":
            self._snapshot_cache[symbol.upper()] = (ist_now_naive(), stored_snapshot)
            return stored_snapshot
        if not settings.kite_snapshot_historical_fallback_enabled:
            self._snapshot_cache[symbol.upper()] = (ist_now_naive(), snapshot)
            return snapshot

        # Try to get a few historical candles to derive simple indicators
        try:
            token = self._find_instrument_token(symbol)
            if token is None:
                return stored_snapshot
            snapshot["instrument_token"] = token
            now = ist_now_naive()
            to_dt = now
            from_dt = now - timedelta(days=14)
            self._record_broker_call("historical_data")
            hist = self.client.historical_data(token, from_dt, to_dt, "5minute")  # type: ignore
            if hist and isinstance(hist, list):
                closes = [float(c["close"]) for c in hist if "close" in c]
                highs = [float(c["high"]) for c in hist if "high" in c]
                lows = [float(c["low"]) for c in hist if "low" in c]
                volumes = [float(c.get("volume", 0.0)) for c in hist]
                snapshot["candles"] = [
                    {
                        "date": str(c.get("date", "")),
                        "open": float(c.get("open", 0.0)),
                        "high": float(c.get("high", 0.0)),
                        "low": float(c.get("low", 0.0)),
                        "close": float(c.get("close", 0.0)),
                        "volume": float(c.get("volume", 0.0)),
                    }
                    for c in hist[-160:]
                ]
                if snapshot["candles"]:
                    snapshot["candle_timestamp"] = snapshot["candles"][-1]["date"]
                if len(closes) >= 26:
                    snapshot["price"] = closes[-1]
                    snapshot["is_real_data"] = closes[-1] > 0
                    rsi = compute_rsi(closes)[-1]
                    ema_9 = compute_ema(closes, 9)[-1]
                    ema_21 = compute_ema(closes, 21)[-1]
                    macd, signal = compute_macd(closes)
                    typical_price_volume = [
                        ((highs[idx] + lows[idx] + closes[idx]) / 3) * volumes[idx]
                        for idx in range(min(len(highs), len(lows), len(closes), len(volumes)))
                    ]
                    volume_sum = sum(volumes[-20:])
                    if volume_sum > 0:
                        vwap = sum(typical_price_volume[-20:]) / volume_sum
                    else:
                        typical_prices = [(highs[idx] + lows[idx] + closes[idx]) / 3 for idx in range(min(len(highs), len(lows), len(closes)))]
                        vwap = sum(typical_prices[-20:]) / max(len(typical_prices[-20:]), 1)
                    avg_volume = sum(volumes[-21:-1]) / max(len(volumes[-21:-1]), 1)
                    snapshot["rsi"] = int(rsi)
                    snapshot["macd"] = round(macd[-1], 4) if macd else 0.0
                    snapshot["macd_signal"] = round(signal[-1], 4) if signal else 0.0
                    snapshot["macd_positive"] = bool(macd and signal and macd[-1] > signal[-1])
                    snapshot["ema_9"] = round(ema_9, 2)
                    snapshot["ema_21"] = round(ema_21, 2)
                    snapshot["ema_alignment"] = ema_9 > ema_21
                    snapshot["vwap"] = round(vwap, 2)
                    snapshot["vwap_above_price"] = closes[-1] > vwap
                    snapshot["volume_confirmed"] = volumes[-1] > avg_volume * 1.15 if avg_volume else False
                    snapshot["adx"] = self._simple_trend_strength(closes)
                    snapshot["trend_bullish"] = closes[-1] >= ema_21 and ema_9 >= ema_21
                    snapshot["market_context"] = "strong" if snapshot["adx"] >= 20 and snapshot["volume_confirmed"] else "neutral"
                    snapshot["indicators_available"] = True
                    snapshot["analysis_ready"] = bool(snapshot.get("is_real_data"))
                    snapshot["data_quality_reasons"] = [] if snapshot["analysis_ready"] else ["fresh_ltp_unavailable"]
                    levels = self._daily_levels(snapshot["candles"])
                    snapshot.update(levels)
        except Exception:
            pass

        if not snapshot.get("is_real_data"):
            snapshot = self._stored_snapshot(symbol, snapshot)
        self._snapshot_cache[symbol.upper()] = (ist_now_naive(), snapshot)
        return snapshot

    def get_instruments(self, exchange: str) -> List[Dict[str, Any]]:
        if self.client is None:
            return []
        cached_at = self._instrument_cache_at.get(exchange)
        ttl = timedelta(seconds=max(1, settings.kite_instrument_cache_ttl_seconds))
        if exchange not in self._instrument_cache or cached_at is None or ist_now_naive() - cached_at > ttl:
            if self.market_data_coordinator is not None:
                self._call_counts["instruments"] += 1
                self._instrument_cache[exchange] = self.market_data_coordinator.instruments(exchange)
                self._instrument_cache_at[exchange] = ist_now_naive()
                return self._instrument_cache[exchange]
            persisted = self._load_persisted_instruments(exchange, ttl)
            if persisted is not None:
                self._instrument_cache[exchange] = persisted
                self._instrument_cache_at[exchange] = ist_now_naive()
                return self._instrument_cache[exchange]
            try:
                self._record_broker_call("instruments")
                self._instrument_cache[exchange] = self.client.instruments(exchange)  # type: ignore
                self._instrument_cache_at[exchange] = ist_now_naive()
                self._save_persisted_instruments(exchange, self._instrument_cache[exchange])
            except Exception:
                self._instrument_cache[exchange] = []
        return self._instrument_cache[exchange]

    def get_quotes(self, instruments: List[str]) -> Dict[str, Any]:
        if self.client is None or not instruments:
            return {}
        now = ist_now_naive()
        ttl = timedelta(seconds=max(1, settings.kite_quote_cache_ttl_seconds))
        result: Dict[str, Any] = {}
        missing: List[str] = []
        for instrument in instruments:
            cached = self._quote_cache.get(instrument)
            if cached is not None and now - cached[0] <= ttl:
                self._call_counts["quote_cache_hits"] += 1
                result[instrument] = cached[1]
            else:
                missing.append(instrument)
        if not missing:
            return result
        try:
            if self.market_data_coordinator is not None:
                self._call_counts["quote"] += 1
                fetched = self.market_data_coordinator.quote(missing)
            else:
                self._record_broker_call("quote")
                fetched = self.client.quote(missing)  # type: ignore
            stamp = ist_now_naive()
            for key, value in fetched.items():
                payload = dict(value) if isinstance(value, dict) else value
                if isinstance(payload, dict):
                    exchange_timestamp, timestamp_source = self._exchange_quote_timestamp(payload)
                    payload["quote_timestamp"] = exchange_timestamp.isoformat(sep=" ") if exchange_timestamp else None
                    payload["receive_timestamp"] = stamp.isoformat(sep=" ")
                    payload["quote_timestamp_source"] = timestamp_source
                self._quote_cache[key] = (stamp, payload)
                result[key] = payload
            return result
        except Exception:
            return result

    def call_counts(self) -> Dict[str, int]:
        return dict(self._call_counts)

    def reset_call_counts(self) -> None:
        for key in self._call_counts:
            self._call_counts[key] = 0

    def _record_broker_call(self, operation: str) -> None:
        self._call_counts[operation] = self._call_counts.get(operation, 0) + 1
        io_call_metrics.record_rest(operation)

    def _cached_snapshot(self, symbol: str) -> Dict[str, Any] | None:
        cached = self._snapshot_cache.get(symbol.upper())
        if cached is None:
            return None
        age = ist_now_naive() - cached[0]
        if age.total_seconds() <= max(1, settings.kite_snapshot_cache_ttl_seconds):
            self._call_counts["snapshot_cache_hits"] += 1
            return cached[1]
        return None

    def _default_snapshot(self, symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "source": "kite",
            "is_real_data": False,
            "price": 0.0,
            "rsi": None,
            "adx": None,
            "macd_positive": None,
            "ema_alignment": None,
            "vwap_above_price": None,
            "volume_confirmed": None,
            "trend_bullish": None,
            "market_context": "unavailable",
            "instrument_token": None,
            "candles": [],
            "vwap": None,
            "ema_9": None,
            "ema_21": None,
            "macd": None,
            "macd_signal": None,
            "previous_day_high": 0.0,
            "previous_day_low": 0.0,
            "previous_day_close": 0.0,
            "day_open": 0.0,
            "day_high": 0.0,
            "day_low": 0.0,
            "quote_timestamp": None,
            "receive_timestamp": None,
            "quote_timestamp_source": "unavailable",
            "candle_timestamp": None,
            "candle_confirmation_close": None,
            "indicators_available": False,
            "analysis_ready": False,
            "data_quality_reasons": ["canonical_current_session_candles_unavailable", "fresh_ltp_unavailable"],
        }

    def _float(self, value: Any) -> float | None:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            return None
        return None

    def _find_instrument_token(self, symbol: str) -> int | None:
        exchange_symbol = self._kite_symbol(symbol).split(":", 1)[-1].upper()
        for item in self.get_instruments(settings.default_exchange):
            if str(item.get("tradingsymbol", "")).upper() == exchange_symbol:
                return self._safe_int(item.get("instrument_token"), None)  # type: ignore
        return None

    def _load_persisted_instruments(self, exchange: str, ttl: timedelta) -> List[Dict[str, Any]] | None:
        path = Path(settings.kite_instrument_cache_file)
        try:
            if not path.exists():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            exchange_payload = payload.get(exchange)
            if not isinstance(exchange_payload, dict):
                return None
            cached_at = datetime.fromisoformat(str(exchange_payload.get("cached_at")))
            if ist_now_naive() - cached_at > ttl:
                return None
            rows = exchange_payload.get("instruments")
            if not isinstance(rows, list):
                return None
            return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception:
            return None

    def _save_persisted_instruments(self, exchange: str, instruments: List[Dict[str, Any]]) -> None:
        path = Path(settings.kite_instrument_cache_file)
        try:
            payload: dict[str, Any] = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(existing, dict):
                        payload = existing
                except Exception:
                    payload = {}
            payload[exchange] = {
                "cached_at": ist_now_naive().isoformat(),
                "source": "kite_instruments",
                "instruments": instruments,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, default=str), encoding="utf-8")
        except Exception:
            pass

    def _stored_snapshot(self, symbol: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        candles = self._recent_stored_candles(symbol)
        now = ist_now_naive()
        try:
            opening_hour, opening_minute = (int(part) for part in settings.opening_structure_end_time.split(":", 1))
        except (TypeError, ValueError):
            opening_hour, opening_minute = 9, 45
        latest_candle_date = None
        if candles:
            latest_timestamp = candles[-1].get("date")
            latest_candle_date = latest_timestamp.date() if isinstance(latest_timestamp, datetime) else None
        opening_session = (
            latest_candle_date == now.date()
            and now.replace(hour=9, minute=15, second=0, microsecond=0).time()
            <= now.time()
            < now.replace(hour=opening_hour, minute=opening_minute, second=0, microsecond=0).time()
        )
        required_candles = (
            max(3, settings.opening_structure_min_5m_candles)
            if opening_session
            else max(6, settings.structure_min_completed_candles)
        )
        if len(candles) < required_candles:
            reasons = ["insufficient_completed_5minute_structure_candles"]
            if not snapshot.get("is_real_data") or float(snapshot.get("price") or 0.0) <= 0:
                reasons.append("fresh_ltp_unavailable")
            return {
                **snapshot,
                "analysis_ready": False,
                "indicators_available": False,
                "data_quality_reasons": reasons,
                "canonical_candle_count": len(candles),
                "required_structure_candle_count": required_candles,
                "structure_direction": "neutral",
            }

        opens = [float(c["open"]) for c in candles]
        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        volumes = [float(c.get("volume", 0.0)) for c in candles]
        structure = classify_completed_structure(
            closes,
            opens=opens,
            highs=highs,
            lows=lows,
            volumes=volumes,
            min_candles=required_candles,
            opening_session=opening_session,
        )
        indicators_available = len(candles) >= 26
        rsi = compute_rsi(closes)[-1] if indicators_available else None
        ema_9 = compute_ema(closes, 9)[-1] if indicators_available else None
        ema_21 = compute_ema(closes, 21)[-1] if indicators_available else None
        macd, signal = compute_macd(closes) if indicators_available else ([], [])
        typical_price_volume = [
            ((highs[idx] + lows[idx] + closes[idx]) / 3) * volumes[idx]
            for idx in range(min(len(highs), len(lows), len(closes), len(volumes)))
        ]
        volume_sum = sum(volumes[-20:])
        if volume_sum > 0:
            vwap = sum(typical_price_volume[-20:]) / volume_sum
        else:
            typical_prices = [(highs[idx] + lows[idx] + closes[idx]) / 3 for idx in range(min(len(highs), len(lows), len(closes)))]
            vwap = sum(typical_prices[-20:]) / max(len(typical_prices[-20:]), 1)
        avg_volume = sum(volumes[-21:-1]) / max(len(volumes[-21:-1]), 1)
        stored = {
            **snapshot,
            "source": "stored_candles",
            "is_real_data": bool(snapshot.get("is_real_data")),
            "price": float(snapshot.get("price") or 0.0),
            "candles": candles,
            "quote_timestamp": snapshot.get("quote_timestamp"),
            "candle_timestamp": candles[-1]["date"] if candles else None,
            "candle_confirmation_close": closes[-1],
            "rsi": int(rsi) if rsi is not None else None,
            "macd": round(macd[-1], 4) if macd else None,
            "macd_signal": round(signal[-1], 4) if signal else None,
            "macd_positive": bool(macd and signal and macd[-1] > signal[-1]) if indicators_available else None,
            "ema_9": round(ema_9, 2) if ema_9 is not None else None,
            "ema_21": round(ema_21, 2) if ema_21 is not None else None,
            "ema_alignment": bool(ema_9 > ema_21) if ema_9 is not None and ema_21 is not None else None,
            "vwap": round(vwap, 2),
            "vwap_above_price": float(snapshot.get("price") or 0.0) > vwap,
            "volume_confirmed": volumes[-1] > avg_volume * 1.15 if avg_volume else False,
            "adx": self._simple_trend_strength(closes) if indicators_available else None,
            "trend_bullish": structure["direction"] == "bullish",
            "structure_direction": structure["direction"],
            "completed_structure": structure,
            "indicators_available": indicators_available,
            "analysis_ready": bool(snapshot.get("is_real_data")) and float(snapshot.get("price") or 0.0) > 0,
            "data_quality_reasons": [] if bool(snapshot.get("is_real_data")) and float(snapshot.get("price") or 0.0) > 0 else ["fresh_ltp_unavailable"],
            "canonical_candle_count": len(candles),
            "required_structure_candle_count": required_candles,
            "candle_source": "canonical_current_session_completed",
        }
        stored["legacy_indicator_shadow"] = {
            "available": indicators_available,
            "rsi": stored["rsi"],
            "ema_9": stored["ema_9"],
            "ema_21": stored["ema_21"],
            "macd": stored["macd"],
            "macd_signal": stored["macd_signal"],
            "adx": stored["adx"],
            "active_decision_role": "diagnostic_only",
        }
        stored["market_context"] = "strong" if float(structure.get("strength") or 0.0) >= 0.55 else "neutral"
        stored.update(self._daily_levels(candles))
        return stored

    def _recent_stored_candles(self, symbol: str) -> List[Dict[str, Any]]:
        now = ist_now_naive()
        session = get_session()
        try:
            market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
            market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
            if now.weekday() < 5 and market_open <= now <= market_close:
                session_start = market_open
                current_bucket = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
            else:
                latest = (
                    session.query(Candle.timestamp)
                    .filter(Candle.symbol == symbol.upper(), Candle.timeframe == "5minute", Candle.timestamp <= now)
                    .order_by(Candle.timestamp.desc())
                    .first()
                )
                if latest is None or latest[0] is None:
                    return []
                latest_time = latest[0].replace(tzinfo=None)
                session_start = latest_time.replace(hour=9, minute=15, second=0, microsecond=0)
                current_bucket = latest_time.replace(hour=15, minute=31, second=0, microsecond=0)
            rows = (
                session.query(Candle)
                .filter(
                    Candle.symbol == symbol.upper(),
                    Candle.timeframe == "5minute",
                    Candle.is_generated == 0,
                    Candle.timestamp >= session_start,
                    Candle.timestamp < current_bucket,
                )
                .order_by(Candle.timestamp.desc())
                .limit(160)
                .all()
            )
            return [
                {
                    "date": row.timestamp.isoformat(sep=" ") if row.timestamp else "",
                    "open": float(row.open_price),
                    "high": float(row.high_price),
                    "low": float(row.low_price),
                    "close": float(row.close_price),
                    "volume": float(row.volume or 0.0),
                    "timestamp_source": getattr(row, "timestamp_source", None),
                    "is_generated": bool(getattr(row, "is_generated", 0)),
                    "data_quality": getattr(row, "data_quality", None),
                }
                for row in reversed(rows)
            ]
        finally:
            session.close()

    def _exchange_quote_timestamp(self, payload: Dict[str, Any]) -> tuple[datetime | None, str]:
        for key in ("exchange_timestamp", "last_trade_time", "timestamp"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                parsed = to_ist_naive(value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
                return parsed, key
            except (TypeError, ValueError):
                continue
        return None, "receive_only"

    def _kite_symbol(self, symbol: str) -> str:
        raw_symbol = symbol.split(":", 1)[-1].upper()
        return self.SYMBOL_ALIASES.get(raw_symbol, raw_symbol)

    def _daily_levels(self, candles: List[Dict[str, Any]]) -> Dict[str, float]:
        by_day: Dict[str, List[Dict[str, Any]]] = {}
        for candle in candles:
            day = str(candle.get("date", ""))[:10]
            if day:
                by_day.setdefault(day, []).append(candle)
        days = sorted(by_day)
        if not days:
            return {}
        current_day = days[-1]
        current = by_day[current_day]
        levels = {
            "day_open": float(current[0].get("open", 0.0)) if current else 0.0,
            "day_high": max(float(c.get("high", 0.0)) for c in current),
            "day_low": min(float(c.get("low", 0.0)) for c in current),
        }
        if len(days) >= 2:
            previous = by_day[days[-2]]
            levels.update(
                {
                    "previous_day_high": max(float(c.get("high", 0.0)) for c in previous),
                    "previous_day_low": min(float(c.get("low", 0.0)) for c in previous),
                    "previous_day_close": float(previous[-1].get("close", 0.0)),
                }
            )
        return levels

    def _simple_trend_strength(self, closes: List[float]) -> int:
        if len(closes) < 20:
            return 15
        net_move = abs(closes[-1] - closes[-20])
        average_close = sum(closes[-20:]) / 20
        strength = (net_move / average_close) * 1000 if average_close else 0
        return min(50, max(10, int(strength)))
