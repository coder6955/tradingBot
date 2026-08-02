from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.config import settings
from app.services.database import RejectedOpportunityRecord, TradeRecord, get_session


class EvidenceMatrixService:
    """Build a lineage-safe evidence matrix from persisted decisions and outcomes."""

    DIMENSIONS = (
        "setup_family",
        "market_regime",
        "time_bucket",
        "dte_bucket",
        "direction",
        "volatility",
        "execution",
        "participation",
    )

    def report(
        self, *, group_by: list[str] | None = None, limit: int = 5000
    ) -> dict[str, Any]:
        dimensions = [
            item
            for item in (group_by or list(self.DIMENSIONS))
            if item in self.DIMENSIONS
        ]
        if not dimensions:
            dimensions = ["setup_family", "market_regime"]
        accepted = self._accepted(limit)
        rejected = self._rejected(limit)
        groups: dict[tuple[str, ...], dict[str, Any]] = defaultdict(
            lambda: {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "net_pnl": 0.0,
                "rejections": 0,
                "pnl_series": [],
            }
        )
        for row in accepted:
            key = tuple(str(row.get(item) or "unknown") for item in dimensions)
            group = groups[key]
            pnl = float(row.get("pnl") or 0.0)
            group["trades"] += 1
            group["wins"] += int(pnl > 0)
            group["losses"] += int(pnl < 0)
            group["gross_profit"] += max(0.0, pnl)
            group["gross_loss"] += abs(min(0.0, pnl))
            group["net_pnl"] += pnl
            group["pnl_series"].append(pnl)
        for row in rejected:
            key = tuple(str(row.get(item) or "unknown") for item in dimensions)
            groups[key]["rejections"] += 1

        rows: list[dict[str, Any]] = []
        for key, group in groups.items():
            trades = int(group["trades"])
            profit_factor = (
                group["gross_profit"] / group["gross_loss"]
                if group["gross_loss"] > 0
                else None
            )
            row = {dimension: value for dimension, value in zip(dimensions, key)}
            row.update(
                {
                    "trades": trades,
                    "rejections": int(group["rejections"]),
                    "wins": int(group["wins"]),
                    "losses": int(group["losses"]),
                    "win_rate_pct": round(group["wins"] / trades * 100, 2)
                    if trades
                    else None,
                    "net_pnl": round(group["net_pnl"], 2),
                    "expectancy_per_trade": round(group["net_pnl"] / trades, 2)
                    if trades
                    else None,
                    "profit_factor": round(profit_factor, 3)
                    if profit_factor is not None
                    else None,
                    "max_drawdown": round(self._max_drawdown(group["pnl_series"]), 2),
                    "evidence_sufficient": trades
                    >= settings.evidence_matrix_min_trades,
                }
            )
            rows.append(row)
        rows.sort(
            key=lambda item: (int(item["trades"]), int(item["rejections"])),
            reverse=True,
        )
        return {
            "strategy_version": settings.strategy_version,
            "dimensions": dimensions,
            "groups": rows,
            "accepted_outcomes": len(accepted),
            "rejected_observations": len(rejected),
            "promotion_thresholds": {
                "minimum_trades": settings.evidence_matrix_min_trades,
                "minimum_expectancy_pct": settings.evidence_matrix_min_expectancy_pct,
                "minimum_profit_factor": settings.evidence_matrix_min_profit_factor,
                "maximum_drawdown_pct": settings.evidence_matrix_max_drawdown_pct,
            },
            "warning": "Rejected observations are not trades and are excluded from expectancy. Evidence is split by the active strategy version.",
        }

    def _accepted(self, limit: int) -> list[dict[str, Any]]:
        session = get_session()
        try:
            rows = (
                session.query(TradeRecord)
                .filter(
                    TradeRecord.strategy_version == settings.strategy_version,
                    TradeRecord.status == "closed",
                )
                .order_by(TradeRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                self._features(
                    row.created_at,
                    row.action,
                    row.order_response_json,
                    pnl=row.net_pnl if row.net_pnl is not None else row.pnl,
                )
                for row in rows
            ]
        finally:
            session.close()

    def _rejected(self, limit: int) -> list[dict[str, Any]]:
        session = get_session()
        try:
            rows = (
                session.query(RejectedOpportunityRecord)
                .filter(
                    RejectedOpportunityRecord.strategy_version
                    == settings.strategy_version
                )
                .order_by(RejectedOpportunityRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                self._features(
                    row.created_at,
                    row.action,
                    row.factor_scores_json,
                    direct_factors=True,
                )
                for row in rows
            ]
        finally:
            session.close()

    def _features(
        self,
        created_at: datetime,
        action: str | None,
        raw: Any,
        *,
        pnl: float | None = None,
        direct_factors: bool = False,
    ) -> dict[str, Any]:
        try:
            payload = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        factors = (
            payload
            if direct_factors
            else payload.get("signal_factor_scores", {})
            if isinstance(payload, dict)
            else {}
        )
        factors = factors if isinstance(factors, dict) else {}
        family = self._dict(factors.get("setup_family"))
        regime = self._dict(factors.get("market_regime"))
        volatility = self._dict(factors.get("volatility_edge"))
        premium = self._dict(
            self._dict(factors.get("option_premium_confirmation")).get("details")
        )
        contract = self._dict(factors.get("contract"))
        quality = self._dict(self._dict(factors.get("option_quality")).get("details"))
        dte = self._dte(contract.get("expiry"), created_at)
        spread = self._float(quality.get("spread_pct") or premium.get("spread_pct"))
        participation = (
            "confirmed"
            if premium.get("volume_expansion") or premium.get("participation_confirmed")
            else "unconfirmed"
        )
        return {
            "setup_family": family.get("name")
            or factors.get("setup_family_name")
            or "unknown",
            "market_regime": regime.get("regime") or "unknown",
            "time_bucket": self._time_bucket(created_at),
            "dte_bucket": "expiry"
            if dte is not None and dte <= 0
            else "1_3_dte"
            if dte is not None and dte <= 3
            else "4_7_dte"
            if dte is not None and dte <= 7
            else "8plus_dte"
            if dte is not None
            else "unknown",
            "direction": "CALL"
            if str(action or "").upper().endswith("CE")
            else "PUT"
            if str(action or "").upper().endswith("PE")
            else "unknown",
            "volatility": volatility.get("classification") or "unknown",
            "execution": "tight"
            if 0 < spread <= 1.5
            else "normal"
            if 0 < spread <= 3
            else "wide"
            if spread > 0
            else "unknown",
            "participation": participation,
            "pnl": pnl,
        }

    def _max_drawdown(self, pnls: list[float]) -> float:
        equity = peak = drawdown = 0.0
        for pnl in reversed(pnls):
            equity += pnl
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        return drawdown

    def _time_bucket(self, value: datetime) -> str:
        minute = value.hour * 60 + value.minute
        return (
            "open"
            if minute < 10 * 60
            else "morning"
            if minute < 12 * 60
            else "midday"
            if minute < 14 * 60
            else "late"
        )

    def _dte(self, expiry: Any, created_at: datetime) -> int | None:
        try:
            return (datetime.fromisoformat(str(expiry)).date() - created_at.date()).days
        except (TypeError, ValueError):
            return None

    def _dict(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
