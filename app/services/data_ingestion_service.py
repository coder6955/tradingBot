from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.greeks_service import GreeksService
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.market_data_service import MarketDataService
from app.services.option_history_repository import OptionHistoryRepository
from app.services.time_utils import ist_now_naive


class DataIngestionService:
    """Ingest underlying candles and option-chain snapshots needed for research/backtests."""

    INDEX_ALIASES = {
        "NIFTY": "NIFTY 50",
        "BANKNIFTY": "NIFTY BANK",
        "FINNIFTY": "NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NIFTY MID SELECT",
        "INDIAVIX": "INDIA VIX",
        "VIX": "INDIA VIX",
    }

    def __init__(
        self,
        *,
        kite_provider_factory: Any,
        market_data_service: MarketDataService | None = None,
        option_history_repository: OptionHistoryRepository | None = None,
        greeks_service: GreeksService | None = None,
        market_data_coordinator: MarketDataCoordinator | None = None,
    ) -> None:
        self.kite_provider_factory = kite_provider_factory
        self.market_data_service = market_data_service or MarketDataService()
        self.option_history_repository = option_history_repository or OptionHistoryRepository()
        self.greeks_service = greeks_service or GreeksService()
        self.market_data_coordinator = market_data_coordinator

    def ingest_candles(
        self,
        *,
        symbols: list[str],
        timeframe: str = "5minute",
        from_date: str | None = None,
        to_date: str | None = None,
        days: int = 90,
        use_checkpoint: bool = False,
        overlap_minutes: int = 30,
    ) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        default_from_dt, to_dt = self._date_range(from_date=from_date, to_date=to_date, days=days)
        nse_instruments = self._instruments(provider, settings.default_exchange)
        results: list[dict[str, Any]] = []

        for symbol in symbols:
            normalized = symbol.upper().strip()
            token = self._find_underlying_token(nse_instruments, normalized)
            if token is None:
                results.append({"symbol": normalized, "status": "error", "message": "instrument token not found"})
                continue

            try:
                from_dt, checkpoint = self._checkpoint_from_dt(
                    symbol=normalized,
                    timeframe=timeframe,
                    default_from_dt=default_from_dt,
                    explicit_from_date=from_date,
                    use_checkpoint=use_checkpoint,
                    overlap_minutes=overlap_minutes,
                )
                fetched = provider.historical_data(token, from_dt, to_dt, timeframe)
                candles = [self._kite_candle_to_row(item) for item in fetched]
                inserted = self.market_data_service.save_candles(normalized, timeframe, candles)
                results.append(
                    {
                        "symbol": normalized,
                        "status": "ok",
                        "instrument_token": token,
                        "checkpoint": checkpoint.isoformat(sep=" ") if checkpoint else None,
                        "from": from_dt.isoformat(sep=" "),
                        "to": to_dt.isoformat(sep=" "),
                        "fetched": len(candles),
                        "inserted": inserted,
                    }
                )
            except Exception as exc:
                results.append({"symbol": normalized, "status": "error", "message": str(exc)})

        return {
            "status": "ok",
            "timeframe": timeframe,
            "from": default_from_dt.isoformat(sep=" "),
            "to": to_dt.isoformat(sep=" "),
            "use_checkpoint": use_checkpoint,
            "overlap_minutes": overlap_minutes,
            "results": results,
        }

    def capture_option_snapshots(
        self,
        *,
        symbols: list[str],
        strike_window_pct: float = 4.0,
        max_contracts_per_symbol: int = 120,
    ) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        option_instruments = self._instruments(provider, settings.option_exchange)
        nse_instruments = self._instruments(provider, settings.default_exchange)
        now = ist_now_naive()
        all_rows: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for symbol in symbols:
            normalized = symbol.upper().strip()
            spot = self._spot_price(provider, nse_instruments, normalized)
            if spot <= 0:
                results.append({"symbol": normalized, "status": "error", "message": "spot price unavailable"})
                continue

            contracts = self._nearby_option_instruments(
                option_instruments=option_instruments,
                underlying=normalized,
                spot_price=spot,
                strike_window_pct=strike_window_pct,
                limit=max_contracts_per_symbol,
            )
            quote_keys = [f"{settings.option_exchange}:{item['tradingsymbol']}" for item in contracts if item.get("tradingsymbol")]
            quotes = self._quotes(provider, quote_keys)
            rows = [
                self._option_snapshot_row(
                    symbol=normalized,
                    spot_price=spot,
                    instrument=item,
                    quote=quotes.get(f"{settings.option_exchange}:{item.get('tradingsymbol')}") or quotes.get(str(item.get("tradingsymbol"))) or {},
                    timestamp=now,
                )
                for item in contracts
            ]
            rows = [row for row in rows if row]
            all_rows.extend(rows)
            results.append({"symbol": normalized, "status": "ok", "spot_price": spot, "snapshots": len(rows)})

        import_result = self.option_history_repository.import_snapshots(all_rows)
        return {
            "status": "ok",
            "timestamp": now.isoformat(sep=" "),
            "inserted": import_result["inserted"],
            "results": results,
        }

    def ingest_option_candles(
        self,
        *,
        symbols: list[str],
        timeframe: str = "5minute",
        from_date: str | None = None,
        to_date: str | None = None,
        days: int = 30,
        strike_window_pct: float = 2.0,
        max_contracts_per_symbol: int = 20,
    ) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        from_dt, to_dt = self._date_range(from_date=from_date, to_date=to_date, days=days)
        option_instruments = self._instruments(provider, settings.option_exchange)
        nse_instruments = self._instruments(provider, settings.default_exchange)
        results: list[dict[str, Any]] = []

        for symbol in symbols:
            normalized = symbol.upper().strip()
            spot = self._spot_price(provider, nse_instruments, normalized)
            if spot <= 0:
                results.append({"symbol": normalized, "status": "error", "message": "spot price unavailable"})
                continue
            contracts = self._nearby_option_instruments(
                option_instruments=option_instruments,
                underlying=normalized,
                spot_price=spot,
                strike_window_pct=strike_window_pct,
                limit=max_contracts_per_symbol,
            )
            symbol_result = {"symbol": normalized, "status": "ok", "spot_price": spot, "contracts": []}
            for contract in contracts:
                token = self._safe_int(contract.get("instrument_token"))
                tradingsymbol = str(contract.get("tradingsymbol") or "")
                if token is None or not tradingsymbol:
                    continue
                try:
                    fetched = provider.historical_data(token, from_dt, to_dt, timeframe)
                    candles = [self._kite_candle_to_row(item) for item in fetched]
                    inserted = self.market_data_service.save_candles(tradingsymbol, timeframe, candles)
                    symbol_result["contracts"].append(
                        {
                            "tradingsymbol": tradingsymbol,
                            "instrument_token": token,
                            "fetched": len(candles),
                            "inserted": inserted,
                        }
                    )
                except Exception as exc:
                    symbol_result["contracts"].append(
                        {"tradingsymbol": tradingsymbol, "instrument_token": token, "status": "error", "message": str(exc)}
                    )
            results.append(symbol_result)

        return {
            "status": "ok",
            "timeframe": timeframe,
            "from": from_dt.isoformat(sep=" "),
            "to": to_dt.isoformat(sep=" "),
            "results": results,
        }

    def ingest_all(
        self,
        *,
        symbols: list[str],
        timeframe: str = "5minute",
        from_date: str | None = None,
        to_date: str | None = None,
        days: int = 90,
        strike_window_pct: float = 4.0,
        max_contracts_per_symbol: int = 120,
        use_checkpoint: bool = False,
        overlap_minutes: int = 30,
    ) -> dict[str, Any]:
        candles = self.ingest_candles(
            symbols=symbols,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            days=days,
            use_checkpoint=use_checkpoint,
            overlap_minutes=overlap_minutes,
        )
        option_snapshots = self.capture_option_snapshots(
            symbols=symbols,
            strike_window_pct=strike_window_pct,
            max_contracts_per_symbol=max_contracts_per_symbol,
        )
        option_candles = self.ingest_option_candles(
            symbols=symbols,
            timeframe=timeframe,
            from_date=from_date,
            to_date=to_date,
            days=min(days, 30),
            strike_window_pct=min(strike_window_pct, 2.0),
            max_contracts_per_symbol=min(max_contracts_per_symbol, 20),
        )
        return {"status": "ok", "candles": candles, "option_candles": option_candles, "option_snapshots": option_snapshots}

    def status(self, *, symbols: list[str] | None = None, timeframe: str = "5minute") -> dict[str, Any]:
        symbols = symbols or []
        return {
            "timeframe": timeframe,
            "total_candles": self.market_data_service.count_candles(timeframe=timeframe),
            "total_option_snapshots": self.option_history_repository.count_snapshots(),
            "symbols": [
                {
                    "symbol": symbol.upper(),
                    "candles": self.market_data_service.count_candles(symbol=symbol.upper(), timeframe=timeframe),
                    "latest_candle": self._format_optional_dt(self.market_data_service.latest_candle_timestamp(symbol.upper(), timeframe)),
                    "option_snapshots": self.option_history_repository.count_snapshots(symbol.upper()),
                }
                for symbol in symbols
            ],
        }

    def _date_range(self, *, from_date: str | None, to_date: str | None, days: int) -> tuple[datetime, datetime]:
        to_dt = self._parse_date(to_date) if to_date else ist_now_naive()
        from_dt = self._parse_date(from_date) if from_date else to_dt - timedelta(days=days)
        return from_dt, to_dt

    def _checkpoint_from_dt(
        self,
        *,
        symbol: str,
        timeframe: str,
        default_from_dt: datetime,
        explicit_from_date: str | None,
        use_checkpoint: bool,
        overlap_minutes: int,
    ) -> tuple[datetime, datetime | None]:
        if explicit_from_date or not use_checkpoint:
            return default_from_dt, None
        checkpoint = self.market_data_service.latest_candle_timestamp(symbol, timeframe)
        if checkpoint is None:
            return default_from_dt, None
        checkpoint = checkpoint.replace(tzinfo=None)
        return checkpoint - timedelta(minutes=max(0, overlap_minutes)), checkpoint

    def _format_optional_dt(self, value: datetime | None) -> str | None:
        return value.isoformat(sep=" ") if value else None

    def _parse_date(self, value: str | None) -> datetime:
        if not value:
            return ist_now_naive()
        return datetime.fromisoformat(value).replace(tzinfo=None)

    def _find_underlying_token(self, instruments: list[dict[str, Any]], symbol: str) -> int | None:
        target = self.INDEX_ALIASES.get(symbol, symbol).upper()
        for item in instruments:
            tradingsymbol = str(item.get("tradingsymbol") or "").upper()
            name = str(item.get("name") or "").upper()
            if tradingsymbol == target or name == target:
                return self._safe_int(item.get("instrument_token"))
        return None

    def _spot_price(self, provider: KiteProvider, instruments: list[dict[str, Any]], symbol: str) -> float:
        kite_symbol = self.INDEX_ALIASES.get(symbol, symbol)
        quote_key = f"{settings.default_exchange}:{kite_symbol}"
        try:
            quote = self._quotes(provider, [quote_key])
            payload = quote.get(quote_key) or {}
            price = float(payload.get("last_price") or 0)
            if price > 0:
                return price
        except Exception:
            pass

        token = self._find_underlying_token(instruments, symbol)
        if token is None:
            return 0.0
        try:
            quote = self._quotes(provider, [str(token)])
            payload = quote.get(str(token)) or {}
            return float(payload.get("last_price") or 0)
        except Exception:
            return 0.0

    def _nearby_option_instruments(
        self,
        *,
        option_instruments: list[dict[str, Any]],
        underlying: str,
        spot_price: float,
        strike_window_pct: float,
        limit: int,
    ) -> list[dict[str, Any]]:
        max_distance = max(spot_price * (strike_window_pct / 100), 300)
        matches = [
            item
            for item in option_instruments
            if self._instrument_matches(item, underlying)
            and str(item.get("instrument_type")) in {"CE", "PE"}
            and abs(float(item.get("strike") or 0) - spot_price) <= max_distance
        ]
        return sorted(matches, key=lambda item: (str(item.get("expiry") or ""), abs(float(item.get("strike") or 0) - spot_price)))[:limit]

    def _quotes(self, provider: KiteProvider, instruments: list[str]) -> dict[str, Any]:
        if self.market_data_coordinator is not None:
            return self.market_data_coordinator.quote(instruments, provider=provider)
        return provider.quote(instruments)

    def _instruments(self, provider: KiteProvider, exchange: str) -> list[dict[str, Any]]:
        if self.market_data_coordinator is not None:
            return self.market_data_coordinator.instruments(exchange, provider=provider)
        return provider.instruments(exchange)

    def _option_snapshot_row(
        self,
        *,
        symbol: str,
        spot_price: float,
        instrument: dict[str, Any],
        quote: dict[str, Any],
        timestamp: datetime,
    ) -> dict[str, Any] | None:
        tradingsymbol = str(instrument.get("tradingsymbol") or "")
        option_type = str(instrument.get("instrument_type") or "").upper()
        if not tradingsymbol or option_type not in {"CE", "PE"}:
            return None

        depth = quote.get("depth", {}) if isinstance(quote, dict) else {}
        buy_depth = depth.get("buy", []) if isinstance(depth, dict) else []
        sell_depth = depth.get("sell", []) if isinstance(depth, dict) else []
        bid = float(buy_depth[0].get("price", 0.0)) if buy_depth else 0.0
        ask = float(sell_depth[0].get("price", 0.0)) if sell_depth else 0.0
        last_price = float(quote.get("last_price") or instrument.get("last_price") or 0)
        greeks = self.greeks_service.estimate(
            spot_price=spot_price,
            strike=float(instrument.get("strike") or 0),
            option_price=max(last_price or ask or bid, 0.01),
            option_type=option_type,
            expiry=instrument.get("expiry"),
        )
        return {
            "underlying": symbol,
            "tradingsymbol": tradingsymbol,
            "exchange": str(instrument.get("exchange") or settings.option_exchange),
            "timestamp": timestamp.isoformat(sep=" "),
            "expiry": str(instrument.get("expiry") or ""),
            "strike": float(instrument.get("strike") or 0),
            "option_type": option_type,
            "last_price": last_price,
            "bid": bid,
            "ask": ask,
            "implied_volatility": greeks.implied_volatility,
            "delta": greeks.delta,
            "gamma": greeks.gamma,
            "theta": greeks.theta,
            "vega": greeks.vega,
            "open_interest": float(quote.get("oi") or instrument.get("oi") or 0),
            "volume": float(quote.get("volume") or 0),
        }

    def _kite_candle_to_row(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "timestamp": item.get("date") or item.get("timestamp"),
            "open": item.get("open"),
            "high": item.get("high"),
            "low": item.get("low"),
            "close": item.get("close"),
            "volume": item.get("volume") or 0,
        }

    def _instrument_matches(self, item: dict[str, Any], symbol: str) -> bool:
        target = symbol.upper().replace(" ", "")
        name = str(item.get("name") or "").upper().replace(" ", "")
        tradingsymbol = str(item.get("tradingsymbol") or "").upper().replace(" ", "")
        return name == target or tradingsymbol.startswith(target)

    def _safe_int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
