from __future__ import annotations

import json
from typing import Any

from app.services.database import TradeRecord, get_session
from app.services.trading_metrics import minutes_between, summarize_groups, summarize_returns, time_bucket


class ExecutionAnalyticsService:
    """Measure paper/live execution quality separately from signal quality."""

    def analyze(self, *, symbol: str | None = "BANKNIFTY", limit: int = 1000) -> dict[str, Any]:
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
            "lineage_warning": "Execution evidence is split by strategy/config; do not combine hashes for readiness." if len(lineage_groups) > 1 else None,
            "overall_by_lineage": {key: self._summary(items) for key, items in lineage_groups.items()},
            "overall": self._summary(closed),
            "segments": {
                "mode": self._groups(closed, lambda trade: str(trade.mode or "unknown")),
                "ce_vs_pe": self._groups(closed, self._ce_pe),
                "time_bucket": self._groups(closed, lambda trade: time_bucket(trade.created_at)),
                "outcome": self._groups(closed, lambda trade: str(trade.outcome or "unknown")),
            },
            "execution_quality": self._execution_quality(trades),
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
            pnl_fn=lambda trade: float(trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0),
            outcome_fn=lambda trade: trade.outcome,
            time_in_trade_fn=lambda trade: minutes_between(trade.created_at, trade.updated_at),
        )

    def _groups(self, trades: list[TradeRecord], key_fn: Any) -> dict[str, Any]:
        return summarize_groups(
            trades,
            key_fn,
            pnl_fn=lambda trade: float(trade.net_pnl if trade.net_pnl is not None else trade.pnl or 0.0),
            outcome_fn=lambda trade: trade.outcome,
            time_in_trade_fn=lambda trade: minutes_between(trade.created_at, trade.updated_at),
        )

    def _execution_quality(self, trades: list[TradeRecord]) -> dict[str, Any]:
        live = [trade for trade in trades if trade.mode == "live"]
        filled = [trade for trade in live if int(trade.filled_quantity or 0) > 0 or str(trade.status).lower() in {"filled", "complete"}]
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
            "live_fill_rate_pct": round((len(filled) / len(live)) * 100, 2) if live else 0.0,
            "avg_requested_fill_ratio_pct": round((sum(fill_ratios) / len(fill_ratios)) * 100, 2) if fill_ratios else 0.0,
            "avg_entry_deviation_pct": round(sum(deviations) / len(deviations), 3) if deviations else 0.0,
            "max_entry_deviation_pct": round(max([abs(value) for value in deviations]), 3) if deviations else 0.0,
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
