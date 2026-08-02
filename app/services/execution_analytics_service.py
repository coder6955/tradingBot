from __future__ import annotations

import json
from typing import Any

from app.services.database import TradeRecord, get_session
from app.services.trading_metrics import (
    minutes_between,
    summarize_groups,
    summarize_returns,
    time_bucket,
)


class ExecutionAnalyticsService:
    """Measure paper/live execution quality separately from signal quality."""

    def analyze(
        self, *, symbol: str | None = "BANKNIFTY", limit: int = 1000
    ) -> dict[str, Any]:
        trades = self._trades(symbol=symbol, limit=limit)
        closed = [trade for trade in trades if trade.status == "closed"]
        lineage_groups: dict[str, list[TradeRecord]] = {}
        for trade in closed:
            key = f"{getattr(trade, 'strategy_version', None) or 'legacy'}|{getattr(trade, 'config_hash', None) or 'unknown'}"
            lineage_groups.setdefault(key, []).append(trade)
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "sample": {
                "total_trades": len(trades),
                "closed": len(closed),
                "open": len([trade for trade in trades if trade.status != "closed"]),
                "live": len([trade for trade in trades if trade.mode == "live"]),
                "paper": len([trade for trade in trades if trade.mode == "paper"]),
            },
            "mixed_lineage": len(lineage_groups) > 1,
            "lineage_warning": "Execution evidence is split by strategy/config; do not combine hashes for readiness."
            if len(lineage_groups) > 1
            else None,
            "overall_by_lineage": {
                key: self._summary(items) for key, items in lineage_groups.items()
            },
            "overall": self._summary(closed),
            "segments": {
                "mode": self._groups(
                    closed, lambda trade: str(trade.mode or "unknown")
                ),
                "ce_vs_pe": self._groups(closed, self._ce_pe),
                "time_bucket": self._groups(
                    closed, lambda trade: time_bucket(trade.created_at)
                ),
                "outcome": self._groups(
                    closed, lambda trade: str(trade.outcome or "unknown")
                ),
                "exit_rule": self._groups(closed, self._exit_rule),
            },
            "execution_quality": self._execution_quality(trades),
            "exit_analytics": self._exit_analytics(closed),
            "exit_rule_ablation": self._exit_rule_ablation(closed),
        }

    def _trades(self, *, symbol: str | None, limit: int) -> list[TradeRecord]:
        session = get_session()
        try:
            query = session.query(TradeRecord).order_by(TradeRecord.id.desc())
            if symbol:
                query = query.filter(TradeRecord.symbol == symbol.upper())
            return query.limit(limit).all()
        finally:
            session.close()

    def _summary(self, trades: list[TradeRecord]) -> dict[str, Any]:
        return summarize_returns(
            trades,
            pnl_fn=lambda trade: float(
                trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0
            ),
            outcome_fn=lambda trade: trade.outcome,
            time_in_trade_fn=lambda trade: minutes_between(
                trade.created_at, trade.updated_at
            ),
        )

    def _groups(self, trades: list[TradeRecord], key_fn: Any) -> dict[str, Any]:
        return summarize_groups(
            trades,
            key_fn,
            pnl_fn=lambda trade: float(
                trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0
            ),
            outcome_fn=lambda trade: trade.outcome,
            time_in_trade_fn=lambda trade: minutes_between(
                trade.created_at, trade.updated_at
            ),
        )

    def _execution_quality(self, trades: list[TradeRecord]) -> dict[str, Any]:
        live = [trade for trade in trades if trade.mode == "live"]
        filled = [
            trade
            for trade in live
            if int(trade.filled_quantity or 0) > 0
            or str(trade.status).lower() in {"filled", "complete"}
        ]
        deviations: list[float] = []
        fill_ratios: list[float] = []
        for trade in live:
            requested = int(trade.requested_quantity or 0)
            placed = int(trade.placed_quantity or 0)
            filled_qty = int(trade.filled_quantity or 0)
            if requested > 0:
                fill_ratios.append((filled_qty or placed) / requested)
            entry = float(trade.entry_price or 0.0)
            average = float(trade.average_price or 0.0)
            if entry > 0 and average > 0:
                deviations.append(((average - entry) / entry) * 100)
        return {
            "live_orders": len(live),
            "filled_live_orders": len(filled),
            "live_fill_rate_pct": round((len(filled) / len(live)) * 100, 2)
            if live
            else 0.0,
            "avg_requested_fill_ratio_pct": round(
                (sum(fill_ratios) / len(fill_ratios)) * 100, 2
            )
            if fill_ratios
            else 0.0,
            "avg_entry_deviation_pct": round(sum(deviations) / len(deviations), 3)
            if deviations
            else 0.0,
            "max_entry_deviation_pct": round(
                max([abs(value) for value in deviations]), 3
            )
            if deviations
            else 0.0,
            "live_order_statuses": self._status_counts(live),
            "quality_payload_coverage_pct": self._quality_payload_coverage(trades),
        }

    def _status_counts(self, trades: list[TradeRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trade in trades:
            key = str(trade.status or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _quality_payload_coverage(self, trades: list[TradeRecord]) -> float:
        covered = 0
        for trade in trades:
            try:
                payload = json.loads(trade.order_response_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and "execution_quality" in payload:
                covered += 1
        return round((covered / len(trades)) * 100, 2) if trades else 0.0

    def _ce_pe(self, trade: TradeRecord) -> str:
        action = str(trade.action or "").upper()
        symbol = str(trade.tradingsymbol or "").upper()
        if "CE" in action or symbol.endswith("CE"):
            return "CE"
        if "PE" in action or symbol.endswith("PE"):
            return "PE"
        return "unknown"

    def _exit_rule(self, trade: TradeRecord) -> str:
        return str(
            getattr(trade, "exit_rule_first_triggered", None)
            or trade.outcome
            or "unknown"
        )

    def _exit_analytics(self, trades: list[TradeRecord]) -> dict[str, Any]:
        groups: dict[str, list[TradeRecord]] = {}
        for trade in trades:
            groups.setdefault(self._exit_rule(trade), []).append(trade)
        return {
            reason: self._exit_reason_summary(rows)
            for reason, rows in sorted(groups.items())
        }

    def _exit_reason_summary(self, trades: list[TradeRecord]) -> dict[str, Any]:
        net = [
            float(trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0)
            for trade in trades
        ]
        mfe = [
            float(trade.mfe_percent)
            for trade in trades
            if trade.mfe_percent is not None
        ]
        mae = [
            float(trade.mae_percent)
            for trade in trades
            if trade.mae_percent is not None
        ]
        holding = [
            minutes_between(trade.created_at, trade.updated_at) for trade in trades
        ]
        slippage = [float(trade.slippage_cost or 0.0) for trade in trades]
        captured: list[float] = []
        for trade in trades:
            entry = float(trade.average_price or trade.entry_price or 0.0)
            exit_price = float(trade.exit_price or 0.0)
            mfe_points = float(trade.mfe_points or 0.0)
            if entry <= 0 or exit_price <= 0 or mfe_points <= 0:
                continue
            favorable = (
                exit_price - entry
                if str(trade.side).upper() == "BUY"
                else entry - exit_price
            )
            captured.append((favorable / mfe_points) * 100.0)
        return {
            "trade_count": len(trades),
            "expectancy_after_costs": round(sum(net) / len(net), 4) if net else 0.0,
            "avg_mfe_pct": round(sum(mfe) / len(mfe), 4) if mfe else None,
            "avg_mae_pct": round(sum(mae) / len(mae), 4) if mae else None,
            "avg_captured_mfe_pct": round(sum(captured) / len(captured), 4)
            if captured
            else None,
            "avg_holding_minutes": round(sum(holding) / len(holding), 3)
            if holding
            else None,
            "avg_slippage_cost": round(sum(slippage) / len(slippage), 4)
            if slippage
            else 0.0,
        }

    def _exit_rule_ablation(self, trades: list[TradeRecord]) -> dict[str, Any]:
        optional = [
            "time_exit",
            "trailing_stop",
            "underlying_invalidation",
            "premium_invalidation",
        ]
        variants: dict[str, Any] = {}
        for rule in optional:
            primary = len([trade for trade in trades if self._exit_rule(trade) == rule])
            co_triggered = 0
            for trade in trades:
                try:
                    triggered = json.loads(
                        getattr(trade, "exit_triggered_rules_json", None) or "[]"
                    )
                except json.JSONDecodeError:
                    triggered = []
                if rule in triggered and self._exit_rule(trade) != rule:
                    co_triggered += 1
            variants[f"without_{rule}"] = {
                "status": "insufficient_counterfactual_path_data"
                if primary
                else "no_observed_primary_triggers",
                "primary_trigger_count": primary,
                "co_trigger_count": co_triggered,
                "expectancy_delta": None,
                "reason": "A valid removal estimate requires chronological executable bid/depth replay after the removed rule would have fired.",
            }
        return {
            "baseline": self._exit_reason_summary(trades),
            "variants": variants,
            "causal_claim_allowed": False,
        }
