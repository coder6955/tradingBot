from __future__ import annotations

import json
import re
import time as time_module
from datetime import date, datetime, timedelta
from typing import Any, Callable

from app.config import settings
from app.providers.kite_provider import KiteProvider
from app.services.database import (
    Candle,
    RawTickRecord,
    RejectedOpportunityRecord,
    get_session,
)
from app.services.market_data_coordinator import MarketDataCoordinator
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.time_utils import ist_today


KiteProviderFactory = Callable[[], KiteProvider]


class RejectedOpportunityOutcomeService:
    """Evaluate whether rejected setups would later have hit target or stop."""

    def __init__(
        self,
        repository: RejectedOpportunityRepository,
        kite_provider_factory: KiteProviderFactory,
        market_data_coordinator: MarketDataCoordinator | None = None,
    ) -> None:
        self.repository = repository
        self.kite_provider_factory = kite_provider_factory
        self.market_data_coordinator = market_data_coordinator
        self.last_result: dict[str, Any] | None = None
        self._token_cache: dict[tuple[str, str], int | None] = {}

    def evaluate_once(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        limit: int = 100,
        learning_only: bool = True,
    ) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        rows = self.repository.list_pending_later_outcomes(
            symbol=symbol, limit=limit, learning_only=learning_only
        )
        results = [self._evaluate_record(provider, row) for row in rows]
        self.last_result = {
            "learning_only": learning_only,
            "evaluated": len(results),
            "updated": len([item for item in results if item.get("updated")]),
            "results": results,
        }
        return self.last_result

    def evaluate_batches(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        batch_limit: int | None = None,
        max_batches: int | None = None,
        learning_only: bool = True,
        delay_seconds: float | None = None,
    ) -> dict[str, Any]:
        provider = self.kite_provider_factory()
        limit = max(1, int(batch_limit or settings.rejected_outcome_batch_limit))
        batches = max(1, int(max_batches or settings.rejected_outcome_max_batches))
        delay = max(
            0.0,
            float(
                settings.rejected_outcome_batch_delay_seconds
                if delay_seconds is None
                else delay_seconds
            ),
        )
        after_id: int | None = None
        all_results: list[dict[str, Any]] = []
        batch_summaries: list[dict[str, Any]] = []
        for batch_no in range(1, batches + 1):
            rows = self.repository.list_pending_later_outcomes(
                symbol=symbol,
                limit=limit,
                learning_only=learning_only,
                after_id=after_id,
            )
            if not rows:
                break
            results = [self._evaluate_record(provider, row) for row in rows]
            all_results.extend(results)
            after_id = int(rows[-1].id)
            batch_summaries.append(
                {
                    "batch": batch_no,
                    "evaluated": len(results),
                    "updated": len([item for item in results if item.get("updated")]),
                    "last_id": after_id,
                }
            )
            if len(rows) < limit:
                break
            if delay > 0 and batch_no < batches:
                time_module.sleep(delay)
        self.last_result = {
            "learning_only": learning_only,
            "batch_mode": True,
            "batch_limit": limit,
            "max_batches": batches,
            "evaluated": len(all_results),
            "updated": len([item for item in all_results if item.get("updated")]),
            "batches": batch_summaries,
            "results": all_results,
        }
        return self.last_result

    def _evaluate_record(
        self, provider: KiteProvider, record: RejectedOpportunityRecord
    ) -> dict[str, Any]:
        prices = self._planned_prices(record)
        if not prices:
            return {
                "id": record.id,
                "updated": False,
                "reason": "planned_prices_unavailable",
            }

        replay_result = self._outcome_from_raw_ticks(provider, record, prices)
        if replay_result is None:
            replay_result = self._outcome_from_candle_replay(provider, record, prices)
        if replay_result is not None:
            updated = self.repository.mark_later_outcome(
                int(record.id),
                outcome=str(replay_result["outcome"]),
                exit_price=self._float(replay_result.get("exit_price")),
                notes=self._notes(
                    record,
                    str(replay_result["outcome"]),
                    float(replay_result["exit_price"]),
                    prices,
                    replay=replay_result,
                ),
                outcome_at=self._parse_datetime(replay_result.get("outcome_at")),
                outcome_minutes=self._float(replay_result.get("outcome_minutes")),
                outcome_source=str(replay_result.get("source") or "candle_replay"),
                outcome_timeframe=str(replay_result.get("timeframe") or ""),
                ambiguous=bool(replay_result.get("ambiguous")),
                confidence=str(replay_result.get("confidence") or "chronological_high"),
            )
            return {
                "id": updated.id,
                "updated": True,
                "tradingsymbol": updated.tradingsymbol,
                "later_outcome": updated.later_outcome,
                "later_exit_price": updated.later_exit_price,
                "source": replay_result.get("source"),
                "timeframe": replay_result.get("timeframe"),
                "source_symbol": replay_result.get("source_symbol"),
                "candle_timestamp": replay_result.get("candle_timestamp"),
                "outcome_minutes": replay_result.get("outcome_minutes"),
                "ambiguous": bool(replay_result.get("ambiguous")),
            }

        age_minutes = self._minutes_between(
            record.created_at, datetime.now().replace(tzinfo=None)
        )
        if (
            age_minutes is None
            or age_minutes < settings.rejected_outcome_horizon_minutes
        ):
            return {
                "id": record.id,
                "updated": False,
                "reason": "chronological_outcome_pending",
            }
        updated = self.repository.mark_later_outcome(
            int(record.id),
            outcome="censored_chronological_data_unavailable",
            notes="Excluded from learning because no chronological raw tick or candle path was available.",
            outcome_source="censored",
            confidence="censored_excluded",
        )
        return {
            "id": updated.id,
            "updated": True,
            "later_outcome": updated.later_outcome,
            "source": "censored",
        }

    def _outcome_from_raw_ticks(
        self,
        provider: KiteProvider,
        record: RejectedOpportunityRecord,
        prices: dict[str, float],
    ) -> dict[str, Any] | None:
        token = self._record_instrument_token(provider, record)
        if token is None or record.created_at is None:
            return None
        start = record.created_at.replace(tzinfo=None)
        horizon = start + timedelta(
            minutes=max(1, int(settings.rejected_outcome_horizon_minutes))
        )
        session = get_session()
        try:
            ticks = (
                session.query(RawTickRecord)
                .filter(
                    RawTickRecord.instrument_token == int(token),
                    RawTickRecord.exchange_timestamp.isnot(None),
                    RawTickRecord.exchange_timestamp >= start,
                    RawTickRecord.exchange_timestamp <= horizon,
                )
                .order_by(
                    RawTickRecord.exchange_timestamp.asc(),
                    RawTickRecord.sequence.asc(),
                    RawTickRecord.id.asc(),
                )
                .all()
            )
        finally:
            session.close()
        for tick in ticks:
            outcome = self._outcome_for_price(record, float(tick.last_price), prices)
            if outcome is not None:
                outcome_at = tick.exchange_timestamp.replace(tzinfo=None)
                return {
                    "outcome": outcome,
                    "exit_price": float(tick.last_price),
                    "outcome_at": outcome_at.isoformat(sep=" "),
                    "outcome_minutes": self._minutes_between(start, outcome_at),
                    "source": "raw_tick_replay",
                    "timeframe": "tick",
                    "confidence": "chronological_high",
                    "sequence": tick.sequence,
                    "ambiguous": False,
                }
        return None

    def _planned_prices(self, record: RejectedOpportunityRecord) -> dict[str, float]:
        factors = self._json(record.factor_scores_json)
        prices = (
            factors.get("prices", {}) if isinstance(factors.get("prices"), dict) else {}
        )
        parsed: dict[str, float] = {}
        for key in ("entry_price", "stop_loss", "target_1", "target_2", "target_3"):
            value = self._float(prices.get(key))
            if value is not None:
                parsed[key] = value
        return parsed

    def _outcome_from_candle_replay(
        self,
        provider: KiteProvider,
        record: RejectedOpportunityRecord,
        prices: dict[str, float],
    ) -> dict[str, Any] | None:
        if not settings.enable_rejected_outcome_candle_replay:
            return None
        symbols = self._replay_symbols(provider, record)
        if not symbols:
            return None

        for timeframe in self._replay_timeframes():
            since = self._replay_start(record, timeframe)
            candles = self._load_replay_candles(
                symbols=symbols, timeframe=timeframe, since=since
            )
            if not candles:
                continue
            outcome = self._replay_candles(record, prices, candles)
            if outcome is not None:
                outcome["timeframe"] = timeframe
                outcome["candles_checked"] = len(candles)
                return outcome
        return None

    def _replay_symbols(
        self, provider: KiteProvider, record: RejectedOpportunityRecord
    ) -> list[str]:
        symbols: list[str] = []
        if record.tradingsymbol:
            symbols.append(str(record.tradingsymbol).upper())
        token = self._record_instrument_token(provider, record)
        if token is not None and settings.rejected_outcome_use_ws_token_candles:
            symbols.append(
                f"{settings.websocket_candle_storage_prefix}:{int(token)}".upper()
            )
        seen: set[str] = set()
        unique: list[str] = []
        for symbol in symbols:
            if symbol and symbol not in seen:
                seen.add(symbol)
                unique.append(symbol)
        return unique

    def _record_instrument_token(
        self, provider: KiteProvider, record: RejectedOpportunityRecord
    ) -> int | None:
        factors = self._json(record.factor_scores_json)
        contract = (
            factors.get("contract", {})
            if isinstance(factors.get("contract"), dict)
            else {}
        )
        for key in ("instrument_token", "token"):
            value = self._int(contract.get(key))
            if value is not None:
                return value
        tradingsymbol = str(record.tradingsymbol or "").upper()
        if not tradingsymbol:
            return None
        exchange = str(record.exchange or settings.option_exchange).upper()
        cache_key = (exchange, tradingsymbol)
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]
        token: int | None = None
        instruments_fn = getattr(provider, "instruments", None)
        if callable(instruments_fn):
            try:
                for item in instruments_fn(exchange) or []:
                    if str(item.get("tradingsymbol") or "").upper() == tradingsymbol:
                        token = self._int(item.get("instrument_token"))
                        break
            except Exception:
                token = None
        self._token_cache[cache_key] = token
        return token

    def _replay_timeframes(self) -> list[str]:
        raw = str(settings.rejected_outcome_replay_timeframes or "1minute,5minute")
        values = [item.strip() for item in raw.split(",") if item.strip()]
        return values or ["1minute", "5minute"]

    def _replay_start(
        self, record: RejectedOpportunityRecord, timeframe: str
    ) -> datetime:
        created_at = record.created_at or datetime.combine(
            ist_today(), datetime.min.time()
        )
        created_at = created_at.replace(tzinfo=None)
        minutes = self._timeframe_minutes(timeframe)
        minute = (created_at.minute // minutes) * minutes
        return created_at.replace(minute=minute, second=0, microsecond=0)

    def _timeframe_minutes(self, timeframe: str) -> int:
        match = re.search(r"(\d+)", str(timeframe or "1minute"))
        if not match:
            return 1
        return max(1, int(match.group(1)))

    def _load_replay_candles(
        self, *, symbols: list[str], timeframe: str, since: datetime
    ) -> list[Candle]:
        session = get_session()
        try:
            return (
                session.query(Candle)
                .filter(Candle.symbol.in_(symbols))
                .filter(Candle.timeframe == timeframe)
                .filter(Candle.timestamp >= since)
                .order_by(Candle.timestamp.asc(), Candle.id.asc())
                .limit(max(1, int(settings.rejected_outcome_replay_max_candles)))
                .all()
            )
        finally:
            session.close()

    def _replay_candles(
        self,
        record: RejectedOpportunityRecord,
        prices: dict[str, float],
        candles: list[Candle],
    ) -> dict[str, Any] | None:
        for candle in candles:
            outcome = self._outcome_for_candle(record, prices, candle)
            if outcome is not None:
                return outcome
        return None

    def _outcome_for_candle(
        self,
        record: RejectedOpportunityRecord,
        prices: dict[str, float],
        candle: Candle,
    ) -> dict[str, Any] | None:
        side = str(record.side or "BUY").upper()
        stop_loss = prices.get("stop_loss")
        target = self._best_target_for_candle(side, prices, candle)
        stop_hit = False
        if stop_loss is not None:
            stop_hit = (
                candle.high_price >= stop_loss
                if side == "SELL"
                else candle.low_price <= stop_loss
            )
        target_hit = target is not None
        if not stop_hit and not target_hit:
            return None
        if stop_hit and target_hit:
            target_key, target_price = target
            return self._candle_result(
                record,
                candle,
                "ambiguous_stop_and_target_same_candle",
                float(candle.close_price),
                ambiguous=True,
                ambiguous_stop_price=float(stop_loss or candle.close_price),
                ambiguous_target_key=target_key,
                ambiguous_target_price=target_price,
            )
        if stop_hit and target is None:
            return self._candle_result(
                record,
                candle,
                "would_have_hit_stop_loss",
                float(stop_loss or candle.close_price),
            )
        if target is not None:
            key, target_price = target
            return self._candle_result(
                record, candle, f"would_have_hit_{key}", target_price
            )
        return None

    def _best_target_for_candle(
        self,
        side: str,
        prices: dict[str, float],
        candle: Candle,
    ) -> tuple[str, float] | None:
        for key in ("target_3", "target_2", "target_1"):
            target = prices.get(key)
            if target is None:
                continue
            if side == "SELL" and candle.low_price <= target:
                return key, float(target)
            if side != "SELL" and candle.high_price >= target:
                return key, float(target)
        return None

    def _candle_result(
        self,
        record: RejectedOpportunityRecord,
        candle: Candle,
        outcome: str,
        exit_price: float,
        *,
        ambiguous: bool = False,
        ambiguous_stop_price: float | None = None,
        ambiguous_target_key: str | None = None,
        ambiguous_target_price: float | None = None,
    ) -> dict[str, Any]:
        outcome_at = candle.timestamp.replace(tzinfo=None) if candle.timestamp else None
        rejection_candle_at = (
            record.created_at.replace(second=0, microsecond=0)
            if record.created_at
            else None
        )
        return {
            "outcome": outcome,
            "exit_price": exit_price,
            "source": "candle_replay",
            "source_symbol": candle.symbol,
            "candle_timestamp": candle.timestamp.isoformat(sep=" ")
            if candle.timestamp
            else None,
            "outcome_at": outcome_at.isoformat(sep=" ") if outcome_at else None,
            "outcome_minutes": self._minutes_between(rejection_candle_at, outcome_at),
            "candle_high": float(candle.high_price),
            "candle_low": float(candle.low_price),
            "candle_close": float(candle.close_price),
            "ambiguous": ambiguous,
            "confidence": "chronological_ambiguous"
            if ambiguous
            else "chronological_medium",
            "ambiguous_stop_price": ambiguous_stop_price,
            "ambiguous_target_key": ambiguous_target_key,
            "ambiguous_target_price": ambiguous_target_price,
        }

    def _current_option_price(
        self, provider: KiteProvider, record: RejectedOpportunityRecord
    ) -> float | None:
        if not record.tradingsymbol:
            return None
        instrument = (
            f"{record.exchange or settings.option_exchange}:{record.tradingsymbol}"
        )
        try:
            if self.market_data_coordinator is not None:
                quote = self.market_data_coordinator.quote(
                    [instrument], provider=provider
                )
            else:
                quote = provider.quote([instrument])
        except Exception:
            return None
        data = quote.get(instrument) or quote.get(record.tradingsymbol) or {}
        if not isinstance(data, dict):
            return None
        return self._float(data.get("last_price") or data.get("last_traded_price"))

    def _expired_price(self, record: RejectedOpportunityRecord) -> float | None:
        expiry = self._parse_date(record.expiry)
        if expiry is None or expiry >= ist_today():
            return None
        return 0.0 if str(record.side).upper() == "BUY" else None

    def _outcome_for_price(
        self, record: RejectedOpportunityRecord, price: float, prices: dict[str, float]
    ) -> str | None:
        expiry = self._parse_date(record.expiry)
        if expiry is not None and expiry < ist_today():
            return "would_have_expired"

        side = str(record.side or "BUY").upper()
        if side == "SELL":
            if prices.get("stop_loss") is not None and price >= prices["stop_loss"]:
                return "would_have_hit_stop_loss"
            for key in ("target_3", "target_2", "target_1"):
                if prices.get(key) is not None and price <= prices[key]:
                    return f"would_have_hit_{key}"
            return None

        if prices.get("stop_loss") is not None and price <= prices["stop_loss"]:
            return "would_have_hit_stop_loss"
        for key in ("target_3", "target_2", "target_1"):
            if prices.get(key) is not None and price >= prices[key]:
                return f"would_have_hit_{key}"
        return None

    def _notes(
        self,
        record: RejectedOpportunityRecord,
        outcome: str,
        exit_price: float,
        prices: dict[str, float],
        replay: dict[str, Any] | None = None,
    ) -> str:
        primary_gate = record.primary_gate or "unknown"
        note = (
            f"Auto-evaluated rejected setup; outcome={outcome}; "
            f"primary_gate={primary_gate}; exit_price={exit_price}; planned_prices={prices}"
        )
        if replay is not None:
            note += (
                f"; source=candle_replay; timeframe={replay.get('timeframe')}; "
                f"source_symbol={replay.get('source_symbol')}; candle_timestamp={replay.get('candle_timestamp')}; "
                f"outcome_minutes={replay.get('outcome_minutes')}; candles_checked={replay.get('candles_checked')}; "
                f"ambiguous={bool(replay.get('ambiguous'))}"
            )
        return note

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _float(self, value: Any) -> float | None:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            return None
        return None

    def _int(self, value: Any) -> int | None:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            return None
        return None

    def _parse_date(self, value: Any) -> date | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            return None

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        except ValueError:
            return None

    def _minutes_between(
        self, start: datetime | None, end: datetime | None
    ) -> float | None:
        if start is None or end is None:
            return None
        return round(
            max(
                0.0,
                (end.replace(tzinfo=None) - start.replace(tzinfo=None)).total_seconds()
                / 60,
            ),
            2,
        )
