from __future__ import annotations

import math
from typing import Any


CURRENT_BOOK_PROXY = "CURRENT_BOOK_PROXY"
HISTORICAL_ADVERSE_MOVE_ESTIMATE = "HISTORICAL_ADVERSE_MOVE_ESTIMATE"
STRESS_EXIT_ESTIMATE = "STRESS_EXIT_ESTIMATE"


class StopExitLiquidityResearchService:
    """Research estimates for long-option stop exits; none guarantees a fill."""

    def estimate(
        self,
        *,
        proposed_quantity: int,
        current_bid: float | None,
        current_ask: float | None,
        current_bid_depth: int | None,
        historical_adverse_observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        quantity = max(0, int(proposed_quantity))
        bid = self._positive(current_bid)
        ask = self._positive(current_ask)
        depth = max(0, int(current_bid_depth or 0))
        current_spread = (ask - bid) if bid is not None and ask is not None else None
        current_slippage = current_spread if current_spread is not None else None
        current = {
            "estimate_name": CURRENT_BOOK_PROXY,
            "research_only": False,
            "guaranteed_fill": False,
            "observed_bid_depth": depth,
            "quantity_exceeds_observed_depth": quantity > depth,
            "estimated_slippage_per_unit": current_slippage,
            "estimated_fillable_quantity": min(quantity, depth),
            "partial_fill_assumed": quantity > depth,
            "validation_status": "ACTIVE_CONSERVATIVE_PROXY",
        }
        samples = [
            item for item in historical_adverse_observations if self._usable(item)
        ]
        historical = self._historical(quantity, samples)
        stress = self._stress(quantity, current, samples)
        recommended = current
        if historical["validation_status"] == "VALIDATED" and float(
            historical.get("estimated_slippage_per_unit") or 0.0
        ) > float(recommended.get("estimated_slippage_per_unit") or 0.0):
            recommended = historical
        if stress["validation_status"] == "VALIDATED" and float(
            stress.get("estimated_slippage_per_unit") or 0.0
        ) > float(recommended.get("estimated_slippage_per_unit") or 0.0):
            recommended = stress
        return {
            "proposed_quantity": quantity,
            "estimates": {
                CURRENT_BOOK_PROXY: current,
                HISTORICAL_ADVERSE_MOVE_ESTIMATE: historical,
                STRESS_EXIT_ESTIMATE: stress,
            },
            "active_conservative_estimate": CURRENT_BOOK_PROXY,
            "research_recommended_conservative_estimate": recommended["estimate_name"],
            "active_behavior_changed": False,
            "disclaimer": "Research estimates describe observed or stressed liquidity and do not guarantee a stop fill.",
        }

    def _historical(
        self, quantity: int, samples: list[dict[str, Any]]
    ) -> dict[str, Any]:
        slippages = sorted(float(item["executable_bid_slippage"]) for item in samples)
        depths = sorted(int(item["bid_depth"]) for item in samples)
        fill_times = sorted(
            float(item.get("exit_time_seconds") or 0.0) for item in samples
        )
        spreads = sorted(float(item.get("spread") or 0.0) for item in samples)
        spread_expansions = sorted(
            float(item.get("spread_expansion") or 0.0) for item in samples
        )
        depth_disappearance = sum(
            1 for item in samples if bool(item.get("bid_depth_disappeared"))
        )
        validated = len(samples) >= 30
        return {
            "estimate_name": HISTORICAL_ADVERSE_MOVE_ESTIMATE,
            "research_only": True,
            "guaranteed_fill": False,
            "sample_size": len(samples),
            "estimated_slippage_per_unit": self._percentile(slippages, 0.90)
            if slippages
            else None,
            "estimated_fillable_quantity": min(
                quantity, int(self._percentile(depths, 0.10))
            )
            if depths
            else 0,
            "estimated_exit_time_seconds": self._percentile(fill_times, 0.90)
            if fill_times
            else None,
            "adverse_spread_percentile": self._percentile(spreads, 0.90)
            if spreads
            else None,
            "spread_expansion_p90": self._percentile(spread_expansions, 0.90)
            if spread_expansions
            else None,
            "bid_depth_disappearance_rate_percent": round(
                depth_disappearance / len(samples) * 100.0, 4
            )
            if samples
            else None,
            "worst_observed_slippage": max(slippages) if slippages else None,
            "quantity_exceeds_observed_depth": bool(
                depths and quantity > int(self._percentile(depths, 0.10))
            ),
            "partial_fill_assumed": bool(
                depths and quantity > int(self._percentile(depths, 0.10))
            ),
            "validation_status": "VALIDATED" if validated else "INSUFFICIENT_SAMPLE",
        }

    def _stress(
        self, quantity: int, current: dict[str, Any], samples: list[dict[str, Any]]
    ) -> dict[str, Any]:
        observed_slippage = [float(item["executable_bid_slippage"]) for item in samples]
        observed_depth = [int(item["bid_depth"]) for item in samples]
        current_slippage = float(current.get("estimated_slippage_per_unit") or 0.0)
        stressed_slippage = max(
            [current_slippage * 2.0, *observed_slippage], default=current_slippage * 2.0
        )
        stressed_depth = (
            min(observed_depth)
            if observed_depth
            else max(0, int(current.get("observed_bid_depth") or 0) // 2)
        )
        return {
            "estimate_name": STRESS_EXIT_ESTIMATE,
            "research_only": True,
            "guaranteed_fill": False,
            "sample_size": len(samples),
            "estimated_slippage_per_unit": stressed_slippage,
            "estimated_fillable_quantity": min(quantity, stressed_depth),
            "quantity_exceeds_observed_depth": quantity > stressed_depth,
            "partial_fill_assumed": quantity > stressed_depth,
            "stress_assumptions": [
                "worst_observed_slippage",
                "half_current_depth_when_history_missing",
            ],
            "validation_status": "VALIDATED"
            if len(samples) >= 100
            else "RESEARCH_ONLY",
        }

    def _usable(self, item: dict[str, Any]) -> bool:
        try:
            return (
                float(item.get("executable_bid_slippage")) >= 0
                and int(item.get("bid_depth")) >= 0
            )
        except (TypeError, ValueError):
            return False

    def _positive(self, value: Any) -> float | None:
        try:
            parsed = float(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    def _percentile(self, values: list[float] | list[int], fraction: float) -> float:
        ordered = sorted(float(value) for value in values)
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]
