from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Any

from app.models import Signal
from app.config import settings
from app.services.banknifty_intelligence_service import BankNiftyIntelligenceService
from app.services.database import Candle, OptionQuoteSnapshot, get_session
from app.services.day_type_service import DayTypeService
from app.services.greeks_service import GreeksService
from app.services.indicator_service import compute_ema, compute_macd, compute_rsi
from app.services.market_regime_service import MarketRegimeService
from app.services.option_premium_confirmation_service import OptionPremiumConfirmationService
from app.services.option_quality_service import OptionQualityService
from app.services.trade_setup_service import OptionContract, TradeSetupService


@dataclass(frozen=True)
class BacktestTrade:
    timestamp: str
    direction: str
    entry_price: float
    exit_price: float
    outcome: str
    move_pct: float
    bars_held: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptionBacktestTrade:
    timestamp: str
    direction: str
    underlying_entry: float
    tradingsymbol: str
    option_entry: float
    option_exit: float
    outcome: str
    pnl_pct: float
    gross_pnl_pct: float
    charges_pct: float
    slippage_pct: float
    spread_impact_pct: float
    bars_held: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HistoricalScannerReplayFeed:
    """Expose stored historical candles through the scanner feed interface."""

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        underlying: list[Candle],
        option_candles: dict[str, list[Candle]],
        option_snapshots: dict[str, list[OptionQuoteSnapshot]] | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.underlying = list(underlying)
        self.option_candles = option_candles
        self.option_snapshots = option_snapshots or {}
        self.current_index = 0
        self._call_counts = {"historical_snapshot": 0, "historical_quotes": 0, "historical_instruments": 0}
        self._token_by_symbol = {name: 900000 + idx for idx, name in enumerate(sorted(option_candles), start=1)}
        self._cached_symbol_candles: dict[str, list[Candle]] = {self.symbol: self.underlying}

    @property
    def current_timestamp(self) -> datetime:
        if not self.underlying:
            return datetime.now()
        index = min(max(self.current_index, 0), len(self.underlying) - 1)
        return self._as_datetime(self.underlying[index].timestamp)

    def set_index(self, index: int) -> None:
        self.current_index = min(max(int(index), 0), max(len(self.underlying) - 1, 0))

    def get_snapshot(self, symbol: str) -> dict[str, Any]:
        self._call_counts["historical_snapshot"] += 1
        clean_symbol = symbol.upper()
        candles = self._candles_for_symbol(clean_symbol)
        if not candles:
            return {
                "symbol": clean_symbol,
                "source": "historical_scanner_replay_unavailable",
                "is_real_data": False,
                "price": 0.0,
                "trend_bullish": False,
                "market_context": "unavailable",
            }
        rows = [row for row in candles if self._as_datetime(row.timestamp) <= self.current_timestamp]
        if not rows:
            return {
                "symbol": clean_symbol,
                "source": "historical_scanner_replay_unavailable",
                "is_real_data": False,
                "price": 0.0,
                "trend_bullish": False,
                "market_context": "unavailable",
            }
        return self._snapshot_from_rows(clean_symbol, rows)

    def get_instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        self._call_counts["historical_instruments"] += 1
        if exchange and exchange != settings.option_exchange:
            return []
        instruments: list[dict[str, Any]] = []
        for tradingsymbol in sorted(self.option_candles):
            parsed = self._parse_option_symbol(tradingsymbol)
            if parsed is None:
                continue
            instruments.append(
                {
                    "tradingsymbol": tradingsymbol,
                    "exchange": settings.option_exchange,
                    "instrument_token": self._token_by_symbol.get(tradingsymbol),
                    "name": self.symbol,
                    "expiry": parsed["expiry"].isoformat(),
                    "strike": parsed["strike"],
                    "instrument_type": parsed["option_type"],
                    "lot_size": 15,
                }
            )
        return instruments

    def get_quotes(self, instruments: list[str]) -> dict[str, Any]:
        self._call_counts["historical_quotes"] += 1
        result: dict[str, Any] = {}
        for instrument in instruments:
            tradingsymbol = str(instrument).split(":", 1)[-1]
            quote = self._quote_for_symbol(tradingsymbol)
            if quote:
                result[str(instrument)] = quote
        return result

    def call_counts(self) -> dict[str, Any]:
        return dict(self._call_counts)

    def candles_for(self, symbol: str, *, current_session_only: bool = False, limit: int | None = None) -> list[Candle]:
        rows = [row for row in self._candles_for_symbol(symbol.upper()) if self._as_datetime(row.timestamp) <= self.current_timestamp]
        if current_session_only:
            session_date = self.current_timestamp.date()
            rows = [row for row in rows if self._as_datetime(row.timestamp).date() == session_date]
        return rows[-limit:] if limit else rows

    def option_candles_for(self, tradingsymbol: str, *, current_session_only: bool = False, limit: int | None = None) -> list[Candle]:
        rows = [row for row in self.option_candles.get(tradingsymbol, []) if self._as_datetime(row.timestamp) <= self.current_timestamp]
        if current_session_only:
            session_date = self.current_timestamp.date()
            rows = [row for row in rows if self._as_datetime(row.timestamp).date() == session_date]
        return rows[-limit:] if limit else rows

    def _snapshot_from_rows(self, symbol: str, rows: list[Candle]) -> dict[str, Any]:
        closes = [float(row.close_price) for row in rows]
        highs = [float(row.high_price) for row in rows]
        lows = [float(row.low_price) for row in rows]
        volumes = [float(row.volume) for row in rows]
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)
        rsi = compute_rsi(closes, 14)
        macd, macd_signal = compute_macd(closes)
        last = rows[-1]
        last_time = self._as_datetime(last.timestamp)
        current_date = last_time.date()
        today_rows = [row for row in rows if self._as_datetime(row.timestamp).date() == current_date]
        previous_rows = [row for row in rows if self._as_datetime(row.timestamp).date() < current_date]
        price = closes[-1]
        day_high = max(float(row.high_price) for row in today_rows) if today_rows else float(last.high_price)
        day_low = min(float(row.low_price) for row in today_rows) if today_rows else float(last.low_price)
        day_open = float(today_rows[0].open_price) if today_rows else float(last.open_price)
        previous_day_high = max(float(row.high_price) for row in previous_rows) if previous_rows else max(highs[-20:])
        previous_day_low = min(float(row.low_price) for row in previous_rows) if previous_rows else min(lows[-20:])
        previous_day_close = float(previous_rows[-1].close_price) if previous_rows else closes[max(0, len(closes) - 2)]
        avg_volume = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
        volume_confirmed = volumes[-1] >= avg_volume * 1.10 if avg_volume > 0 else False
        vwap = self._vwap(today_rows or rows[-20:])
        trend_bullish = price >= ema21[-1]
        adx = self._approx_adx(rows)
        return {
            "symbol": symbol,
            "source": "historical_scanner_replay",
            "is_real_data": True,
            "price": price,
            "rsi": rsi[-1],
            "adx": adx,
            "macd_positive": macd[-1] >= macd_signal[-1],
            "ema_alignment": ema9[-1] >= ema21[-1] >= ema50[-1],
            "vwap_above_price": price >= vwap,
            "volume_confirmed": volume_confirmed,
            "trend_bullish": trend_bullish,
            "market_context": "strong" if adx >= 20 else "neutral",
            "previous_day_high": previous_day_high,
            "previous_day_low": previous_day_low,
            "previous_day_close": previous_day_close,
            "day_open": day_open,
            "day_high": day_high,
            "day_low": day_low,
            "vwap": vwap,
            "ema_9": ema9[-1],
            "ema_21": ema21[-1],
            "last_candle_close": price,
            "candle_close": price,
            "latest_candle_at": last_time.isoformat(sep=" "),
            "quote_timestamp": last_time.isoformat(sep=" "),
            "timestamp": last_time.isoformat(sep=" "),
            "candles": [
                {
                    "date": self._as_datetime(row.timestamp).isoformat(sep=" "),
                    "open": float(row.open_price),
                    "high": float(row.high_price),
                    "low": float(row.low_price),
                    "close": float(row.close_price),
                    "volume": float(row.volume),
                }
                for row in rows[-20:]
            ],
        }

    def _quote_for_symbol(self, tradingsymbol: str) -> dict[str, Any] | None:
        snapshot = self._latest_option_snapshot(tradingsymbol)
        if snapshot is not None:
            return {
                "instrument_token": self._token_by_symbol.get(tradingsymbol),
                "last_price": float(snapshot.last_price or 0.0),
                "volume": float(snapshot.volume or 0.0),
                "oi": float(snapshot.open_interest or 0.0),
                "quote_timestamp": self._as_datetime(snapshot.timestamp).isoformat(sep=" "),
                "source": "historical_option_quote_snapshot",
                "depth": {
                    "buy": [{"price": float(snapshot.bid or 0.0)}],
                    "sell": [{"price": float(snapshot.ask or 0.0)}],
                },
            }
        candle = self._latest_option_candle(tradingsymbol)
        if candle is None:
            return None
        close = float(candle.close_price)
        return {
            "instrument_token": self._token_by_symbol.get(tradingsymbol),
            "last_price": close,
            "volume": float(candle.volume or 0.0),
            "oi": 0.0,
            "quote_timestamp": self._as_datetime(candle.timestamp).isoformat(sep=" "),
            "source": "historical_option_candle_fallback_missing_oi",
            "depth": {
                "buy": [{"price": round(close * 0.9975, 2)}],
                "sell": [{"price": round(close * 1.0025, 2)}],
            },
        }

    def _latest_option_snapshot(self, tradingsymbol: str) -> OptionQuoteSnapshot | None:
        rows = [
            row
            for row in self.option_snapshots.get(tradingsymbol, [])
            if self._as_datetime(row.timestamp) <= self.current_timestamp
        ]
        return rows[-1] if rows else None

    def _latest_option_candle(self, tradingsymbol: str) -> Candle | None:
        rows = self.option_candles_for(tradingsymbol)
        return rows[-1] if rows else None

    def _candles_for_symbol(self, symbol: str) -> list[Candle]:
        if symbol in self._cached_symbol_candles:
            return self._cached_symbol_candles[symbol]
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.timeframe == self.timeframe)
                .order_by(Candle.timestamp.asc())
                .all()
            )
            self._cached_symbol_candles[symbol] = list(rows)
            return self._cached_symbol_candles[symbol]
        finally:
            session.close()

    def _parse_option_symbol(self, symbol: str) -> dict[str, Any] | None:
        match = re.search(r"(?P<day>\d{2})(?P<month>[A-Z]{3})(?P<strike>\d+(?:\.\d+)?)(?P<option_type>CE|PE)$", symbol.upper())
        if not match:
            fallback = re.search(r"(?P<strike>\d+(?:\.\d+)?)(?P<option_type>CE|PE)$", symbol.upper())
            if not fallback:
                return None
            return {"expiry": self.current_timestamp.date(), "strike": float(fallback.group("strike")), "option_type": fallback.group("option_type")}
        year = self.current_timestamp.year
        try:
            expiry = datetime.strptime(f"{year}-{match.group('month')}-{int(match.group('day'))}", "%Y-%b-%d").date()
        except ValueError:
            expiry = self.current_timestamp.date()
        return {"expiry": expiry, "strike": float(match.group("strike")), "option_type": match.group("option_type")}

    def _vwap(self, rows: list[Candle]) -> float:
        total_volume = sum(float(row.volume or 0.0) for row in rows)
        if total_volume <= 0:
            return sum(float(row.close_price) for row in rows) / max(len(rows), 1)
        return sum(((float(row.high_price) + float(row.low_price) + float(row.close_price)) / 3) * float(row.volume or 0.0) for row in rows) / total_volume

    def _approx_adx(self, rows: list[Candle]) -> float:
        recent = rows[-14:] if len(rows) >= 14 else rows
        if len(recent) < 2:
            return 15.0
        move = abs(float(recent[-1].close_price) - float(recent[0].close_price))
        ranges = [max(float(row.high_price) - float(row.low_price), 0.01) for row in recent]
        strength = move / max(sum(ranges), 0.01)
        return max(10.0, min(35.0, 12.0 + strength * 45.0))

    def _as_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)


class BacktestPremiumConfirmationService(OptionPremiumConfirmationService):
    def __init__(self, feed: HistoricalScannerReplayFeed) -> None:
        super().__init__(websocket_price_feed=None)
        self.feed = feed

    def _recent_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        return self.feed.option_candles_for(symbol, current_session_only=True, limit=limit)

    def _freshness_payload(self, *, source: str, first: datetime | None, last: datetime | None, symbol: str | None = None) -> dict[str, Any]:
        now = self.feed.current_timestamp
        session_date = now.date()
        age = max(0.0, (now - last).total_seconds()) if last else None
        candle_date = last.date().isoformat() if last else None
        max_age = self._max_age_seconds(source)
        passed = bool(last and last.date() == session_date and age is not None and age <= max_age)
        reason = None if passed else "premium_candles_stale_or_missing"
        payload = {
            "option_candle_ingestion_enabled": settings.automation_intraday_candle_sync,
            "premium_candle_source": source,
            "premium_first_timestamp": first.isoformat(sep=" ") if first else None,
            "premium_last_timestamp": last.isoformat(sep=" ") if last else None,
            "premium_candle_age_seconds": round(age, 3) if age is not None else None,
            "premium_candle_session_date": candle_date,
            "current_market_session_date": session_date.isoformat(),
            "premium_candle_freshness_passed": passed,
            "premium_candle_rejection_reason": reason,
            "premium_candle_max_age_seconds": max_age,
            "selected_option_candle_source": source,
            "selected_option_last_candle_age_seconds": round(age, 3) if age is not None else None,
        }
        payload.update(self._current_session_candle_status(symbol or ""))
        return payload

    def _missing_freshness(self, source: str, reason: str) -> dict[str, Any]:
        payload = super()._missing_freshness(source, reason)
        payload["current_market_session_date"] = self.feed.current_timestamp.date().isoformat()
        return payload

    def _current_session_candle_status(self, symbol: str) -> dict[str, Any]:
        rows = self.feed.option_candles_for(symbol, current_session_only=True) if symbol else []
        latest = rows[-1].timestamp if rows else None
        latest_dt = latest.replace(tzinfo=None) if isinstance(latest, datetime) else latest
        age = max(0.0, (self.feed.current_timestamp - latest_dt).total_seconds()) if isinstance(latest_dt, datetime) else None
        return {
            "latest_current_session_option_candle": latest_dt.isoformat(sep=" ") if isinstance(latest_dt, datetime) else None,
            "selected_option_candle_count_today": len(rows),
            "selected_option_last_candle_age_seconds": round(age, 3) if age is not None else None,
        }


class BacktestDayTypeService(DayTypeService):
    def __init__(self, feed: HistoricalScannerReplayFeed) -> None:
        self.feed = feed

    def _today_candles(self, *, symbol: str, timeframe: str) -> list[Candle]:
        return self.feed.candles_for(symbol, current_session_only=True)


class BacktestMarketRegimeService(MarketRegimeService):
    def __init__(self, feed: HistoricalScannerReplayFeed) -> None:
        self.feed = feed

    def calendar_check(self, symbol: str) -> dict[str, Any]:
        today = self.feed.current_timestamp.date().isoformat()
        reasons: list[str] = []
        blocked_dates = {item.strip() for item in settings.blocked_event_dates.split(",") if item.strip()}
        if today in blocked_dates:
            reasons.append(f"{today} is configured as a blocked event date")
        blocked_symbols = {item.strip().upper() for item in settings.blocked_symbols.split(",") if item.strip()}
        if symbol.upper() in blocked_symbols:
            reasons.append(f"{symbol.upper()} is configured as blocked")
        return {"passed": not reasons, "reasons": reasons, "timestamp": self.feed.current_timestamp.isoformat()}


class BacktestGreeksService(GreeksService):
    def __init__(self, feed: HistoricalScannerReplayFeed) -> None:
        self.feed = feed

    def _days_to_expiry(self, expiry: Any) -> int:
        parsed = self._parse_date(expiry)
        if parsed is None:
            return 7
        return max(0, (parsed - self.feed.current_timestamp.date()).days)


class BacktestTradeSetupService(TradeSetupService):
    def __init__(self, feed: HistoricalScannerReplayFeed) -> None:
        super().__init__()
        self.feed = feed

    def risk_checks(self, score: int, contract: OptionContract, entry_price: float, side: str, enforce_budget: bool = False) -> list[str]:
        failures: list[str] = []
        if self.liquidity_score(contract) < settings.min_option_liquidity_score:
            failures.append("option liquidity is below threshold")
        if side.upper() == "BUY":
            if entry_price < settings.min_option_buy_premium:
                failures.append("option premium is below minimum configured for buying")
            expiry = self._parse_expiry(contract.expiry)
            if settings.block_expiry_day_option_buying and expiry is not None and expiry <= self.feed.current_timestamp.date():
                failures.append("expiry-day option buying is blocked")
        else:
            if not settings.allow_option_selling:
                failures.append("option selling is disabled by configuration")
        if self._spread_pct(contract) > settings.max_bid_ask_spread_pct:
            failures.append("bid/ask spread is too wide")
        if contract.volume < settings.min_option_volume:
            failures.append("option volume is below threshold")
        if contract.open_interest < settings.min_option_oi:
            failures.append("option open interest is below threshold")
        return failures

    def _option_premium_structure(self, tradingsymbol: str, timeframe: str = "5minute", limit: int = 24) -> dict[str, float]:
        candles = self.feed.option_candles_for(tradingsymbol, limit=limit)
        if len(candles) < 4:
            return {"atr": 0.0, "swing_low": 0.0, "swing_high": 0.0}
        true_ranges: list[float] = []
        previous_close = float(candles[0].close_price)
        for candle in candles[1:]:
            high = float(candle.high_price)
            low = float(candle.low_price)
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
            previous_close = float(candle.close_price)
        recent = candles[-8:] if len(candles) >= 8 else candles
        return {
            "atr": sum(true_ranges[-14:]) / max(len(true_ranges[-14:]), 1),
            "swing_low": min(float(candle.low_price) for candle in recent),
            "swing_high": max(float(candle.high_price) for candle in recent),
        }


class BacktestBankNiftyIntelligenceService(BankNiftyIntelligenceService):
    def __init__(self, feed: HistoricalScannerReplayFeed) -> None:
        self.feed = feed

    def _today_candles(self, symbol: str, timeframe: str = "5minute") -> list[Candle]:
        return self.feed.candles_for(symbol, current_session_only=True)

    def _recent_candles(self, symbol: str, timeframe: str = "5minute", limit: int = 30) -> list[Candle]:
        return self.feed.candles_for(symbol, limit=limit)

    def _opening_range_status(self, bullish: bool, price: float) -> dict[str, Any]:
        candles = self._today_candles("BANKNIFTY")
        if not candles:
            return {"status": "unavailable", "passed": True, "reason": "opening range candles unavailable"}
        if self.feed.current_timestamp.time() < self._parse_time(settings.banknifty_first_trade_time):
            return {"status": "pre_opening_range_complete", "passed": False, "reason": "opening range is not complete"}
        opening = self._candles_between(candles, settings.banknifty_opening_range_start, settings.banknifty_opening_range_end)
        opening = opening or candles[:3]
        high = max(float(candle.high_price) for candle in opening)
        low = min(float(candle.low_price) for candle in opening)
        first = opening[0]
        body = abs(float(first.close_price) - float(first.open_price))
        wick = (float(first.high_price) - float(first.low_price)) - body
        last_close = price or float(candles[-1].close_price)
        previous = float(candles[-2].close_price) if len(candles) > 1 else last_close
        if low <= last_close <= high:
            status = "inside_opening_range"
            reason = "Bank Nifty is inside opening range"
        elif bullish and last_close > high:
            status = "breakout" if previous >= high or last_close > previous else "failed_breakout"
            reason = "opening range breakout confirmed" if status == "breakout" else "opening breakout is not holding"
        elif (not bullish) and last_close < low:
            status = "breakdown" if previous <= low or last_close < previous else "failed_breakdown"
            reason = "opening range breakdown confirmed" if status == "breakdown" else "opening breakdown is not holding"
        else:
            status = "opposite_side"
            reason = "Bank Nifty is on the wrong side of opening range"
        return {
            "status": status,
            "passed": status in {"breakout", "breakdown", "unavailable"},
            "openingRangeHigh": round(high, 2),
            "openingRangeLow": round(low, 2),
            "first15Body": round(body, 2),
            "first15Wick": round(max(wick, 0.0), 2),
            "largeWick": wick > body * 1.25 if body > 0 else True,
            "reason": reason,
        }

    def _dte_mode(self, expiry: str) -> dict[str, Any]:
        try:
            expiry_date = datetime.fromisoformat(str(expiry)).date()
            dte = max(0, (expiry_date - self.feed.current_timestamp.date()).days)
        except ValueError:
            dte = 999
        if dte <= 1:
            mode = "gamma_scalp"
            risk = "near_expiry"
        elif dte <= 5:
            mode = "momentum"
            risk = "normal"
        else:
            mode = "trend_continuation"
            risk = "far_expiry"
        return {"daysToExpiry": dte, "mode": mode, "risk": risk}

    def _event_day_mode(self) -> dict[str, Any]:
        today = self.feed.current_timestamp.date().isoformat()
        events = [item.strip() for item in settings.banknifty_event_dates.split(",") if item.strip()]
        is_event = today in events
        after = self.feed.current_timestamp.time() >= self._parse_time(settings.banknifty_event_preferred_after_time)
        return {
            "is_event_day": is_event,
            "mode": "event_day" if is_event else "normal",
            "post_event_confirmation_window": (not is_event) or after,
            "configured_dates": events,
        }


class BacktestNoopRejectedOpportunityRepository:
    def save_rejection(self, **_: Any) -> None:
        return None


class BacktestNoLookaheadGuard:
    def evaluate(self, **_: Any) -> dict[str, Any]:
        return {"enabled": False, "passed": True, "reasons": ["disabled in historical scanner parity to avoid lookahead bias"]}


class BacktestService:
    """Replay stored underlying candles to validate directional option-buying rules."""

    ABLATION_VARIANTS: dict[str, dict[str, Any]] = {
        "baseline": {},
        "without_rsi": {"disabled": {"rsi"}},
        "without_macd": {"disabled": {"macd"}},
        "without_adx": {"unsupported": "ADX is not stored in historical candle backtest inputs yet."},
        "without_pcr": {"unsupported": "PCR is option-chain context and not part of candle replay yet."},
        "without_max_pain": {"unsupported": "Max pain is option-chain context and not part of candle replay yet."},
        "without_nifty_context": {"unsupported": "Nifty context is live scanner context and not part of single-symbol replay yet."},
        "without_vix_context": {"unsupported": "VIX context is live scanner context and not part of single-symbol replay yet."},
        "without_premium_confirmation": {"unsupported": "Premium confirmation requires live/stored option premium snapshots, not underlying-only signals."},
        "without_candle_confirmation": {"disabled": {"breakout"}},
        "without_day_type_hard_gate": {"unsupported": "Day-type hard gate uses intraday context and is not represented in this option premium replay."},
        "only_vwap_premium_quality": {"unsupported": "VWAP/premium/quality ablation needs stored VWAP and option-quality snapshots."},
        "only_trend_quality_premium": {"unsupported": "Quality and premium confirmation are not fully represented in this candle signal replay."},
    }

    def run(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        side: str = "BUY",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        side = side.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        candles = self._load_candles(symbol=symbol.upper(), timeframe=timeframe, limit=limit)
        if len(candles) < 60:
            return {
                "status": "insufficient_data",
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "candles": len(candles),
                "minimum_required": 60,
                "message": "Store more historical candles before this backtest can produce useful evidence.",
            }

        closes = [float(item.close_price) for item in candles]
        highs = [float(item.high_price) for item in candles]
        lows = [float(item.low_price) for item in candles]
        volumes = [float(item.volume) for item in candles]
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)
        rsi = compute_rsi(closes, 14)
        macd, macd_signal = compute_macd(closes)

        trades: list[BacktestTrade] = []
        warmup = 55
        last_signal_index = -horizon
        for idx in range(warmup, len(candles) - horizon):
            if idx - last_signal_index < max(3, horizon // 3):
                continue
            signal_direction, reason = self._signal_direction(
                idx=idx,
                closes=closes,
                highs=highs,
                lows=lows,
                volumes=volumes,
                ema9=ema9,
                ema21=ema21,
                ema50=ema50,
                rsi=rsi,
                macd=macd,
                macd_signal=macd_signal,
            )
            if signal_direction is None:
                continue
            if direction in {"CALL", "CE", "BULLISH"} and signal_direction != "CALL":
                continue
            if direction in {"PUT", "PE", "BEARISH"} and signal_direction != "PUT":
                continue
            trade = self._simulate_trade(
                candles=candles,
                idx=idx,
                direction=signal_direction,
                horizon=horizon,
                reason=reason,
            )
            trades.append(trade)
            last_signal_index = idx

        summary = self._summarize(trades)
        return {
            "status": "ok",
            "mode": "underlying_rule_replay",
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "side": side,
            "direction": direction,
            "candles": len(candles),
            "horizon_candles": horizon,
            "summary": summary,
            "examples": [trade.to_dict() for trade in trades[-20:]],
            "limitations": [
                "This validates directional entry logic on underlying candles.",
                "Exact option P&L requires historical option-chain quotes, bid/ask, IV, and Greeks snapshots.",
                "Use this result as a filter-quality study, not as a guaranteed live fill simulation.",
            ],
        }

    def run_option_premium(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 2000,
        decision_mode: str = "scanner_parity",
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        underlying = self._load_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        if len(underlying) < 60:
            return {"status": "insufficient_data", "symbol": symbol, "candles": len(underlying), "minimum_required": 60}

        option_candles = self._load_option_candles(underlying=symbol, timeframe=timeframe)
        if not option_candles:
            return {
                "status": "insufficient_option_data",
                "symbol": symbol,
                "message": "No stored option premium candles found for this underlying. Run /data/ingest/option-candles first.",
            }

        if decision_mode != "legacy":
            return self._run_scanner_parity_option_premium(
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                horizon=horizon,
                underlying=underlying,
                option_candles=option_candles,
            )

        signals = self._signals_from_underlying(underlying, direction=direction, horizon=horizon)
        trades: list[OptionBacktestTrade] = []
        for idx, signal_direction, reason in signals:
            trade = self._simulate_option_trade(
                underlying=underlying,
                option_candles=option_candles,
                idx=idx,
                direction=signal_direction,
                horizon=horizon,
                reason=reason,
            )
            if trade is not None:
                trades.append(trade)

        summary = self._summarize_option_trades(trades, timeframe=timeframe)
        return {
            "status": "ok",
            "mode": "option_premium_replay",
            "decision_mode": "legacy",
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "underlying_candles": len(underlying),
            "option_contracts": len(option_candles),
            "horizon_candles": horizon,
            "stop_loss_pct": settings.backtest_option_stop_loss_pct,
            "target_pct": settings.backtest_option_target_pct,
            "slippage_pct": settings.backtest_slippage_pct,
            "charges_pct": settings.backtest_charges_pct,
            "summary": summary,
            "segments": self._segment_option_trades(trades, timeframe=timeframe),
            "examples": [trade.to_dict() for trade in trades[-20:]],
        }

    def run_walk_forward(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 3000,
        decision_mode: str = "scanner_parity",
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        candles = self._load_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        if len(candles) < 120:
            return {"status": "insufficient_data", "symbol": symbol, "candles": len(candles), "minimum_required": 120}

        split = int(len(candles) * (settings.backtest_walk_forward_train_pct / 100))
        split = min(max(split, 60), len(candles) - 60)
        train_result = self._run_option_premium_on_candles(symbol=symbol, timeframe=timeframe, direction=direction, horizon=horizon, underlying=candles[:split], decision_mode=decision_mode)
        test_result = self._run_option_premium_on_candles(symbol=symbol, timeframe=timeframe, direction=direction, horizon=horizon, underlying=candles[split:], decision_mode=decision_mode)
        passed, reasons = self._validation_passed(test_result.get("summary", {}))
        return {
            "status": "ok",
            "mode": "walk_forward_option",
            "decision_mode": decision_mode,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "train_candles": split,
            "test_candles": len(candles) - split,
            "train_summary": train_result.get("summary", {}),
            "test_summary": test_result.get("summary", {}),
            "summary": test_result.get("summary", {}),
            "passed": passed,
            "reasons": reasons,
            "thresholds": {
                "min_trades": settings.min_strategy_trades,
                "min_expectancy_pct": settings.min_strategy_expectancy_pct,
                "min_profit_factor": settings.min_strategy_profit_factor,
                "min_win_rate_pct": settings.min_strategy_win_rate_pct,
            },
        }

    def run_ablation(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        underlying = self._load_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        if len(underlying) < 60:
            return {"status": "insufficient_data", "symbol": symbol, "candles": len(underlying), "minimum_required": 60}
        option_candles = self._load_option_candles(underlying=symbol, timeframe=timeframe)
        if not option_candles:
            return {"status": "insufficient_option_data", "symbol": symbol, "message": "No stored option premium candles found for ablation replay."}

        variants: dict[str, Any] = {}
        baseline_summary: dict[str, Any] | None = None
        for name, config in self.ABLATION_VARIANTS.items():
            if config.get("unsupported"):
                variants[name] = {"status": "unsupported", "reason": config["unsupported"]}
                continue
            signals = self._signals_from_underlying(
                underlying,
                direction=direction,
                horizon=horizon,
                disabled_factors=set(config.get("disabled", set())),
            )
            trades = [
                trade
                for idx, signal_direction, reason in signals
                if (trade := self._simulate_option_trade(underlying=underlying, option_candles=option_candles, idx=idx, direction=signal_direction, horizon=horizon, reason=f"{name}: {reason}"))
                is not None
            ]
            variants[name] = {
                "status": "ok",
                "disabled_factors": sorted(config.get("disabled", [])),
                "summary": self._summarize_option_trades(trades, timeframe=timeframe),
                "segments": self._segment_option_trades(trades, timeframe=timeframe),
            }
            if name == "baseline":
                baseline_summary = variants[name]["summary"]
        if baseline_summary:
            for name, result in variants.items():
                if result.get("status") != "ok":
                    continue
                result["impact_vs_baseline"] = self._ablation_impact(baseline_summary, result.get("summary", {}))
        return {
            "status": "ok",
            "mode": "ablation_option_replay",
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "horizon_candles": horizon,
            "variants": variants,
            "note": "Unsupported variants need stored option-chain/VIX/VWAP/quality snapshots before they can affect replay.",
        }

    def _run_scanner_parity_option_premium(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: str,
        horizon: int,
        underlying: list[Candle],
        option_candles: dict[str, list[Candle]],
    ) -> dict[str, Any]:
        if len(underlying) < 60:
            return {"status": "insufficient_data", "symbol": symbol, "candles": len(underlying), "minimum_required": 60}
        feed = HistoricalScannerReplayFeed(
            symbol=symbol,
            timeframe=timeframe,
            underlying=underlying,
            option_candles=option_candles,
            option_snapshots=self._load_option_snapshots(option_candles),
        )
        scanner = self._historical_scanner(feed)
        trades: list[OptionBacktestTrade] = []
        decisions_scanned = 0
        accepted = 0
        rejected = 0
        rejection_reasons: dict[str, int] = {}
        decision_examples: list[dict[str, Any]] = []
        warmup = 55
        last_entry_index = -horizon
        for idx in range(warmup, len(underlying) - horizon):
            if idx - last_entry_index < max(3, horizon // 3):
                continue
            feed.set_index(idx)
            diagnostics = scanner.scan_with_diagnostics(
                symbols=[symbol],
                side="BUY",
                order_mode="paper",
                rejection_source="backtest_scanner_parity",
            )
            if not diagnostics:
                continue
            row = diagnostics[0]
            decisions_scanned += 1
            signal = row.get("signal")
            if signal is not None and not self._direction_allowed(signal, direction):
                rejected += 1
                rejection_reasons["direction_filtered"] = rejection_reasons.get("direction_filtered", 0) + 1
                continue
            if signal is None:
                rejected += 1
                for reason in row.get("reasons", []) or ["no_signal"]:
                    key = str(reason)
                    rejection_reasons[key] = rejection_reasons.get(key, 0) + 1
                if len(decision_examples) < 10:
                    decision_examples.append(self._decision_example(row))
                continue
            accepted += 1
            trade = self._simulate_option_trade_from_signal(
                option_candles=option_candles,
                signal=signal,
                idx_timestamp=feed.current_timestamp,
                horizon=horizon,
                reason=f"scanner_parity score={row.get('score')} state={row.get('entry_timing_state') or row.get('setup_state')}",
            )
            if trade is not None:
                trades.append(trade)
                last_entry_index = idx
                if len(decision_examples) < 10:
                    decision_examples.append(self._decision_example(row))

        summary = self._summarize_option_trades(trades, timeframe=timeframe)
        return {
            "status": "ok",
            "mode": "scanner_parity_option_premium_replay",
            "decision_mode": "scanner_parity",
            "decision_engine": "ScannerService",
            "shared_live_components": [
                "ScannerService",
                "DecisionEngineService.score_breakdown",
                "TradeSetupService.select_contract",
                "TradeSetupService.build_prices",
                "OptionPremiumConfirmationService-compatible historical adapter",
                "DayTypeService-compatible historical adapter",
                "BankNiftyIntelligenceService-compatible historical adapter",
            ],
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "underlying_candles": len(underlying),
            "option_contracts": len(option_candles),
            "horizon_candles": horizon,
            "decisions_scanned": decisions_scanned,
            "accepted_signals": accepted,
            "rejected_decisions": rejected,
            "rejection_reasons": dict(sorted(rejection_reasons.items(), key=lambda item: item[1], reverse=True)[:20]),
            "summary": summary,
            "segments": self._segment_option_trades(trades, timeframe=timeframe),
            "examples": [trade.to_dict() for trade in trades[-20:]],
            "decision_examples": decision_examples,
            "legacy_mode_available": 'Pass {"decision_mode": "legacy"} to compare with the older independent candle-rule replay.',
            "limitations": [
                "Backtest now uses the scanner decision path, selected contract, score, stop loss, and target where historical data is available.",
                "Live-only adaptive guards such as outcome learning are disabled in replay to avoid lookahead bias.",
                "Historical option-chain/VIX/top-bank accuracy depends on stored snapshots and candles; unavailable context is surfaced in rejection reasons.",
            ],
        }

    def _historical_scanner(self, feed: HistoricalScannerReplayFeed) -> Any:
        from app.services.scanner_service import ScannerService

        return ScannerService(
            feed=feed,
            trade_setup_service=BacktestTradeSetupService(feed),
            market_regime_service=BacktestMarketRegimeService(feed),
            day_type_service=BacktestDayTypeService(feed),
            option_premium_confirmation_service=BacktestPremiumConfirmationService(feed),
            option_quality_service=OptionQualityService(BacktestGreeksService(feed)),
            banknifty_intelligence_service=BacktestBankNiftyIntelligenceService(feed),
            rejected_opportunity_repository=BacktestNoopRejectedOpportunityRepository(),
            outcome_learning_service=BacktestNoLookaheadGuard(),
            time_bucket_edge_service=BacktestNoLookaheadGuard(),
            strategy_edge_service=BacktestNoLookaheadGuard(),
            banknifty_option_prewarm_service=None,
            armed_entry_tracker=None,
        )

    def _simulate_option_trade_from_signal(
        self,
        *,
        option_candles: dict[str, list[Candle]],
        signal: Signal,
        idx_timestamp: datetime,
        horizon: int,
        reason: str,
    ) -> OptionBacktestTrade | None:
        tradingsymbol = str(signal.tradingsymbol or "")
        series = option_candles.get(tradingsymbol)
        if not series:
            return None
        option_idx = self._find_candle_index_at_or_after(series, idx_timestamp)
        if option_idx is None or option_idx + 1 >= len(series):
            return None
        entry_raw = float(signal.entry_price or 0.0)
        stop = float(signal.stop_loss or 0.0)
        target = float(signal.target_1 or 0.0)
        if entry_raw <= 0 or stop <= 0 or target <= entry_raw:
            return None
        entry_friction_pct = settings.backtest_slippage_pct + settings.paper_spread_impact_pct_per_side
        exit_friction_pct = settings.backtest_slippage_pct + settings.paper_spread_impact_pct_per_side
        entry = entry_raw * (1 + entry_friction_pct / 100)
        exit_price = float(series[min(option_idx + horizon, len(series) - 1)].close_price) * (1 - exit_friction_pct / 100)
        outcome = "expired"
        bars_held = min(horizon, len(series) - option_idx - 1)
        for offset in range(1, min(horizon, len(series) - option_idx - 1) + 1):
            candle = series[option_idx + offset]
            low = float(candle.low_price)
            high = float(candle.high_price)
            if low <= stop:
                exit_price = stop * (1 - exit_friction_pct / 100)
                outcome = "stop_loss"
                bars_held = offset
                break
            if high >= target:
                exit_price = target * (1 - exit_friction_pct / 100)
                outcome = "target"
                bars_held = offset
                break
        gross_pnl_pct = ((exit_price - entry) / entry) * 100
        charges_pct = settings.backtest_charges_pct
        pnl_pct = gross_pnl_pct - charges_pct
        option_type = "CALL" if str(signal.action or "").upper().endswith("CE") else "PUT"
        return OptionBacktestTrade(
            timestamp=idx_timestamp.isoformat(sep=" "),
            direction=option_type,
            underlying_entry=0.0,
            tradingsymbol=tradingsymbol,
            option_entry=round(entry, 2),
            option_exit=round(exit_price, 2),
            outcome=outcome,
            pnl_pct=round(pnl_pct, 3),
            gross_pnl_pct=round(gross_pnl_pct, 3),
            charges_pct=round(charges_pct, 3),
            slippage_pct=round(settings.backtest_slippage_pct * 2, 3),
            spread_impact_pct=round(settings.paper_spread_impact_pct_per_side * 2, 3),
            bars_held=bars_held,
            reason=reason,
        )

    def _load_option_snapshots(self, option_candles: dict[str, list[Candle]]) -> dict[str, list[OptionQuoteSnapshot]]:
        if not option_candles:
            return {}
        symbols = list(option_candles.keys())
        session = get_session()
        try:
            rows = (
                session.query(OptionQuoteSnapshot)
                .filter(OptionQuoteSnapshot.tradingsymbol.in_(symbols))
                .order_by(OptionQuoteSnapshot.tradingsymbol.asc(), OptionQuoteSnapshot.timestamp.asc())
                .all()
            )
            grouped: dict[str, list[OptionQuoteSnapshot]] = {}
            for row in rows:
                grouped.setdefault(row.tradingsymbol, []).append(row)
            return grouped
        finally:
            session.close()

    def _direction_allowed(self, signal: Any, direction: str) -> bool:
        action = str(getattr(signal, "action", "") or "").upper()
        if direction in {"BOTH", "ALL"}:
            return True
        if direction in {"CALL", "CE", "BULLISH"}:
            return action.endswith("CE")
        if direction in {"PUT", "PE", "BEARISH"}:
            return action.endswith("PE")
        return True

    def _decision_example(self, row: dict[str, Any]) -> dict[str, Any]:
        signal = row.get("signal")
        return {
            "timestamp": row.get("factor_scores", {}).get("strategy_metadata", {}).get("generated_at") if isinstance(row.get("factor_scores"), dict) else None,
            "passed": bool(row.get("passed")),
            "score": row.get("score"),
            "trend": row.get("trend"),
            "reasons": row.get("reasons", []),
            "entry_timing_state": row.get("entry_timing_state"),
            "tradingsymbol": getattr(signal, "tradingsymbol", None) if signal else None,
            "entry_price": getattr(signal, "entry_price", None) if signal else None,
            "stop_loss": getattr(signal, "stop_loss", None) if signal else None,
            "target_1": getattr(signal, "target_1", None) if signal else None,
        }

    def _run_option_premium_on_candles(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: str,
        horizon: int,
        underlying: list[Candle],
        decision_mode: str = "scanner_parity",
    ) -> dict[str, Any]:
        option_candles = self._load_option_candles(underlying=symbol, timeframe=timeframe)
        if not option_candles:
            return {"summary": self._summarize_option_trades([], timeframe=timeframe), "examples": []}
        if decision_mode != "legacy":
            return self._run_scanner_parity_option_premium(
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                horizon=horizon,
                underlying=underlying,
                option_candles=option_candles,
            )
        signals = self._signals_from_underlying(underlying, direction=direction, horizon=horizon)
        trades = [
            trade
            for idx, signal_direction, reason in signals
            if (trade := self._simulate_option_trade(underlying=underlying, option_candles=option_candles, idx=idx, direction=signal_direction, horizon=horizon, reason=reason))
            is not None
        ]
        return {
            "summary": self._summarize_option_trades(trades, timeframe=timeframe),
            "segments": self._segment_option_trades(trades, timeframe=timeframe),
            "examples": [trade.to_dict() for trade in trades[-20:]],
        }

    def _load_candles(self, *, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()

    def _load_option_candles(self, *, underlying: str, timeframe: str) -> dict[str, list[Candle]]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol.like(f"{underlying.upper()}%"), Candle.timeframe == timeframe)
                .order_by(Candle.symbol.asc(), Candle.timestamp.asc())
                .all()
            )
            grouped: dict[str, list[Candle]] = {}
            for row in rows:
                if self._parse_option_symbol(row.symbol) is not None:
                    grouped.setdefault(row.symbol, []).append(row)
            return grouped
        finally:
            session.close()

    def _signals_from_underlying(self, candles: list[Candle], *, direction: str, horizon: int, disabled_factors: set[str] | None = None) -> list[tuple[int, str, str]]:
        closes = [float(item.close_price) for item in candles]
        highs = [float(item.high_price) for item in candles]
        lows = [float(item.low_price) for item in candles]
        volumes = [float(item.volume) for item in candles]
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)
        rsi = compute_rsi(closes, 14)
        macd, macd_signal = compute_macd(closes)
        signals: list[tuple[int, str, str]] = []
        warmup = 55
        last_signal_index = -horizon
        for idx in range(warmup, len(candles) - horizon):
            if idx - last_signal_index < max(3, horizon // 3):
                continue
            signal_direction, reason = self._signal_direction(
                idx=idx,
                closes=closes,
                highs=highs,
                lows=lows,
                volumes=volumes,
                ema9=ema9,
                ema21=ema21,
                ema50=ema50,
                rsi=rsi,
                macd=macd,
                macd_signal=macd_signal,
                disabled_factors=disabled_factors or set(),
            )
            if signal_direction is None:
                continue
            if direction in {"CALL", "CE", "BULLISH"} and signal_direction != "CALL":
                continue
            if direction in {"PUT", "PE", "BEARISH"} and signal_direction != "PUT":
                continue
            signals.append((idx, signal_direction, reason))
            last_signal_index = idx
        return signals

    def _signal_direction(
        self,
        *,
        idx: int,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        volumes: list[float],
        ema9: list[float],
        ema21: list[float],
        ema50: list[float],
        rsi: list[float],
        macd: list[float],
        macd_signal: list[float],
        disabled_factors: set[str] | None = None,
    ) -> tuple[str | None, str]:
        disabled_factors = disabled_factors or set()
        recent_high = max(highs[idx - 10 : idx])
        recent_low = min(lows[idx - 10 : idx])
        avg_volume = sum(volumes[idx - 20 : idx]) / 20
        volume_ok = volumes[idx] >= avg_volume * 1.15
        breakout_call = "breakout" in disabled_factors or closes[idx] > recent_high
        breakout_put = "breakout" in disabled_factors or closes[idx] < recent_low
        ema_call = "ema" in disabled_factors or ema9[idx] > ema21[idx] > ema50[idx]
        ema_put = "ema" in disabled_factors or ema9[idx] < ema21[idx] < ema50[idx]
        macd_call = "macd" in disabled_factors or macd[idx] > macd_signal[idx]
        macd_put = "macd" in disabled_factors or macd[idx] < macd_signal[idx]
        rsi_call = "rsi" in disabled_factors or 48 <= rsi[idx] <= 72
        rsi_put = "rsi" in disabled_factors or 28 <= rsi[idx] <= 52
        volume_call = "volume" in disabled_factors or volume_ok
        volume_put = "volume" in disabled_factors or volume_ok

        bullish = (
            breakout_call
            and ema_call
            and macd_call
            and rsi_call
            and volume_call
        )
        bearish = (
            breakout_put
            and ema_put
            and macd_put
            and rsi_put
            and volume_put
        )
        if bullish:
            return "CALL", "breakout with EMA alignment, MACD confirmation, RSI room, and volume expansion"
        if bearish:
            return "PUT", "breakdown with EMA alignment, MACD confirmation, RSI room, and volume expansion"
        return None, ""

    def _simulate_trade(self, *, candles: list[Candle], idx: int, direction: str, horizon: int, reason: str) -> BacktestTrade:
        entry = float(candles[idx].close_price)
        target_pct = 0.0065
        stop_pct = 0.0045
        if direction == "CALL":
            target = entry * (1 + target_pct)
            stop = entry * (1 - stop_pct)
        else:
            target = entry * (1 - target_pct)
            stop = entry * (1 + stop_pct)

        exit_price = float(candles[idx + horizon].close_price)
        outcome = "expired"
        bars_held = horizon
        for offset in range(1, horizon + 1):
            candle = candles[idx + offset]
            high = float(candle.high_price)
            low = float(candle.low_price)
            if direction == "CALL":
                if low <= stop:
                    exit_price = stop
                    outcome = "stop_loss"
                    bars_held = offset
                    break
                if high >= target:
                    exit_price = target
                    outcome = "target"
                    bars_held = offset
                    break
            else:
                if high >= stop:
                    exit_price = stop
                    outcome = "stop_loss"
                    bars_held = offset
                    break
                if low <= target:
                    exit_price = target
                    outcome = "target"
                    bars_held = offset
                    break

        move_pct = ((exit_price - entry) / entry) * 100
        if direction == "PUT":
            move_pct *= -1
        timestamp = candles[idx].timestamp
        if isinstance(timestamp, datetime):
            timestamp_text = timestamp.isoformat(sep=" ")
        else:
            timestamp_text = str(timestamp)
        return BacktestTrade(
            timestamp=timestamp_text,
            direction=direction,
            entry_price=round(entry, 2),
            exit_price=round(exit_price, 2),
            outcome=outcome,
            move_pct=round(move_pct, 3),
            bars_held=bars_held,
            reason=reason,
        )

    def _simulate_option_trade(
        self,
        *,
        underlying: list[Candle],
        option_candles: dict[str, list[Candle]],
        idx: int,
        direction: str,
        horizon: int,
        reason: str,
    ) -> OptionBacktestTrade | None:
        option_type = "CE" if direction == "CALL" else "PE"
        timestamp = underlying[idx].timestamp
        spot = float(underlying[idx].close_price)
        selected_symbol = self._select_option_symbol(option_candles, option_type=option_type, spot=spot, timestamp=timestamp)
        if selected_symbol is None:
            return None
        series = option_candles[selected_symbol]
        option_idx = self._find_candle_index_at_or_after(series, timestamp)
        if option_idx is None or option_idx + 1 >= len(series):
            return None

        entry_raw = float(series[option_idx].close_price)
        if entry_raw <= 0:
            return None
        entry_friction_pct = settings.backtest_slippage_pct + settings.paper_spread_impact_pct_per_side
        exit_friction_pct = settings.backtest_slippage_pct + settings.paper_spread_impact_pct_per_side
        entry = entry_raw * (1 + entry_friction_pct / 100)
        stop = entry * (1 - settings.backtest_option_stop_loss_pct / 100)
        target = entry * (1 + settings.backtest_option_target_pct / 100)
        exit_price = float(series[min(option_idx + horizon, len(series) - 1)].close_price) * (1 - exit_friction_pct / 100)
        outcome = "expired"
        bars_held = min(horizon, len(series) - option_idx - 1)

        for offset in range(1, min(horizon, len(series) - option_idx - 1) + 1):
            candle = series[option_idx + offset]
            low = float(candle.low_price)
            high = float(candle.high_price)
            if low <= stop:
                exit_price = stop * (1 - exit_friction_pct / 100)
                outcome = "stop_loss"
                bars_held = offset
                break
            if high >= target:
                exit_price = target * (1 - exit_friction_pct / 100)
                outcome = "target"
                bars_held = offset
                break

        gross_pnl_pct = ((exit_price - entry) / entry) * 100
        charges_pct = settings.backtest_charges_pct
        pnl_pct = gross_pnl_pct - charges_pct
        return OptionBacktestTrade(
            timestamp=timestamp.isoformat(sep=" ") if isinstance(timestamp, datetime) else str(timestamp),
            direction=direction,
            underlying_entry=round(spot, 2),
            tradingsymbol=selected_symbol,
            option_entry=round(entry, 2),
            option_exit=round(exit_price, 2),
            outcome=outcome,
            pnl_pct=round(pnl_pct, 3),
            gross_pnl_pct=round(gross_pnl_pct, 3),
            charges_pct=round(charges_pct, 3),
            slippage_pct=round(settings.backtest_slippage_pct * 2, 3),
            spread_impact_pct=round(settings.paper_spread_impact_pct_per_side * 2, 3),
            bars_held=bars_held,
            reason=reason,
        )

    def _summarize(self, trades: list[BacktestTrade]) -> dict[str, Any]:
        if not trades:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "average_win_pct": 0.0,
                "average_loss_pct": 0.0,
                "avg_move_pct": 0.0,
                "expectancy_pct": 0.0,
                "profit_factor": None,
                "max_drawdown_pct": 0.0,
                "avg_bars_held": 0.0,
            }
        wins = [trade for trade in trades if trade.outcome == "target"]
        losses = [trade for trade in trades if trade.outcome == "stop_loss"]
        moves = [trade.move_pct for trade in trades]
        gross_win = sum(trade.move_pct for trade in wins)
        gross_loss = abs(sum(trade.move_pct for trade in losses))
        max_drawdown = self._max_drawdown([trade.move_pct for trade in trades])
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "expired": len([trade for trade in trades if trade.outcome == "expired"]),
            "win_rate": round((len(wins) / len(trades)) * 100, 2),
            "average_win_pct": round(gross_win / len(wins), 3) if wins else 0.0,
            "average_loss_pct": round(gross_loss / len(losses), 3) if losses else 0.0,
            "avg_move_pct": round(sum(moves) / len(moves), 3),
            "expectancy_pct": round((gross_win - gross_loss) / len(trades), 3),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown_pct": round(max_drawdown, 3),
            "avg_bars_held": round(sum(trade.bars_held for trade in trades) / len(trades), 2),
        }

    def _summarize_option_trades(self, trades: list[OptionBacktestTrade], timeframe: str = "5minute") -> dict[str, Any]:
        if not trades:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "expired": 0,
                "win_rate": 0.0,
                "average_win_pct": 0.0,
                "average_loss_pct": 0.0,
                "avg_pnl_pct": 0.0,
                "expectancy_pct": 0.0,
                "profit_factor": None,
                "max_drawdown_pct": 0.0,
                "charges_pct_per_trade": settings.backtest_charges_pct,
                "slippage_pct_per_side": settings.backtest_slippage_pct,
                "avg_bars_held": 0.0,
                "avg_time_in_trade_minutes": 0.0,
                "gross_pnl_pct": 0.0,
                "net_pnl_pct": 0.0,
            }
        wins = [trade for trade in trades if trade.pnl_pct > 0]
        losses = [trade for trade in trades if trade.pnl_pct < 0]
        gross_win = sum(trade.pnl_pct for trade in wins)
        gross_loss = abs(sum(trade.pnl_pct for trade in losses))
        raw_gross_win = sum(trade.gross_pnl_pct for trade in trades if trade.gross_pnl_pct > 0)
        raw_gross_loss = abs(sum(trade.gross_pnl_pct for trade in trades if trade.gross_pnl_pct < 0))
        avg_bars = sum(trade.bars_held for trade in trades) / len(trades)
        avg_minutes = avg_bars * self._timeframe_minutes(timeframe)
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "expired": len([trade for trade in trades if trade.outcome == "expired"]),
            "win_rate": round((len(wins) / len(trades)) * 100, 2),
            "average_win_pct": round(gross_win / len(wins), 3) if wins else 0.0,
            "average_loss_pct": round(gross_loss / len(losses), 3) if losses else 0.0,
            "avg_pnl_pct": round(sum(trade.pnl_pct for trade in trades) / len(trades), 3),
            "expectancy_pct": round((gross_win - gross_loss) / len(trades), 3),
            "gross_expectancy_pct": round((raw_gross_win - raw_gross_loss) / len(trades), 3),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "gross_profit_factor": round(raw_gross_win / raw_gross_loss, 2) if raw_gross_loss else None,
            "max_drawdown_pct": round(self._max_drawdown([trade.pnl_pct for trade in trades]), 3),
            "charges_pct_per_trade": settings.backtest_charges_pct,
            "slippage_pct_per_side": settings.backtest_slippage_pct,
            "spread_impact_pct_per_side": settings.paper_spread_impact_pct_per_side,
            "avg_bars_held": round(avg_bars, 2),
            "avg_time_in_trade_minutes": round(avg_minutes, 2),
            "gross_pnl_pct": round(sum(trade.gross_pnl_pct for trade in trades), 3),
            "net_pnl_pct": round(sum(trade.pnl_pct for trade in trades), 3),
        }

    def _ablation_impact(self, baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        baseline_trades = int(baseline.get("trades") or 0)
        current_trades = int(current.get("trades") or 0)
        baseline_wins = int(baseline.get("wins") or 0)
        current_wins = int(current.get("wins") or 0)
        return {
            "net_expectancy_delta": round(float(current.get("expectancy_pct") or 0.0) - float(baseline.get("expectancy_pct") or 0.0), 3),
            "win_rate_delta": round(float(current.get("win_rate") or 0.0) - float(baseline.get("win_rate") or 0.0), 2),
            "profit_factor_delta": self._optional_delta(current.get("profit_factor"), baseline.get("profit_factor")),
            "max_drawdown_delta": round(float(current.get("max_drawdown_pct") or 0.0) - float(baseline.get("max_drawdown_pct") or 0.0), 3),
            "trade_count_delta": current_trades - baseline_trades,
            "missed_winning_trades": max(0, baseline_wins - current_wins),
        }

    def _optional_delta(self, current: Any, baseline: Any) -> float | None:
        if current is None or baseline is None:
            return None
        return round(float(current) - float(baseline), 3)

    def _segment_option_trades(self, trades: list[OptionBacktestTrade], timeframe: str = "5minute") -> dict[str, Any]:
        return {
            "ce_vs_pe": self._summarize_groups(trades, lambda trade: "CE" if trade.direction == "CALL" else "PE", timeframe),
            "expiry_day": self._summarize_groups(trades, lambda trade: "expiry_day" if self._is_expiry_day_trade(trade) else "non_expiry_or_unknown", timeframe),
            "time_bucket": self._summarize_groups(trades, lambda trade: self._time_bucket(trade.timestamp), timeframe),
            "moneyness": self._summarize_groups(trades, self._moneyness_bucket, timeframe),
            "setup": self._summarize_groups(trades, lambda trade: trade.reason.split(":", 1)[0] if ":" in trade.reason else trade.reason[:60], timeframe),
        }

    def _summarize_groups(self, trades: list[OptionBacktestTrade], key_fn: Any, timeframe: str) -> dict[str, Any]:
        groups: dict[str, list[OptionBacktestTrade]] = {}
        for trade in trades:
            groups.setdefault(str(key_fn(trade)), []).append(trade)
        return {key: self._summarize_option_trades(items, timeframe=timeframe) for key, items in groups.items()}

    def _time_bucket(self, timestamp: str) -> str:
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return "unknown"
        total = parsed.hour * 60 + parsed.minute
        if total < 10 * 60:
            return "open_early"
        if total < 11 * 60 + 30:
            return "morning"
        if total < 13 * 60 + 30:
            return "midday"
        if total < 14 * 60 + 45:
            return "afternoon"
        return "closing"

    def _moneyness_bucket(self, trade: OptionBacktestTrade) -> str:
        parsed = self._parse_option_symbol(trade.tradingsymbol)
        if parsed is None:
            return "unknown"
        distance_pct = ((float(parsed["strike"]) - trade.underlying_entry) / max(trade.underlying_entry, 0.01)) * 100
        if abs(distance_pct) <= 0.15:
            return "ATM"
        if trade.direction == "CALL":
            return "ITM" if distance_pct < 0 else "OTM"
        return "ITM" if distance_pct > 0 else "OTM"

    def _is_expiry_day_trade(self, trade: OptionBacktestTrade) -> bool:
        expiry = self._parse_expiry_from_symbol(trade.tradingsymbol, trade.timestamp)
        if expiry is None:
            return False
        try:
            trade_date = datetime.fromisoformat(trade.timestamp).date()
        except ValueError:
            return False
        return trade_date == expiry.date()

    def _parse_expiry_from_symbol(self, symbol: str, timestamp: str) -> datetime | None:
        match = re.search(r"(?P<day>\d{2})(?P<month>[A-Z]{3})\d+(?:\.\d+)?(?:CE|PE)$", symbol.upper())
        if not match:
            return None
        try:
            year = datetime.fromisoformat(timestamp).year
            return datetime.strptime(f"{year}-{match.group('month')}-{match.group('day')}", "%Y-%b-%d")
        except ValueError:
            return None

    def _max_drawdown(self, returns_pct: list[float]) -> float:
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in returns_pct:
            equity += value
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        return abs(max_drawdown)

    def _timeframe_minutes(self, timeframe: str) -> float:
        match = re.match(r"(\d+)", timeframe)
        return float(match.group(1)) if match else 5.0

    def _validation_passed(self, summary: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if int(summary.get("trades") or 0) < settings.min_strategy_trades:
            reasons.append("not enough out-of-sample trades")
        if float(summary.get("expectancy_pct") or 0.0) < settings.min_strategy_expectancy_pct:
            reasons.append("out-of-sample expectancy is below threshold")
        profit_factor = summary.get("profit_factor")
        if profit_factor is None or float(profit_factor) < settings.min_strategy_profit_factor:
            reasons.append("out-of-sample profit factor is below threshold")
        if float(summary.get("win_rate") or 0.0) < settings.min_strategy_win_rate_pct:
            reasons.append("out-of-sample win rate is below threshold")
        return not reasons, reasons

    def _select_option_symbol(self, option_candles: dict[str, list[Candle]], *, option_type: str, spot: float, timestamp: datetime) -> str | None:
        candidates: list[tuple[float, str]] = []
        for symbol, candles in option_candles.items():
            parsed = self._parse_option_symbol(symbol)
            if parsed is None or parsed["option_type"] != option_type:
                continue
            if self._find_candle_index_at_or_after(candles, timestamp) is None:
                continue
            candidates.append((abs(float(parsed["strike"]) - spot), symbol))
        if not candidates:
            return None
        return sorted(candidates)[0][1]

    def _find_candle_index_at_or_after(self, candles: list[Candle], timestamp: datetime) -> int | None:
        target = timestamp.replace(tzinfo=None) if isinstance(timestamp, datetime) else timestamp
        for idx, candle in enumerate(candles):
            candle_ts = candle.timestamp.replace(tzinfo=None) if isinstance(candle.timestamp, datetime) else candle.timestamp
            if candle_ts >= target:
                return idx
        return None

    def _parse_option_symbol(self, symbol: str) -> dict[str, Any] | None:
        match = re.search(r"(?P<strike>\d+(?:\.\d+)?)(?P<option_type>CE|PE)$", symbol.upper())
        if not match:
            return None
        return {"strike": float(match.group("strike")), "option_type": match.group("option_type")}
