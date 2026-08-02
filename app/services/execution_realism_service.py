from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any

from app.config import settings


@dataclass(frozen=True)
class ExecutionFill:
    intended_price: float
    fill_price: float
    filled: bool
    model: str
    reason: str
    price_impact: float
    price_impact_pct: float
    components: dict[str, float | bool | str | None]
    no_fill_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["intended_price"] = round(self.intended_price, 2)
        payload["fill_price"] = round(self.fill_price, 2)
        payload["price_impact"] = round(self.price_impact, 2)
        payload["price_impact_pct"] = round(self.price_impact_pct, 3)
        return payload


class ExecutionRealismService:
    """Conservative fill model for paper trading and historical option backtests.

    This service does not decide whether a setup is valid. It only converts an
    intended simulated price into a more realistic fill price for option buying.
    Live trades must still use broker-confirmed average prices.
    """

    model_name = "banknifty_option_execution_realism_v1"

    def entry_fill(
        self,
        *,
        intended_price: float,
        side: str = "BUY",
        bid: float | None = None,
        ask: float | None = None,
        timestamp: datetime | None = None,
        expiry: str | date | None = None,
        iv_rank: float | None = None,
    ) -> ExecutionFill:
        intended = max(0.0, float(intended_price or 0.0))
        if intended <= 0:
            return self._empty(intended, "entry_price_invalid")
        components = self._components(
            timestamp=timestamp,
            expiry=expiry,
            iv_rank=iv_rank,
            bid=bid,
            ask=ask,
            base_pct=settings.realism_entry_buy_slippage_pct,
        )
        friction_pct = self._total_pct(components)
        if str(side).upper() == "BUY":
            quoted = float(ask or 0.0)
            synthetic = intended * (1 + friction_pct / 100)
            fill = max(intended, quoted, synthetic)
        else:
            quoted = float(bid or 0.0)
            synthetic = intended * (1 - friction_pct / 100)
            fill = min(intended, quoted if quoted > 0 else synthetic, synthetic)
        return self._fill(
            intended=intended,
            fill=max(0.05, fill),
            reason="entry_buy_near_ask",
            components=components,
        )

    def exit_fill(
        self,
        *,
        intended_price: float,
        side: str = "BUY",
        outcome: str = "exit",
        candle: Any | None = None,
        bid: float | None = None,
        ask: float | None = None,
        timestamp: datetime | None = None,
        expiry: str | date | None = None,
        iv_rank: float | None = None,
    ) -> ExecutionFill:
        intended = max(0.0, float(intended_price or 0.0))
        if intended <= 0:
            return self._empty(intended, "exit_price_invalid")
        outcome_key = str(outcome or "").lower()
        buffer_pct = settings.realism_no_fill_touch_buffer_pct
        if (
            "target" in outcome_key
            and candle is not None
            and str(side).upper() == "BUY"
        ):
            candle_high = self._candle_value(candle, "high_price", "high")
            if candle_high > 0 and candle_high < intended * (1 + buffer_pct / 100):
                return ExecutionFill(
                    intended_price=intended,
                    fill_price=0.0,
                    filled=False,
                    model=self.model_name,
                    reason="target_touch_not_enough_for_realistic_fill",
                    price_impact=0.0,
                    price_impact_pct=0.0,
                    components={
                        "no_fill_touch_buffer_pct": buffer_pct,
                        "candle_high": round(candle_high, 2),
                    },
                    no_fill_reason="target_barely_touched",
                )
        base_pct = (
            settings.realism_exit_stop_overshoot_pct
            if "stop" in outcome_key
            else settings.realism_exit_target_slippage_pct
        )
        components = self._components(
            timestamp=timestamp,
            expiry=expiry,
            iv_rank=iv_rank,
            bid=bid,
            ask=ask,
            base_pct=base_pct,
        )
        friction_pct = self._total_pct(components)
        if str(side).upper() == "BUY":
            quoted = float(bid or 0.0)
            synthetic = intended * (1 - friction_pct / 100)
            fill = min(intended, quoted if quoted > 0 else synthetic, synthetic)
            reason = (
                "stop_overshoot_sell_near_bid"
                if "stop" in outcome_key
                else "target_sell_near_bid"
            )
        else:
            quoted = float(ask or 0.0)
            synthetic = intended * (1 + friction_pct / 100)
            fill = max(intended, quoted, synthetic)
            reason = "short_exit_buy_near_ask"
        return self._fill(
            intended=intended,
            fill=max(0.05, fill),
            reason=reason,
            components=components,
        )

    def assumptions(self) -> dict[str, Any]:
        return {
            "enabled": settings.enable_execution_realism,
            "model": self.model_name,
            "entry_buy_slippage_pct": settings.realism_entry_buy_slippage_pct,
            "exit_target_slippage_pct": settings.realism_exit_target_slippage_pct,
            "exit_stop_overshoot_pct": settings.realism_exit_stop_overshoot_pct,
            "no_fill_touch_buffer_pct": settings.realism_no_fill_touch_buffer_pct,
            "first_15_min_extra_slippage_pct": settings.realism_first_15_min_extra_slippage_pct,
            "expiry_day_extra_slippage_pct": settings.realism_expiry_day_extra_slippage_pct,
            "high_iv_extra_slippage_pct": settings.realism_high_iv_extra_slippage_pct,
            "wide_spread_extra_slippage_pct": settings.realism_wide_spread_extra_slippage_pct,
        }

    def _empty(self, intended: float, reason: str) -> ExecutionFill:
        return ExecutionFill(
            intended_price=intended,
            fill_price=0.0,
            filled=False,
            model=self.model_name,
            reason=reason,
            price_impact=0.0,
            price_impact_pct=0.0,
            components={},
            no_fill_reason=reason,
        )

    def _fill(
        self, *, intended: float, fill: float, reason: str, components: dict[str, Any]
    ) -> ExecutionFill:
        impact = fill - intended
        return ExecutionFill(
            intended_price=intended,
            fill_price=fill,
            filled=True,
            model=self.model_name,
            reason=reason,
            price_impact=impact,
            price_impact_pct=(impact / intended) * 100 if intended > 0 else 0.0,
            components=components,
        )

    def _components(
        self,
        *,
        timestamp: datetime | None,
        expiry: str | date | None,
        iv_rank: float | None,
        bid: float | None,
        ask: float | None,
        base_pct: float,
    ) -> dict[str, Any]:
        spread_pct = self._spread_pct(bid=bid, ask=ask)
        return {
            "base_pct": max(0.0, float(base_pct or 0.0)),
            "first_15_min_extra_pct": settings.realism_first_15_min_extra_slippage_pct
            if self._is_first_15_min(timestamp)
            else 0.0,
            "expiry_day_extra_pct": settings.realism_expiry_day_extra_slippage_pct
            if self._is_expiry_day(timestamp, expiry)
            else 0.0,
            "high_iv_extra_pct": settings.realism_high_iv_extra_slippage_pct
            if iv_rank is not None and float(iv_rank) >= 75
            else 0.0,
            "wide_spread_extra_pct": settings.realism_wide_spread_extra_slippage_pct
            if spread_pct >= settings.max_execution_spread_pct
            else 0.0,
            "spread_pct": round(spread_pct, 3),
            "bid": float(bid) if bid else None,
            "ask": float(ask) if ask else None,
        }

    def _total_pct(self, components: dict[str, Any]) -> float:
        return sum(
            float(components.get(key) or 0.0)
            for key in (
                "base_pct",
                "first_15_min_extra_pct",
                "expiry_day_extra_pct",
                "high_iv_extra_pct",
                "wide_spread_extra_pct",
            )
        )

    def _is_first_15_min(self, timestamp: datetime | None) -> bool:
        if timestamp is None:
            return False
        value = timestamp.time()
        return time(9, 15) <= value < time(9, 30)

    def _is_expiry_day(
        self, timestamp: datetime | None, expiry: str | date | None
    ) -> bool:
        if timestamp is None or expiry is None:
            return False
        expiry_date: date | None = None
        if isinstance(expiry, date):
            expiry_date = expiry
        elif isinstance(expiry, str):
            try:
                expiry_date = datetime.fromisoformat(expiry[:10]).date()
            except ValueError:
                expiry_date = None
        return bool(expiry_date and timestamp.date() == expiry_date)

    def _spread_pct(self, *, bid: float | None, ask: float | None) -> float:
        bid_value = float(bid or 0.0)
        ask_value = float(ask or 0.0)
        mid = (bid_value + ask_value) / 2
        if bid_value <= 0 or ask_value <= 0 or mid <= 0:
            return 0.0
        return ((ask_value - bid_value) / mid) * 100

    def _candle_value(self, candle: Any, *names: str) -> float:
        for name in names:
            value = getattr(candle, name, None)
            if value is None and isinstance(candle, dict):
                value = candle.get(name)
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0
