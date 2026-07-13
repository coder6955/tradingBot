from __future__ import annotations

import json
import time as time_module
from collections import defaultdict
from datetime import date, time
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.database import Candle, OpportunityRecord, RejectedOpportunityRecord, TradeRecord, get_session
from app.services.greeks_service import GreeksService
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.market_data_service import MarketDataService
from app.services.option_history_repository import OptionHistoryRepository
from app.services.time_utils import ist_now_naive, ist_today


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
        self._live_backfill_last_attempts: dict[tuple[str, str], datetime] = {}

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

    def backfill_relevant_option_candles(
        self,
        *,
        symbols: list[str],
        trading_date: str | date | None = None,
        timeframes: list[str] | None = None,
        max_contracts: int | None = None,
        batch_limit: int | None = None,
        delay_seconds: float | None = None,
    ) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        day = self._parse_trading_date(trading_date)
        day_start, day_end = self._market_day_window(day)
        selected_timeframes = timeframes or self._targeted_backfill_timeframes()
        contract_limit = max(1, int(max_contracts or settings.targeted_option_candle_backfill_max_contracts))
        batch_size = max(1, int(batch_limit or settings.targeted_option_candle_backfill_batch_limit))
        delay = max(0.0, float(settings.targeted_option_candle_backfill_delay_seconds if delay_seconds is None else delay_seconds))
        contracts = self.relevant_option_contracts(symbols=symbols, trading_date=day, limit=contract_limit)
        option_instruments: list[dict[str, Any]] | None = None
        results: list[dict[str, Any]] = []
        attempted = 0
        inserted_total = 0
        fetched_total = 0

        for idx, contract in enumerate(contracts[:contract_limit], start=1):
            token = self._safe_int(contract.get("instrument_token"))
            tradingsymbol = str(contract.get("tradingsymbol") or "").upper()
            if not token and tradingsymbol:
                if option_instruments is None:
                    option_instruments = self._instruments(provider, settings.option_exchange)
                token = self._resolve_option_token(option_instruments, tradingsymbol)
            if not token or not tradingsymbol:
                results.append({**contract, "status": "skipped", "reason": "instrument_token_unavailable"})
                continue

            contract_result: dict[str, Any] = {
                **contract,
                "instrument_token": token,
                "status": "ok",
                "timeframes": {},
            }
            for timeframe in selected_timeframes:
                try:
                    fetched = provider.historical_data(int(token), day_start, day_end, timeframe)
                    candles = [self._kite_candle_to_row(item) for item in fetched]
                    inserted = self.market_data_service.save_candles(tradingsymbol, timeframe, candles)
                    attempted += 1
                    inserted_total += int(inserted)
                    fetched_total += len(candles)
                    contract_result["timeframes"][timeframe] = {
                        "status": "ok",
                        "fetched": len(candles),
                        "inserted": inserted,
                    }
                except Exception as exc:
                    contract_result["status"] = "partial"
                    contract_result["timeframes"][timeframe] = {"status": "error", "message": str(exc)}
            results.append(contract_result)
            if delay > 0 and idx % batch_size == 0 and idx < len(contracts):
                time_module.sleep(delay)

        coverage = self.option_candle_coverage_report(
            symbols=symbols,
            trading_date=day,
            timeframes=selected_timeframes,
            max_contracts=contract_limit,
        )
        return {
            "status": "ok",
            "trading_date": day.isoformat(),
            "from": day_start.isoformat(sep=" "),
            "to": day_end.isoformat(sep=" "),
            "timeframes": selected_timeframes,
            "contracts_found": len(contracts),
            "contracts_attempted": len([row for row in results if row.get("status") in {"ok", "partial"}]),
            "historical_calls": attempted,
            "fetched": fetched_total,
            "inserted": inserted_total,
            "batch_limit": batch_size,
            "delay_seconds": delay,
            "results": results,
            "coverage_after": coverage,
        }

    def relevant_option_contracts(
        self,
        *,
        symbols: list[str],
        trading_date: str | date | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        day = self._parse_trading_date(trading_date)
        day_start, day_end = self._calendar_day_window(day)
        symbol_set = {symbol.upper().strip() for symbol in symbols if symbol}
        contracts: dict[str, dict[str, Any]] = {}
        session = get_session()
        try:
            rejection_query = session.query(RejectedOpportunityRecord).filter(
                RejectedOpportunityRecord.created_at >= day_start,
                RejectedOpportunityRecord.created_at <= day_end,
                RejectedOpportunityRecord.tradingsymbol.is_not(None),
            )
            opportunity_query = session.query(OpportunityRecord).filter(
                OpportunityRecord.created_at >= day_start,
                OpportunityRecord.created_at <= day_end,
                OpportunityRecord.tradingsymbol.is_not(None),
            )
            trade_query = session.query(TradeRecord).filter(
                TradeRecord.created_at >= day_start,
                TradeRecord.created_at <= day_end,
                TradeRecord.tradingsymbol.is_not(None),
            )
            if symbol_set:
                rejection_query = rejection_query.filter(RejectedOpportunityRecord.symbol.in_(symbol_set))
                opportunity_query = opportunity_query.filter(OpportunityRecord.symbol.in_(symbol_set))
                trade_query = trade_query.filter(TradeRecord.symbol.in_(symbol_set))
            for row in rejection_query.order_by(RejectedOpportunityRecord.id.asc()).all():
                self._merge_contract(
                    contracts,
                    underlying=str(row.symbol or ""),
                    tradingsymbol=row.tradingsymbol,
                    instrument_token=self._extract_token_from_rejection(row),
                    seen_at=row.created_at,
                    source="rejected_opportunity",
                    option_type=row.option_type,
                    strike=row.strike,
                    expiry=row.expiry,
                )
            for row in opportunity_query.order_by(OpportunityRecord.id.asc()).all():
                self._merge_contract(
                    contracts,
                    underlying=str(row.symbol or ""),
                    tradingsymbol=row.tradingsymbol,
                    instrument_token=self._extract_token_from_opportunity(row),
                    seen_at=row.created_at,
                    source="accepted_opportunity",
                    option_type=self._option_type_from_symbol(row.tradingsymbol),
                    strike=row.strike,
                    expiry=row.expiry,
                )
            for row in trade_query.order_by(TradeRecord.id.asc()).all():
                self._merge_contract(
                    contracts,
                    underlying=str(row.symbol or ""),
                    tradingsymbol=row.tradingsymbol,
                    instrument_token=row.instrument_token or self._extract_token_from_trade(row),
                    seen_at=row.created_at,
                    source="trade",
                    option_type=self._option_type_from_symbol(row.tradingsymbol),
                    strike=None,
                    expiry=None,
                )
        finally:
            session.close()
        rows = sorted(
            contracts.values(),
            key=lambda item: (
                item.get("first_seen_at") or "",
                str(item.get("underlying") or ""),
                str(item.get("tradingsymbol") or ""),
            ),
        )
        max_rows = int(limit or settings.targeted_option_candle_backfill_max_contracts)
        return rows[:max(1, max_rows)]

    def option_candle_coverage_report(
        self,
        *,
        symbols: list[str],
        trading_date: str | date | None = None,
        timeframes: list[str] | None = None,
        max_contracts: int | None = None,
    ) -> dict[str, Any]:
        day = self._parse_trading_date(trading_date)
        selected_timeframes = timeframes or self._targeted_backfill_timeframes()
        contract_limit = max(1, int(max_contracts or settings.targeted_option_candle_backfill_max_contracts))
        contracts = self.relevant_option_contracts(symbols=symbols, trading_date=day, limit=contract_limit)
        _, market_close = self._market_day_window(day)
        now = ist_now_naive()
        if day == ist_today():
            market_close = min(market_close, now)
        rows: list[dict[str, Any]] = []
        summaries: dict[str, dict[str, Any]] = {}
        for contract in contracts:
            first_seen = self._parse_optional_datetime(contract.get("first_seen_at")) or self._market_day_window(day)[0]
            for timeframe in selected_timeframes:
                coverage = self._contract_candle_coverage(
                    tradingsymbol=str(contract.get("tradingsymbol") or ""),
                    instrument_token=self._safe_int(contract.get("instrument_token")),
                    timeframe=timeframe,
                    start=first_seen,
                    end=market_close,
                )
                rows.append({**contract, **coverage})
        for timeframe in selected_timeframes:
            items = [row for row in rows if row.get("timeframe") == timeframe]
            summaries[timeframe] = self._coverage_summary(items)
        all_summary = self._coverage_summary(rows)
        return {
            "status": "ok",
            "trading_date": day.isoformat(),
            "symbols": [symbol.upper() for symbol in symbols],
            "timeframes": selected_timeframes,
            "contracts": len(contracts),
            "summary": all_summary,
            "by_timeframe": summaries,
            "rows": rows,
        }

    def backfill_live_relevant_option_candle_gaps(
        self,
        *,
        symbols: list[str],
        now: datetime | None = None,
        timeframes: list[str] | None = None,
        max_contracts: int | None = None,
        lookback_minutes: int | None = None,
        batch_limit: int | None = None,
        delay_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not settings.enable_live_option_candle_gap_backfill:
            return {"status": "skipped", "reason": "live_option_candle_gap_backfill_disabled"}
        current = (now or ist_now_naive()).replace(tzinfo=None)
        day = current.date()
        contracts = self.relevant_option_contracts(
            symbols=symbols,
            trading_date=day,
            limit=max(1, int(max_contracts or settings.live_option_candle_backfill_max_contracts)),
        )
        return self._backfill_live_contract_gaps(
            contracts=contracts,
            now=current,
            timeframes=timeframes or self._live_backfill_timeframes(),
            lookback_minutes=int(lookback_minutes or settings.live_option_candle_backfill_lookback_minutes),
            batch_limit=int(batch_limit or settings.live_option_candle_backfill_batch_limit),
            delay_seconds=float(settings.live_option_candle_backfill_delay_seconds if delay_seconds is None else delay_seconds),
            reason="automation_live_gap_catchup",
        )

    def backfill_option_contract_live_gap(
        self,
        contract: Any,
        *,
        now: datetime | None = None,
        timeframes: list[str] | None = None,
        lookback_minutes: int | None = None,
        reason: str = "on_demand_premium_confirmation",
    ) -> dict[str, Any]:
        if not settings.enable_live_option_candle_gap_backfill:
            return {"status": "skipped", "reason": "live_option_candle_gap_backfill_disabled"}
        current = (now or ist_now_naive()).replace(tzinfo=None)
        payload = self._contract_payload_from_object(contract, seen_at=current, source=reason)
        if not payload.get("tradingsymbol"):
            return {"status": "skipped", "reason": "tradingsymbol_unavailable"}
        return self._backfill_live_contract_gaps(
            contracts=[payload],
            now=current,
            timeframes=timeframes or self._live_backfill_timeframes(),
            lookback_minutes=int(lookback_minutes or settings.live_option_candle_backfill_lookback_minutes),
            batch_limit=1,
            delay_seconds=0.0,
            reason=reason,
        )

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

    def _targeted_backfill_timeframes(self) -> list[str]:
        raw = str(settings.targeted_option_candle_backfill_timeframes or "1minute,5minute")
        values = [item.strip() for item in raw.split(",") if item.strip()]
        return values or ["1minute", "5minute"]

    def _live_backfill_timeframes(self) -> list[str]:
        raw = str(settings.live_option_candle_backfill_timeframes or "1minute")
        values = [item.strip() for item in raw.split(",") if item.strip()]
        return values or ["1minute"]

    def _backfill_live_contract_gaps(
        self,
        *,
        contracts: list[dict[str, Any]],
        now: datetime,
        timeframes: list[str],
        lookback_minutes: int,
        batch_limit: int,
        delay_seconds: float,
        reason: str,
    ) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        market_start, market_close = self._market_day_window(now.date())
        live_end = min(now.replace(second=0, microsecond=0), market_close)
        if live_end < market_start:
            return {"status": "skipped", "reason": "outside_market_window"}
        option_instruments: list[dict[str, Any]] | None = None
        results: list[dict[str, Any]] = []
        attempted = 0
        inserted_total = 0
        fetched_total = 0
        batch_size = max(1, int(batch_limit or 1))
        delay = max(0.0, float(delay_seconds))
        for idx, contract in enumerate(contracts, start=1):
            token = self._safe_int(contract.get("instrument_token"))
            tradingsymbol = str(contract.get("tradingsymbol") or "").upper()
            if not token and tradingsymbol:
                if option_instruments is None:
                    option_instruments = self._instruments(provider, settings.option_exchange)
                token = self._resolve_option_token(option_instruments, tradingsymbol)
            if not token or not tradingsymbol:
                results.append({**contract, "status": "skipped", "reason": "instrument_token_unavailable"})
                continue
            contract_result: dict[str, Any] = {**contract, "instrument_token": token, "status": "ok", "timeframes": {}}
            for timeframe in timeframes:
                backfill_window = self._live_gap_window(
                    tradingsymbol=tradingsymbol,
                    instrument_token=token,
                    timeframe=timeframe,
                    now=live_end,
                    market_start=market_start,
                    lookback_minutes=lookback_minutes,
                )
                if not backfill_window.get("needed"):
                    contract_result["timeframes"][timeframe] = backfill_window
                    continue
                cooldown = self._live_backfill_cooldown(tradingsymbol=tradingsymbol, timeframe=timeframe, now=now, reason=reason)
                if cooldown is not None:
                    contract_result["timeframes"][timeframe] = {**backfill_window, "status": "skipped", "reason": "recently_attempted", "cooldown_seconds_remaining": cooldown}
                    continue
                try:
                    start = self._parse_optional_datetime(backfill_window.get("from"))
                    end = self._parse_optional_datetime(backfill_window.get("to"))
                    if start is None or end is None or end < start:
                        contract_result["timeframes"][timeframe] = {**backfill_window, "status": "skipped", "reason": "invalid_backfill_window"}
                        continue
                    fetched = provider.historical_data(int(token), start, end, timeframe)
                    candles = [self._kite_candle_to_row(item) for item in fetched]
                    inserted = self.market_data_service.save_candles(tradingsymbol, timeframe, candles)
                    attempted += 1
                    inserted_total += int(inserted)
                    fetched_total += len(candles)
                    self._live_backfill_last_attempts[(tradingsymbol, timeframe)] = now
                    contract_result["timeframes"][timeframe] = {
                        **backfill_window,
                        "status": "ok",
                        "fetched": len(candles),
                        "inserted": inserted,
                    }
                except Exception as exc:
                    self._live_backfill_last_attempts[(tradingsymbol, timeframe)] = now
                    contract_result["status"] = "partial"
                    contract_result["timeframes"][timeframe] = {**backfill_window, "status": "error", "message": str(exc)}
            results.append(contract_result)
            if delay > 0 and idx % batch_size == 0 and idx < len(contracts):
                time_module.sleep(delay)
        return {
            "status": "ok",
            "reason": reason,
            "timeframes": timeframes,
            "lookback_minutes": lookback_minutes,
            "contracts_found": len(contracts),
            "contracts_attempted": len([row for row in results if row.get("status") in {"ok", "partial"}]),
            "historical_calls": attempted,
            "fetched": fetched_total,
            "inserted": inserted_total,
            "results": results,
        }

    def _live_gap_window(
        self,
        *,
        tradingsymbol: str,
        instrument_token: int,
        timeframe: str,
        now: datetime,
        market_start: datetime,
        lookback_minutes: int,
    ) -> dict[str, Any]:
        minutes = self._timeframe_minutes(timeframe)
        latest = self._latest_contract_candle_timestamp(tradingsymbol=tradingsymbol, instrument_token=instrument_token, timeframe=timeframe)
        earliest = max(market_start, now - timedelta(minutes=max(1, lookback_minutes)))
        if latest is None:
            start = earliest
        else:
            start = max(earliest, latest.replace(second=0, microsecond=0) + timedelta(minutes=minutes))
        end = now - timedelta(minutes=minutes)
        end = self._floor_to_timeframe(end, timeframe)
        if end < start:
            return {
                "needed": False,
                "status": "skipped",
                "reason": "no_missing_closed_candles",
                "latest_candle": latest.isoformat(sep=" ") if latest else None,
                "from": start.isoformat(sep=" "),
                "to": end.isoformat(sep=" "),
            }
        gap_seconds = (end - (latest or market_start)).total_seconds() if latest else (end - start).total_seconds() + (minutes * 60)
        if gap_seconds < settings.live_option_candle_backfill_min_gap_seconds:
            return {
                "needed": False,
                "status": "skipped",
                "reason": "gap_below_threshold",
                "gap_seconds": round(max(0.0, gap_seconds), 3),
                "latest_candle": latest.isoformat(sep=" ") if latest else None,
                "from": start.isoformat(sep=" "),
                "to": end.isoformat(sep=" "),
            }
        return {
            "needed": True,
            "gap_seconds": round(max(0.0, gap_seconds), 3),
            "latest_candle": latest.isoformat(sep=" ") if latest else None,
            "from": start.isoformat(sep=" "),
            "to": end.isoformat(sep=" "),
        }

    def _live_backfill_cooldown(self, *, tradingsymbol: str, timeframe: str, now: datetime, reason: str) -> int | None:
        last = self._live_backfill_last_attempts.get((tradingsymbol.upper(), timeframe))
        if last is None:
            return None
        elapsed = max(0.0, (now - last).total_seconds())
        cooldown = (
            max(0, int(settings.live_option_candle_backfill_interval_seconds))
            if str(reason) == "automation_live_gap_catchup"
            else max(0, int(settings.on_demand_premium_candle_backfill_cooldown_seconds))
        )
        if elapsed >= cooldown:
            return None
        return int(round(cooldown - elapsed))

    def _latest_contract_candle_timestamp(self, *, tradingsymbol: str, instrument_token: int | None, timeframe: str) -> datetime | None:
        symbols = [tradingsymbol.upper()]
        if instrument_token is not None:
            symbols.append(f"{settings.websocket_candle_storage_prefix}:{int(instrument_token)}".upper())
        session = get_session()
        try:
            row = (
                session.query(Candle.timestamp)
                .filter(Candle.symbol.in_(symbols))
                .filter(Candle.timeframe == timeframe)
                .order_by(Candle.timestamp.desc())
                .first()
            )
            return row[0].replace(tzinfo=None) if row else None
        finally:
            session.close()

    def _contract_payload_from_object(self, contract: Any, *, seen_at: datetime, source: str) -> dict[str, Any]:
        return {
            "underlying": str(getattr(contract, "name", None) or getattr(contract, "underlying", None) or "BANKNIFTY").upper(),
            "tradingsymbol": str(getattr(contract, "tradingsymbol", "") or "").upper(),
            "instrument_token": self._safe_int(getattr(contract, "instrument_token", None)),
            "option_type": getattr(contract, "option_type", None) or self._option_type_from_symbol(getattr(contract, "tradingsymbol", None)),
            "strike": self._safe_float(getattr(contract, "strike", None)),
            "expiry": str(getattr(contract, "expiry", "") or "") or None,
            "first_seen_at": seen_at.isoformat(sep=" "),
            "last_seen_at": seen_at.isoformat(sep=" "),
            "sources": [source],
        }

    def _parse_trading_date(self, value: str | date | None) -> date:
        if value is None:
            return ist_today()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.fromisoformat(str(value)[:10]).date()

    def _calendar_day_window(self, day: date) -> tuple[datetime, datetime]:
        return datetime.combine(day, time.min), datetime.combine(day, time.max)

    def _market_day_window(self, day: date) -> tuple[datetime, datetime]:
        return datetime.combine(day, self._parse_time(settings.market_open_time)), datetime.combine(day, self._parse_time(settings.market_close_time))

    def _parse_time(self, value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))

    def _merge_contract(
        self,
        contracts: dict[str, dict[str, Any]],
        *,
        underlying: str,
        tradingsymbol: str | None,
        instrument_token: Any,
        seen_at: datetime | None,
        source: str,
        option_type: str | None = None,
        strike: Any = None,
        expiry: Any = None,
    ) -> None:
        symbol = str(tradingsymbol or "").upper().strip()
        if not symbol:
            return
        existing = contracts.setdefault(
            symbol,
            {
                "underlying": underlying.upper().strip(),
                "tradingsymbol": symbol,
                "instrument_token": self._safe_int(instrument_token),
                "option_type": option_type or self._option_type_from_symbol(symbol),
                "strike": self._safe_float(strike),
                "expiry": str(expiry) if expiry else None,
                "first_seen_at": seen_at.isoformat(sep=" ") if seen_at else None,
                "last_seen_at": seen_at.isoformat(sep=" ") if seen_at else None,
                "sources": [],
            },
        )
        if instrument_token and not existing.get("instrument_token"):
            existing["instrument_token"] = self._safe_int(instrument_token)
        if option_type and not existing.get("option_type"):
            existing["option_type"] = option_type
        if strike is not None and existing.get("strike") is None:
            existing["strike"] = self._safe_float(strike)
        if expiry and not existing.get("expiry"):
            existing["expiry"] = str(expiry)
        sources = set(existing.get("sources") or [])
        sources.add(source)
        existing["sources"] = sorted(sources)
        if seen_at is not None:
            current_first = self._parse_optional_datetime(existing.get("first_seen_at"))
            current_last = self._parse_optional_datetime(existing.get("last_seen_at"))
            if current_first is None or seen_at < current_first:
                existing["first_seen_at"] = seen_at.isoformat(sep=" ")
            if current_last is None or seen_at > current_last:
                existing["last_seen_at"] = seen_at.isoformat(sep=" ")

    def _extract_token_from_rejection(self, row: RejectedOpportunityRecord) -> int | None:
        factors = self._json(row.factor_scores_json)
        for path in (("contract",), ("rejection_snapshot",)):
            payload = self._nested_dict(factors, *path)
            token = self._safe_int(payload.get("instrument_token") or payload.get("token")) if payload else None
            if token is not None:
                return token
        return None

    def _extract_token_from_opportunity(self, row: OpportunityRecord) -> int | None:
        signal = self._json(row.signal_json)
        token = self._safe_int(signal.get("instrument_token"))
        if token is not None:
            return token
        factors = self._json(row.factor_scores_json)
        contract = self._nested_dict(factors, "contract")
        return self._safe_int(contract.get("instrument_token") or contract.get("token")) if contract else None

    def _extract_token_from_trade(self, row: TradeRecord) -> int | None:
        payload = self._json(row.order_response_json)
        token = self._safe_int(payload.get("instrument_token"))
        if token is not None:
            return token
        signal = self._nested_dict(payload, "signal")
        if signal:
            token = self._safe_int(signal.get("instrument_token"))
            if token is not None:
                return token
        factors = self._nested_dict(payload, "signal_factor_scores")
        contract = self._nested_dict(factors, "contract") if factors else {}
        return self._safe_int(contract.get("instrument_token") or contract.get("token")) if contract else None

    def _contract_candle_coverage(
        self,
        *,
        tradingsymbol: str,
        instrument_token: int | None,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        start = self._floor_to_timeframe(start, timeframe)
        end = end.replace(second=0, microsecond=0)
        expected = self._expected_candle_count(start=start, end=end, timeframe=timeframe)
        candle_symbols = [tradingsymbol.upper()]
        if instrument_token is not None:
            candle_symbols.append(f"{settings.websocket_candle_storage_prefix}:{int(instrument_token)}".upper())
        timestamps: set[datetime] = set()
        latest: datetime | None = None
        session = get_session()
        try:
            rows = (
                session.query(Candle.symbol, Candle.timestamp)
                .filter(Candle.symbol.in_(candle_symbols))
                .filter(Candle.timeframe == timeframe)
                .filter(Candle.timestamp >= start)
                .filter(Candle.timestamp <= end)
                .all()
            )
            normal_rows = 0
            websocket_rows = 0
            for symbol, timestamp in rows:
                normalized_ts = timestamp.replace(second=0, microsecond=0)
                timestamps.add(normalized_ts)
                latest = normalized_ts if latest is None or normalized_ts > latest else latest
                if str(symbol).upper().startswith(f"{settings.websocket_candle_storage_prefix}:".upper()):
                    websocket_rows += 1
                else:
                    normal_rows += 1
        finally:
            session.close()
        actual = len(timestamps)
        missing = max(0, expected - actual)
        coverage_pct = round((actual / expected) * 100, 2) if expected else 0.0
        quality = self._coverage_quality(expected=expected, actual=actual, coverage_pct=coverage_pct)
        return {
            "timeframe": timeframe,
            "coverage_start": start.isoformat(sep=" "),
            "coverage_end": end.isoformat(sep=" "),
            "expected_candles": expected,
            "actual_candles": actual,
            "missing_candles": missing,
            "coverage_pct": coverage_pct,
            "data_quality": quality,
            "latest_candle": latest.isoformat(sep=" ") if latest else None,
            "stored_symbol_rows": normal_rows,
            "websocket_token_rows": websocket_rows,
        }

    def _coverage_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {
                "contracts": 0,
                "high_confidence": 0,
                "partial_data": 0,
                "insufficient_candles": 0,
                "avg_coverage_pct": 0.0,
                "missing_candles": 0,
                "data_quality": "insufficient_candles",
            }
        counter: defaultdict[str, int] = defaultdict(int)
        for row in rows:
            counter[str(row.get("data_quality") or "unknown")] += 1
        avg = round(sum(float(row.get("coverage_pct") or 0.0) for row in rows) / len(rows), 2)
        missing = sum(int(row.get("missing_candles") or 0) for row in rows)
        quality = "high_confidence"
        if counter["insufficient_candles"]:
            quality = "insufficient_candles"
        elif counter["partial_data"]:
            quality = "partial_data"
        return {
            "contracts": len(rows),
            "high_confidence": counter["high_confidence"],
            "partial_data": counter["partial_data"],
            "insufficient_candles": counter["insufficient_candles"],
            "avg_coverage_pct": avg,
            "missing_candles": missing,
            "data_quality": quality,
        }

    def _coverage_quality(self, *, expected: int, actual: int, coverage_pct: float) -> str:
        if expected <= 0 or actual <= 0:
            return "insufficient_candles"
        if coverage_pct >= float(settings.min_targeted_option_candle_coverage_pct):
            return "high_confidence"
        return "partial_data"

    def _expected_candle_count(self, *, start: datetime, end: datetime, timeframe: str) -> int:
        if end < start:
            return 0
        minutes = max(1, self._timeframe_minutes(timeframe))
        return int((end - start).total_seconds() // (minutes * 60)) + 1

    def _floor_to_timeframe(self, value: datetime, timeframe: str) -> datetime:
        minutes = max(1, self._timeframe_minutes(timeframe))
        minute = (value.minute // minutes) * minutes
        return value.replace(minute=minute, second=0, microsecond=0)

    def _timeframe_minutes(self, timeframe: str) -> int:
        digits = "".join(ch for ch in str(timeframe or "1minute") if ch.isdigit())
        return max(1, int(digits or "1"))

    def _resolve_option_token(self, option_instruments: list[dict[str, Any]], tradingsymbol: str) -> int | None:
        target = tradingsymbol.upper()
        for item in option_instruments:
            if str(item.get("tradingsymbol") or "").upper() == target:
                return self._safe_int(item.get("instrument_token"))
        return None

    def _option_type_from_symbol(self, tradingsymbol: str | None) -> str | None:
        symbol = str(tradingsymbol or "").upper()
        if symbol.endswith("CE"):
            return "CE"
        if symbol.endswith("PE"):
            return "PE"
        return None

    def _parse_optional_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        except ValueError:
            return None

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _nested_dict(self, payload: dict[str, Any], *keys: str) -> dict[str, Any]:
        current: Any = payload
        for key in keys:
            if not isinstance(current, dict):
                return {}
            current = current.get(key)
        return current if isinstance(current, dict) else {}

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

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            return None
        return None
