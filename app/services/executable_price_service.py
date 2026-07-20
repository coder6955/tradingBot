from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from app.services.active_price_feed import PriceTick


@dataclass(frozen=True)
class ExecutableExitPrice:
    ltp: float
    best_bid: float | None
    best_ask: float | None
    executable_price: float | None
    requested_quantity: int
    depth_quantity: int
    depth_coverage: float | None
    spread_pct: float | None
    price_source: str
    quote_timestamp: object
    target_supported: bool
    live_safe: bool
    rejection_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExecutablePriceService:
    """Estimate the conservative price available when selling a long option."""

    def for_long_exit(self, tick: PriceTick, *, quantity: int) -> ExecutableExitPrice:
        requested = max(1, int(quantity or 0))
        bid = self._positive(tick.bid)
        ask = self._positive(tick.ask)
        spread_pct = ((ask - bid) / bid) * 100.0 if bid and ask and ask >= bid else None
        levels = self._levels(tick.buy_depth)
        if levels:
            remaining = requested
            notional = 0.0
            filled = 0
            worst = levels[0][0]
            for price, available in levels:
                take = min(remaining, available)
                if take <= 0:
                    continue
                notional += price * take
                filled += take
                remaining -= take
                worst = min(worst, price)
                if remaining <= 0:
                    break
            coverage = min(1.0, filled / requested)
            if filled >= requested:
                executable = notional / requested
                source = "depth_weighted_bid"
                target_supported = True
                live_safe = True
                rejection = None
            else:
                executable = worst if filled > 0 else None
                source = "partial_depth_conservative_bid"
                target_supported = False
                live_safe = False
                rejection = "visible_bid_depth_does_not_cover_position"
            return ExecutableExitPrice(
                ltp=float(tick.price),
                best_bid=bid or levels[0][0],
                best_ask=ask,
                executable_price=executable,
                requested_quantity=requested,
                depth_quantity=filled,
                depth_coverage=round(coverage, 6),
                spread_pct=round(spread_pct, 4) if spread_pct is not None else None,
                price_source=source,
                quote_timestamp=tick.timestamp,
                target_supported=target_supported,
                live_safe=live_safe,
                rejection_reason=rejection,
            )
        if bid is not None:
            return ExecutableExitPrice(
                ltp=float(tick.price),
                best_bid=bid,
                best_ask=ask,
                executable_price=bid,
                requested_quantity=requested,
                depth_quantity=0,
                depth_coverage=None,
                spread_pct=round(spread_pct, 4) if spread_pct is not None else None,
                price_source="best_bid_quantity_unavailable",
                quote_timestamp=tick.timestamp,
                target_supported=True,
                live_safe=False,
                rejection_reason="bid_quantity_unavailable",
            )
        return ExecutableExitPrice(
            ltp=float(tick.price),
            best_bid=None,
            best_ask=ask,
            executable_price=None,
            requested_quantity=requested,
            depth_quantity=0,
            depth_coverage=0.0,
            spread_pct=None,
            price_source="ltp_diagnostic_only",
            quote_timestamp=tick.timestamp,
            target_supported=False,
            live_safe=False,
            rejection_reason="executable_bid_unavailable",
        )

    def _levels(self, levels: Iterable[dict[str, Any]]) -> list[tuple[float, int]]:
        result: list[tuple[float, int]] = []
        for level in levels:
            price = self._positive(level.get("price"))
            try:
                quantity = int(level.get("quantity") or level.get("qty") or 0)
            except (TypeError, ValueError):
                quantity = 0
            if price is not None and quantity > 0:
                result.append((price, quantity))
        return result

    def _positive(self, value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
