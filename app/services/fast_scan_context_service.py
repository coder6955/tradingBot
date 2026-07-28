from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from app.config import settings
from app.services.entry_opportunity_service import EntryOpportunityService
from app.services.strategy_lineage_service import current_strategy_lineage
from app.services.time_utils import ist_now_naive


@dataclass(frozen=True)
class FastScanContext:
    created_at: datetime
    symbols: tuple[str, ...]
    market_snapshots: dict[str, dict[str, Any]]
    completed_candles: dict[str, tuple[dict[str, Any], ...]]
    candidates: dict[str, dict[str, Any]]
    websocket_health: dict[str, Any]
    strategy_version: str
    config_hash: str


class FastScanContextService:
    """Immutable slow evidence consumed by the zero-I/O fast-rally validator."""

    def __init__(self, websocket_price_feed: Any | None = None) -> None:
        self.websocket_price_feed = websocket_price_feed
        self._lock = RLock()
        self._context: FastScanContext | None = None
        self._last_decision: dict[str, Any] | None = None

    def refresh(
        self,
        *,
        symbols: list[str],
        market_snapshots: dict[str, dict[str, Any]],
        completed_candles: dict[str, list[dict[str, Any]]] | None = None,
        candidates: dict[str, dict[str, Any]] | None = None,
        websocket_health: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        refresh_at = (created_at or ist_now_naive()).replace(tzinfo=None)
        new_completed = {name: tuple(deepcopy(rows)) for name, rows in (completed_candles or {}).items()}
        with self._lock:
            previous = self._context
        if previous is not None:
            elapsed = max(0.0, (refresh_at - previous.created_at).total_seconds())
            candle_changed = self._candle_markers(previous.completed_candles) != self._candle_markers(new_completed)
            if not candle_changed and elapsed < max(30.0, min(60.0, float(settings.fast_scan_context_refresh_seconds))):
                return self.status()
        lineage = current_strategy_lineage()
        context = FastScanContext(
            created_at=refresh_at,
            symbols=tuple(str(symbol).upper() for symbol in symbols),
            market_snapshots=deepcopy(market_snapshots),
            completed_candles=new_completed,
            candidates=deepcopy(candidates or {}),
            websocket_health=deepcopy(websocket_health or {}),
            strategy_version=str(lineage["strategy_version"]),
            config_hash=str(lineage["config_hash"]),
        )
        with self._lock:
            self._context = context
        return self.status()

    def validate(self, *, symbol: str = "BANKNIFTY", now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            context = self._context
        if context is None:
            return {"passed": False, "reason": "fast_scan_context_missing"}
        current = (now or ist_now_naive()).replace(tzinfo=None)
        age = max(0.0, (current - context.created_at).total_seconds())
        lineage = current_strategy_lineage()
        if age > settings.fast_scan_context_max_age_seconds:
            return {"passed": False, "reason": "fast_scan_context_stale", "age_seconds": round(age, 3)}
        if context.config_hash != lineage["config_hash"] or context.strategy_version != lineage["strategy_version"]:
            return {"passed": False, "reason": "fast_scan_context_config_mismatch", "age_seconds": round(age, 3)}
        if symbol.upper() not in context.symbols:
            return {"passed": False, "reason": "fast_scan_symbol_not_prepared", "age_seconds": round(age, 3)}
        return {
            "passed": True,
            "age_seconds": round(age, 3),
            "created_at": context.created_at.isoformat(sep=" "),
            "strategy_version": context.strategy_version,
            "config_hash": context.config_hash,
        }

    def validate_candidate(self, event: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Validate one cached direction without broker, database or persistence fallback."""
        current = (now or ist_now_naive()).replace(tzinfo=None)
        base = self.validate(symbol="BANKNIFTY", now=current)
        if not base.get("passed"):
            return self._remember({**base, "stage": "fast_candidate_rejected"})
        direction = str(event.get("direction") or "").lower()
        if direction not in {"bullish", "bearish"}:
            return self._remember({**base, "passed": False, "reason": "fast_candidate_direction_invalid"})
        with self._lock:
            context = self._context
        candidate = deepcopy((context.candidates if context else {}).get(direction) or {})
        if not candidate:
            return self._remember({**base, "passed": False, "reason": "fast_candidate_not_prewarmed", "direction": direction})

        for key, reason in (
            ("directional_agreement", "one_minute_and_five_minute_direction_disagree"),
            ("constituent_participation", "banknifty_constituent_participation_failed"),
            ("data_fresh", "fast_candidate_context_data_stale"),
            ("gap_safe", "fast_candidate_data_gap_active"),
            ("risk_preflight", "fast_candidate_risk_preflight_failed"),
        ):
            if candidate.get(key) is not True:
                return self._remember({**base, "passed": False, "reason": reason, "direction": direction})

        health = self._websocket_health()
        if not health.get("connected") or health.get("entry_blocking_gap"):
            reason = "fast_candidate_websocket_disconnected" if not health.get("connected") else "fast_candidate_data_gap_active"
            return self._remember({**base, "passed": False, "reason": reason, "direction": direction})

        token = self._safe_int(candidate.get("instrument_token"))
        tick = self.websocket_price_feed.get_latest_tick(token) if token and self.websocket_price_feed is not None else None
        if tick is None:
            return self._remember({**base, "passed": False, "reason": "prewarmed_option_quote_missing", "direction": direction})
        quote_at = (getattr(tick, "receive_timestamp", None) or getattr(tick, "timestamp", None))
        quote_at = quote_at.replace(tzinfo=None) if isinstance(quote_at, datetime) else None
        quote_age = max(0.0, (current - quote_at).total_seconds()) if quote_at else None
        if quote_age is None or quote_age > settings.websocket_price_stale_seconds:
            return self._remember({**base, "passed": False, "reason": "prewarmed_option_quote_stale", "quote_age_seconds": quote_age})

        last = self._safe_float(getattr(tick, "price", None))
        bid = self._safe_float(getattr(tick, "bid", None))
        ask = self._safe_float(getattr(tick, "ask", None))
        depth = sum(self._safe_float(level.get("quantity") or level.get("qty")) for level in (getattr(tick, "buy_depth", ()) or ()))
        if min(last, bid, ask) <= 0 or ask < bid:
            return self._remember({**base, "passed": False, "reason": "prewarmed_option_quote_not_executable"})
        spread_pct = ((ask - bid) / max(last, 0.01)) * 100.0
        if spread_pct > settings.max_execution_spread_pct:
            return self._remember({**base, "passed": False, "reason": "prewarmed_option_spread_too_wide", "spread_pct": round(spread_pct, 3)})
        if depth < settings.contract_min_depth_quantity:
            return self._remember({**base, "passed": False, "reason": "prewarmed_option_bid_depth_insufficient", "bid_depth_quantity": depth})
        if self._safe_float(getattr(tick, "volume", None)) <= 0:
            return self._remember({**base, "passed": False, "reason": "prewarmed_option_participation_missing"})

        trigger = self._safe_float(candidate.get("entry_trigger_price"))
        if trigger <= 0 or min(last, bid) < trigger:
            return self._remember({**base, "passed": False, "reason": "premium_trigger_not_confirmed", "confirmation_price": min(last, bid), "trigger": trigger})
        stop = self._safe_float(candidate.get("stop_loss"))
        target = self._safe_float(candidate.get("target_1"))
        original_entry = self._safe_float(candidate.get("entry_price"))
        opportunity = EntryOpportunityService().evaluate(
            current=ask,
            trigger=trigger,
            base=self._safe_float(candidate.get("base_price")) or original_entry,
            stop=stop,
            target=target,
            spread_pct=spread_pct,
            observations=[original_entry, last, bid, ask],
            volatility_scale=self._safe_float(candidate.get("opportunity_scale")),
            expected_move_coverage=self._optional_float(candidate.get("expected_move_coverage")),
            room_to_level_pct=self._optional_float(candidate.get("room_to_level_pct")),
            breakout_accepted=bool(candidate.get("breakout_accepted")),
        )
        if not opportunity["passed"]:
            return self._remember(
                {
                    **base,
                    "passed": False,
                    "reason": str(opportunity["blockers"][0]),
                    "opportunity": opportunity,
                }
            )
        plan = deepcopy(candidate.get("plan") or {})

        return self._remember(
            {
                **base,
                "passed": True,
                "stage": "cached_candidate_confirmed",
                "direction": direction,
                "instrument_token": token,
                "tradingsymbol": candidate.get("tradingsymbol"),
                "quote_age_seconds": round(quote_age, 3),
                "spread_pct": round(spread_pct, 3),
                "bid_depth_quantity": round(depth, 3),
                "remaining_risk_reward": opportunity["remaining_risk_reward"],
                "normalized_chase": opportunity["normalized_chase"],
                "opportunity": opportunity,
                "action": "promote_precomputed_plan_to_armed_entry" if plan else "observe_existing_armed_entry",
                "plan": plan,
            }
        )

    def status(self) -> dict[str, Any]:
        result = self.validate()
        with self._lock:
            context = self._context
            last_decision = deepcopy(self._last_decision)
        result.update(
            {
                "max_age_seconds": settings.fast_scan_context_max_age_seconds,
                "timeframes": ["1minute", "5minute"],
                "candidate_directions": sorted((context.candidates if context else {}).keys()),
                "last_decision": last_decision,
            }
        )
        return result

    def _websocket_health(self) -> dict[str, Any]:
        if self.websocket_price_feed is None:
            return {"connected": False, "entry_blocking_gap": True}
        health = getattr(self.websocket_price_feed, "entry_health", None)
        if callable(health):
            return dict(health())
        return {"connected": bool(getattr(self.websocket_price_feed, "connected", False)), "entry_blocking_gap": False}

    def _remember(self, decision: dict[str, Any]) -> dict[str, Any]:
        payload = {**decision, "decided_at": ist_now_naive().isoformat(sep=" ")}
        with self._lock:
            self._last_decision = deepcopy(payload)
        return payload

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _safe_int(self, value: Any) -> int | None:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    def _optional_float(self, value: Any) -> float | None:
        parsed = self._safe_float(value)
        return parsed if parsed > 0 else None

    def _candle_markers(self, candles: dict[str, tuple[dict[str, Any], ...]]) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (
                    str(timeframe),
                    str(rows[-1].get("last_completed_at") if rows else ""),
                )
                for timeframe, rows in candles.items()
            )
        )
