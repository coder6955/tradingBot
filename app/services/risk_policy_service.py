from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import floor
from typing import Any

from app.config import settings


TIER_1_BASE = "TIER_1_BASE"
TIER_2_STRONG = "TIER_2_STRONG"
TIER_3_HIGH = "TIER_3_HIGH"
TIER_4_EXCEPTIONAL = "TIER_4_EXCEPTIONAL"

RISK_BUDGET_BELOW_MINIMUM_LOT = "RISK_BUDGET_BELOW_MINIMUM_LOT"
RISK_TIER_NOT_VALIDATED = "RISK_TIER_NOT_VALIDATED"
RISK_TIER_DOWNGRADED = "RISK_TIER_DOWNGRADED"
RISK_EXCEEDS_ABSOLUTE_MAXIMUM = "RISK_EXCEEDS_ABSOLUTE_MAXIMUM"
RISK_EXCEEDS_DAILY_CAP = "RISK_EXCEEDS_DAILY_CAP"
RISK_EXCEEDS_OPEN_RISK_CAP = "RISK_EXCEEDS_OPEN_RISK_CAP"
STOP_DISTANCE_INVALID = "STOP_DISTANCE_INVALID"
STOP_EXIT_LIQUIDITY_INADEQUATE = "STOP_EXIT_LIQUIDITY_INADEQUATE"
RISK_POLICY_CONFIGURATION_CONFLICT = "RISK_POLICY_CONFIGURATION_CONFLICT"


@dataclass(frozen=True)
class RiskDecisionContext:
    account_equity: float
    expected_entry: float
    stop_price: float
    lot_size: int
    requested_tier: str = TIER_1_BASE
    order_mode: str = "paper"
    policy_mode: str = "active"
    symbol: str = "BANKNIFTY"
    strategy_version: str = ""
    setup_family: str = "unknown"
    market_regime: str = "unknown"
    session_phase: str = "unknown"
    direction: str = "unknown"
    target_structure: dict[str, Any] = field(default_factory=dict)
    remaining_risk_reward: float = 0.0
    expected_entry_slippage: float = 0.0
    expected_exit_slippage: float = 0.0
    allocated_entry_costs: float = 0.0
    allocated_exit_costs: float = 0.0
    option_spread_pct: float = 0.0
    entry_depth_quantity: int | None = None
    stop_exit_depth_quantity: int | None = None
    contract_dte: int | None = None
    realized_daily_pnl: float = 0.0
    unrealized_daily_pnl: float = 0.0
    current_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    trades_taken_today: int = 0
    open_positions: int = 0
    existing_premium_exposure: float = 0.0
    planned_risk_today: float = 0.0
    total_open_risk: float = 0.0
    banknifty_open_risk: float = 0.0
    correlated_banknifty_positions: int = 0
    setup_quality_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision:
    requested_tier: str
    approved_tier: str | None
    approved_risk_percent: float
    approved_risk_amount: float
    maximum_quantity: int
    risk_per_unit: float
    risk_per_lot: float
    estimated_total_loss_at_stop: float
    downgrade_reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    evidence_version: str | None
    risk_policy_version: str
    policy_mode: str
    active: bool
    paper_only: bool
    shadow_only: bool
    configuration_conflicts: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            not self.rejection_reasons
            and self.maximum_quantity > 0
            and self.approved_tier is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


class RiskPolicyService:
    """Pure, centralized authority for tier approval and stop-risk sizing.

    Scanner scores are deliberately absent. Higher tiers require explicit,
    versioned validation evidence and active-policy permission.
    """

    TIERS = (TIER_1_BASE, TIER_2_STRONG, TIER_3_HIGH, TIER_4_EXCEPTIONAL)
    HARD_ABSOLUTE_MAX_PERCENT = 5.0

    def evaluate(self, context: RiskDecisionContext) -> RiskDecision:
        requested = self._normalize_tier(context.requested_tier)
        conflicts = self.configuration_conflicts()
        fatal_conflicts = [
            reason for reason in conflicts if not reason.startswith("POLICY_CONFLICT_")
        ]
        rejections: list[str] = []
        downgrades: list[str] = []
        if fatal_conflicts:
            rejections.extend([RISK_POLICY_CONFIGURATION_CONFLICT, *fatal_conflicts])
            if any(
                "ABSOLUTE_MAX" in reason or "OUTSIDE_SAFE_RANGE" in reason
                for reason in fatal_conflicts
            ):
                rejections.append(RISK_EXCEEDS_ABSOLUTE_MAXIMUM)

        risk_per_unit = self._risk_per_unit(context)
        risk_per_lot = risk_per_unit * max(0, int(context.lot_size))
        if context.account_equity <= 0:
            rejections.append("ACCOUNT_EQUITY_UNAVAILABLE")
        if context.lot_size <= 0:
            rejections.append("LOT_SIZE_INVALID")
        if risk_per_unit <= 0:
            rejections.append(STOP_DISTANCE_INVALID)
        if (
            context.stop_exit_depth_quantity is not None
            and context.stop_exit_depth_quantity < context.lot_size
        ):
            rejections.append(STOP_EXIT_LIQUIDITY_INADEQUATE)
        if (
            context.entry_depth_quantity is not None
            and context.entry_depth_quantity < context.lot_size
        ):
            rejections.append("ENTRY_LIQUIDITY_INADEQUATE")

        approved = self._highest_allowed_tier(context, requested, downgrades)
        approved_pct = self._tier_percent(approved)
        absolute_cap = min(
            float(settings.absolute_max_risk_per_trade_percent),
            self.HARD_ABSOLUTE_MAX_PERCENT,
        )
        if approved_pct > absolute_cap or approved_pct > float(
            settings.max_risk_per_trade_percent
        ):
            rejections.append(RISK_EXCEEDS_ABSOLUTE_MAXIMUM)

        risk_amount = max(0.0, context.account_equity * approved_pct / 100.0)
        if not self._within_daily_and_open_limits(context, risk_amount, rejections):
            approved = None
            approved_pct = 0.0
            risk_amount = 0.0

        maximum_lots = floor(risk_amount / risk_per_lot) if risk_per_lot > 0 else 0
        maximum_quantity = max(0, maximum_lots * max(0, int(context.lot_size)))
        depth_limits = [
            int(value)
            for value in (
                context.entry_depth_quantity,
                context.stop_exit_depth_quantity,
            )
            if value is not None
        ]
        if depth_limits and context.lot_size > 0:
            depth_quantity = (min(depth_limits) // context.lot_size) * context.lot_size
            maximum_quantity = min(maximum_quantity, depth_quantity)
        estimated_loss = maximum_quantity * risk_per_unit
        if not rejections and maximum_quantity <= 0:
            rejections.append(RISK_BUDGET_BELOW_MINIMUM_LOT)
        if estimated_loss > risk_amount + 0.01:
            rejections.append("ESTIMATED_STOP_LOSS_EXCEEDS_APPROVED_RISK")
            maximum_quantity = 0
            estimated_loss = 0.0

        evidence = context.setup_quality_evidence
        return RiskDecision(
            requested_tier=requested,
            approved_tier=approved,
            approved_risk_percent=round(approved_pct, 4),
            approved_risk_amount=round(risk_amount, 2),
            maximum_quantity=maximum_quantity,
            risk_per_unit=round(risk_per_unit, 4),
            risk_per_lot=round(risk_per_lot, 2),
            estimated_total_loss_at_stop=round(estimated_loss, 2),
            downgrade_reasons=tuple(dict.fromkeys(downgrades)),
            rejection_reasons=tuple(dict.fromkeys(rejections)),
            evidence_version=str(evidence.get("evidence_version"))
            if evidence.get("evidence_version")
            else None,
            risk_policy_version=str(settings.risk_policy_version),
            policy_mode=str(context.policy_mode),
            active=str(context.policy_mode).lower() == "active",
            paper_only=str(context.order_mode).lower() == "paper"
            and str(context.policy_mode).lower() == "active",
            shadow_only=str(context.policy_mode).lower() == "shadow",
            configuration_conflicts=tuple(conflicts),
        )

    def shadow_tier_evaluations(
        self, context: RiskDecisionContext
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for tier in self.TIERS:
            shadow_context = RiskDecisionContext(
                **{**asdict(context), "requested_tier": tier, "policy_mode": "shadow"}
            )
            decision = self.evaluate(shadow_context).to_dict()
            configured_requested_pct = self._tier_percent(tier)
            requested_pct = min(
                configured_requested_pct,
                max(0.0, float(settings.absolute_max_risk_per_trade_percent)),
                max(0.0, float(settings.max_risk_per_trade_percent)),
                self.HARD_ABSOLUTE_MAX_PERCENT,
            )
            risk_amount = max(
                0.0, float(context.account_equity) * requested_pct / 100.0
            )
            risk_per_unit = self._risk_per_unit(context)
            risk_per_lot = risk_per_unit * max(0, int(context.lot_size))
            lots = floor(risk_amount / risk_per_lot) if risk_per_lot > 0 else 0
            quantity = max(0, lots * max(0, int(context.lot_size)))
            depth_limits = [
                int(value)
                for value in (
                    context.entry_depth_quantity,
                    context.stop_exit_depth_quantity,
                )
                if value is not None
            ]
            if depth_limits and context.lot_size > 0:
                quantity = min(
                    quantity, (min(depth_limits) // context.lot_size) * context.lot_size
                )
            decision.update(
                {
                    "counterfactual_only": True,
                    "counterfactual_configured_requested_risk_percent": configured_requested_pct,
                    "counterfactual_requested_risk_percent": requested_pct,
                    "counterfactual_risk_amount": round(risk_amount, 2),
                    "counterfactual_maximum_quantity": quantity,
                    "counterfactual_estimated_loss_at_stop": round(
                        quantity * risk_per_unit, 2
                    ),
                    "counterfactual_can_reach_order_router": False,
                }
            )
            results[tier] = decision
        return results

    def configuration_conflicts(self) -> list[str]:
        reasons: list[str] = []
        absolute = float(settings.absolute_max_risk_per_trade_percent)
        if absolute <= 0 or absolute > self.HARD_ABSOLUTE_MAX_PERCENT:
            reasons.append(
                "ABSOLUTE_MAX_RISK_PER_TRADE_PERCENT_MUST_BE_BETWEEN_0_AND_5"
            )
        if float(settings.max_risk_per_trade_percent) > min(
            absolute, self.HARD_ABSOLUTE_MAX_PERCENT
        ):
            reasons.append("MAX_RISK_PER_TRADE_PERCENT_EXCEEDS_ABSOLUTE_MAXIMUM")
        for tier in self.TIERS:
            value = self._tier_percent(tier)
            if value <= 0 or value > self.HARD_ABSOLUTE_MAX_PERCENT:
                reasons.append(f"{tier}_PERCENT_OUTSIDE_SAFE_RANGE")
        if any(
            self._tier_percent(self.TIERS[index])
            > self._tier_percent(self.TIERS[index + 1])
            for index in range(3)
        ):
            reasons.append("RISK_TIER_PERCENTAGES_NOT_MONOTONIC")
        if float(settings.max_risk_per_trade_percent) > float(
            settings.max_realized_daily_loss_percent
        ):
            reasons.append(
                "POLICY_CONFLICT_PER_TRADE_MAX_EXCEEDS_REALIZED_DAILY_LOSS_CAP"
            )
        if float(settings.max_risk_per_trade_percent) > float(
            settings.max_daily_planned_risk_percent
        ):
            reasons.append(
                "POLICY_CONFLICT_PER_TRADE_MAX_EXCEEDS_DAILY_PLANNED_RISK_CAP"
            )
        return reasons

    def _highest_allowed_tier(
        self, context: RiskDecisionContext, requested: str, downgrades: list[str]
    ) -> str:
        requested_index = self.TIERS.index(requested)
        policy_cap = self._normalize_tier(
            settings.shadow_max_risk_tier
            if str(context.policy_mode).lower() == "shadow"
            else settings.active_live_max_risk_tier
            if str(context.order_mode).lower() == "live"
            else settings.active_paper_max_risk_tier
        )
        if (
            str(context.policy_mode).lower() == "active"
            and not settings.enable_validated_higher_risk_active
        ):
            policy_cap = TIER_1_BASE
        if (
            str(context.policy_mode).lower() == "active"
            and str(context.order_mode).lower() == "live"
            and policy_cap == TIER_4_EXCEPTIONAL
            and not settings.enable_exceptional_live_risk
        ):
            policy_cap = TIER_3_HIGH
        allowed_index = min(requested_index, self.TIERS.index(policy_cap))
        if allowed_index < requested_index:
            downgrades.extend([RISK_TIER_DOWNGRADED, "RISK_POLICY_MODE_TIER_CAP"])

        defensive_cap = self._defensive_tier_cap(context)
        if self.TIERS.index(defensive_cap) < allowed_index:
            allowed_index = self.TIERS.index(defensive_cap)
            downgrades.extend([RISK_TIER_DOWNGRADED, "ACCOUNT_DEFENSIVE_STATE"])

        while allowed_index > 0 and not self._evidence_allows(
            self.TIERS[allowed_index], context.setup_quality_evidence, context
        ):
            allowed_index -= 1
            downgrades.extend([RISK_TIER_NOT_VALIDATED, RISK_TIER_DOWNGRADED])
        return self.TIERS[allowed_index]

    def _defensive_tier_cap(self, context: RiskDecisionContext) -> str:
        if context.consecutive_losses >= int(settings.max_consecutive_losses):
            return TIER_1_BASE
        if context.consecutive_losses >= 1:
            return TIER_1_BASE
        if context.current_drawdown_pct >= float(
            settings.risk_reduction_after_drawdown_percent
        ):
            return TIER_1_BASE
        unrealized_loss_percent = (
            max(0.0, -float(context.unrealized_daily_pnl))
            / max(float(context.account_equity), 0.01)
            * 100.0
        )
        if unrealized_loss_percent >= float(
            settings.risk_reduction_after_drawdown_percent
        ):
            return TIER_1_BASE
        return TIER_4_EXCEPTIONAL

    def _evidence_allows(
        self, tier: str, evidence: dict[str, Any], context: RiskDecisionContext
    ) -> bool:
        if tier == TIER_1_BASE:
            return True
        if (
            not evidence.get("validated")
            or float(evidence.get("after_cost_expectancy_pct") or 0.0) <= 0
        ):
            return False
        if (
            str(evidence.get("validation_source") or "")
            != "strategy_validation_repository"
        ):
            return False
        if not evidence.get("independent_chronological_oos"):
            return False
        if context.strategy_version and str(
            evidence.get("strategy_version") or ""
        ) != str(context.strategy_version):
            return False
        if context.setup_family != "unknown" and str(
            evidence.get("setup_family") or ""
        ) != str(context.setup_family):
            return False
        index = self.TIERS.index(tier) + 1
        trades = int(evidence.get("out_of_sample_trades") or 0)
        sessions = int(evidence.get("out_of_sample_sessions") or 0)
        folds = int(evidence.get("fold_count") or 0)
        profit_factor = float(evidence.get("profit_factor") or 0.0)
        drawdown = float(evidence.get("max_drawdown_pct") or 100.0)
        requirements = {
            2: (
                settings.risk_tier_2_min_oos_trades,
                settings.risk_tier_2_min_sessions,
                settings.risk_tier_2_min_folds,
                settings.risk_tier_2_min_profit_factor,
                settings.risk_tier_2_max_drawdown_pct,
            ),
            3: (
                settings.risk_tier_3_min_oos_trades,
                settings.risk_tier_3_min_sessions,
                settings.risk_tier_3_min_folds,
                settings.risk_tier_3_min_profit_factor,
                settings.risk_tier_3_max_drawdown_pct,
            ),
            4: (
                settings.risk_tier_4_min_oos_trades,
                settings.risk_tier_4_min_sessions,
                settings.risk_tier_4_min_folds,
                settings.risk_tier_4_min_profit_factor,
                settings.risk_tier_4_max_drawdown_pct,
            ),
        }
        min_trades, min_sessions, min_folds, min_pf, max_dd = requirements[index]
        if (
            trades < min_trades
            or sessions < min_sessions
            or folds < min_folds
            or profit_factor < min_pf
            or drawdown > max_dd
        ):
            return False
        if index >= 3 and not evidence.get("stable_across_folds"):
            return False
        if index >= 3 and not evidence.get("acceptable_mae_and_loss_streak"):
            return False
        if index == 4:
            permitted = {
                str(value) for value in evidence.get("policy_permitted_tiers", [])
            }
            return bool(
                evidence.get("stable_across_regimes")
                and evidence.get("current_distribution_match")
                and not evidence.get("unvalidated_event_or_recovery")
                and TIER_4_EXCEPTIONAL in permitted
            )
        return True

    def _within_daily_and_open_limits(
        self, context: RiskDecisionContext, risk_amount: float, rejections: list[str]
    ) -> bool:
        equity = max(context.account_equity, 0.01)
        planned_limit = equity * float(settings.max_daily_planned_risk_percent) / 100.0
        realized_limit = (
            equity * float(settings.max_realized_daily_loss_percent) / 100.0
        )
        open_limit = equity * float(settings.max_total_open_risk_percent) / 100.0
        bank_limit = equity * float(settings.max_banknifty_open_risk_percent) / 100.0
        realized_loss = max(0.0, -float(context.realized_daily_pnl))
        if context.planned_risk_today + risk_amount > planned_limit + 0.01:
            rejections.append(RISK_EXCEEDS_DAILY_CAP)
        if (
            realized_loss >= realized_limit
            or realized_loss + risk_amount > realized_limit + 0.01
        ):
            rejections.append(RISK_EXCEEDS_DAILY_CAP)
        if context.total_open_risk + risk_amount > open_limit + 0.01:
            rejections.append(RISK_EXCEEDS_OPEN_RISK_CAP)
        if context.banknifty_open_risk + risk_amount > bank_limit + 0.01:
            rejections.append(RISK_EXCEEDS_OPEN_RISK_CAP)
        if context.consecutive_losses >= int(settings.max_consecutive_losses):
            rejections.append("MAX_CONSECUTIVE_LOSSES_REACHED")
        return not rejections

    def _risk_per_unit(self, context: RiskDecisionContext) -> float:
        entry_slippage = (
            context.expected_entry_slippage
            or context.expected_entry
            * float(settings.risk_expected_entry_slippage_pct)
            / 100.0
        )
        exit_slippage = (
            context.expected_exit_slippage
            or context.expected_entry
            * float(settings.risk_expected_exit_slippage_pct)
            / 100.0
        )
        entry_costs = (
            context.allocated_entry_costs
            or context.expected_entry
            * float(settings.risk_allocated_entry_cost_pct)
            / 100.0
        )
        exit_costs = (
            context.allocated_exit_costs
            or context.expected_entry
            * float(settings.risk_allocated_exit_cost_pct)
            / 100.0
        )
        entry_cost_per_unit = context.expected_entry + entry_slippage + entry_costs
        stop_exit_value_per_unit = context.stop_price - exit_slippage - exit_costs
        return entry_cost_per_unit - stop_exit_value_per_unit

    def _tier_percent(self, tier: str | None) -> float:
        values = {
            TIER_1_BASE: float(settings.risk_tier_1_base_pct),
            TIER_2_STRONG: float(settings.risk_tier_2_strong_pct),
            TIER_3_HIGH: float(settings.risk_tier_3_high_pct),
            TIER_4_EXCEPTIONAL: float(settings.risk_tier_4_exceptional_pct),
        }
        return values.get(str(tier), 0.0)

    def _normalize_tier(self, value: str) -> str:
        normalized = str(value or TIER_1_BASE).upper()
        return normalized if normalized in self.TIERS else TIER_1_BASE
