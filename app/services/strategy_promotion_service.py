from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.evidence_matrix_service import EvidenceMatrixService
from app.services.strategy_validation_repository import StrategyValidationRepository


class StrategyPromotionService:
    """Evaluate promotion evidence without changing runtime strategy behavior."""

    def __init__(self, evidence: EvidenceMatrixService | None = None, validations: StrategyValidationRepository | None = None) -> None:
        self.evidence = evidence or EvidenceMatrixService()
        self.validations = validations or StrategyValidationRepository()

    def evaluate(self, *, timeframe: str = "5minute") -> dict[str, Any]:
        matrix = self.evidence.report(group_by=["setup_family", "market_regime", "direction"])
        records = [row for row in self.validations.recent(limit=100) if row.strategy_version == settings.strategy_version]
        current = [row for row in records if row.symbol == "BANKNIFTY" and row.timeframe == timeframe and row.mode == "walk_forward_option"]
        checks: list[dict[str, Any]] = []
        for direction in ("CALL", "PUT"):
            row = next((item for item in current if item.direction == direction), None)
            threshold_passed = bool(
                row
                and row.passed
                and int(row.trades) >= settings.evidence_matrix_min_trades
                and float(row.expectancy_pct) >= settings.evidence_matrix_min_expectancy_pct
                and row.profit_factor is not None
                and float(row.profit_factor) >= settings.evidence_matrix_min_profit_factor
                and float(row.max_drawdown_pct) <= settings.evidence_matrix_max_drawdown_pct
            )
            checks.append(
                {
                    "direction": direction,
                    "present": row is not None,
                    "passed": threshold_passed,
                    "trades": int(row.trades) if row else 0,
                    "expectancy_pct": float(row.expectancy_pct) if row else None,
                    "profit_factor": float(row.profit_factor) if row and row.profit_factor is not None else None,
                    "max_drawdown_pct": float(row.max_drawdown_pct) if row else None,
                    "fold_count": int(row.fold_count) if row else 0,
                    "out_of_sample_sessions": int(row.out_of_sample_sessions) if row else 0,
                    "thresholds": {
                        "minimum_trades": settings.evidence_matrix_min_trades,
                        "minimum_expectancy_pct": settings.evidence_matrix_min_expectancy_pct,
                        "minimum_profit_factor": settings.evidence_matrix_min_profit_factor,
                        "maximum_drawdown_pct": settings.evidence_matrix_max_drawdown_pct,
                    },
                }
            )
        sufficiently_segmented = any(bool(row.get("evidence_sufficient")) for row in matrix["groups"])
        eligible = all(item["passed"] for item in checks) and sufficiently_segmented
        reasons: list[str] = []
        if not all(item["present"] for item in checks):
            reasons.append("walk_forward_validation_missing_for_call_or_put")
        if any(item["present"] and not item["passed"] for item in checks):
            reasons.append("walk_forward_validation_failed")
        if not sufficiently_segmented:
            reasons.append("setup_regime_evidence_cells_below_minimum_sample")
        return {
            "strategy_version": settings.strategy_version,
            "promotion_eligible": eligible,
            "automatic_promotion": False,
            "automatic_live_mutation": False,
            "manual_registration_required": True,
            "checks": checks,
            "evidence_matrix_summary": {
                "groups": len(matrix["groups"]),
                "accepted_outcomes": matrix["accepted_outcomes"],
                "sufficient_groups": len([row for row in matrix["groups"] if row.get("evidence_sufficient")]),
            },
            "reasons": reasons,
            "policy": "Research can propose a version; it cannot modify live settings, thresholds, or strategy lineage.",
        }
