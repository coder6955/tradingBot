from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.config import settings
from app.providers.token_store import load_access_token
from app.services.database import Candle, get_session
from app.services.indicator_service import compute_ema, compute_macd, compute_rsi

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
        self._instrument_cache: Dict[str, List[Dict[str, Any]]] = {}
        if KiteConnect is None:
            self.client = None
        else:
            self.client = KiteConnect(api_key=settings.kite_api_key)
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

    def get_snapshot(self, symbol: str) -> Dict[str, Any]:
        # Default fallback snapshot
        snapshot = {
            "symbol": symbol,
            "source": "kite",
            "is_real_data": False,
            "price": 0.0,
            "rsi": 50,
            "adx": 15,
            "macd_positive": False,
            "ema_alignment": False,
            "vwap_above_price": False,
            "volume_confirmed": False,
            "trend_bullish": False,
            "market_context": "neutral",
            "instrument_token": None,
            "candles": [],
            "vwap": 0.0,
            "ema_9": 0.0,
            "ema_21": 0.0,
            "macd": 0.0,
            "macd_signal": 0.0,
            "previous_day_high": 0.0,
            "previous_day_low": 0.0,
            "previous_day_close": 0.0,
            "day_open": 0.0,
            "day_high": 0.0,
            "day_low": 0.0,
        }

        if self.client is None:
            return self._stored_snapshot(symbol, snapshot)

        # Attempt to fetch LTP via quote() — this expects instrument token or "exchange:tradingsymbol"
        try:
            # try common NSE format
            kite_symbol = self._kite_symbol(symbol)
            instrument = f"NSE:{kite_symbol}" if ":" not in kite_symbol else kite_symbol
            q = self.client.quote([instrument])  # type: ignore
            # q structure may vary; try to extract last_price
            last_price = None
            for key in (instrument, "last_price"):
                if isinstance(q, dict) and key in q and isinstance(q[key], dict) and "last_price" in q[key]:
                    last_price = q[key]["last_price"]
                    break
            if last_price is None and isinstance(q, dict) and "last_price" in q:
                last_price = q["last_price"]
            if last_price is not None:
                snapshot["price"] = float(last_price)
                snapshot["is_real_data"] = snapshot["price"] > 0
        except Exception:
            # ignore network / key errors
            pass

        # Try to get a few historical candles to derive simple indicators
        try:
            token = self._find_instrument_token(symbol)
            if token is None:
                return self._stored_snapshot(symbol, snapshot)
            snapshot["instrument_token"] = token
            now = datetime.utcnow()
            to_dt = now
            from_dt = now - timedelta(days=14)
            hist = self.client.historical_data(token, from_dt, to_dt, "15minute")  # type: ignore
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
                    levels = self._daily_levels(snapshot["candles"])
                    snapshot.update(levels)
        except Exception:
            pass

        if not snapshot.get("is_real_data"):
            return self._stored_snapshot(symbol, snapshot)
        return snapshot

    def get_instruments(self, exchange: str) -> List[Dict[str, Any]]:
        if self.client is None:
            return []
        if exchange not in self._instrument_cache:
            try:
                self._instrument_cache[exchange] = self.client.instruments(exchange)  # type: ignore
            except Exception:
                self._instrument_cache[exchange] = []
        return self._instrument_cache[exchange]

    def get_quotes(self, instruments: List[str]) -> Dict[str, Any]:
        if self.client is None or not instruments:
            return {}
        try:
            return self.client.quote(instruments)  # type: ignore
        except Exception:
            return {}

    def _find_instrument_token(self, symbol: str) -> int | None:
        exchange_symbol = self._kite_symbol(symbol).split(":", 1)[-1].upper()
        for item in self.get_instruments(settings.default_exchange):
            if str(item.get("tradingsymbol", "")).upper() == exchange_symbol:
                return self._safe_int(item.get("instrument_token"), None)  # type: ignore
        return None

    def _stored_snapshot(self, symbol: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        candles = self._recent_stored_candles(symbol)
        if len(candles) < 26:
            return snapshot

        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        volumes = [float(c.get("volume", 0.0)) for c in candles]
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
        stored = {
            **snapshot,
            "source": "stored_candles",
            "is_real_data": True,
            "price": closes[-1],
            "candles": candles,
            "rsi": int(rsi),
            "macd": round(macd[-1], 4) if macd else 0.0,
            "macd_signal": round(signal[-1], 4) if signal else 0.0,
            "macd_positive": bool(macd and signal and macd[-1] > signal[-1]),
            "ema_9": round(ema_9, 2),
            "ema_21": round(ema_21, 2),
            "ema_alignment": ema_9 > ema_21,
            "vwap": round(vwap, 2),
            "vwap_above_price": closes[-1] > vwap,
            "volume_confirmed": volumes[-1] > avg_volume * 1.15 if avg_volume else False,
            "adx": self._simple_trend_strength(closes),
            "trend_bullish": closes[-1] >= ema_21 and ema_9 >= ema_21,
        }
        stored["market_context"] = "strong" if stored["adx"] >= 20 and stored["volume_confirmed"] else "neutral"
        stored.update(self._daily_levels(candles))
        return stored

    def _recent_stored_candles(self, symbol: str) -> List[Dict[str, Any]]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol.upper(), Candle.timeframe == "5minute")
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
                }
                for row in reversed(rows)
            ]
        finally:
            session.close()

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
