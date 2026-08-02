from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from app.config import settings


MISSED_REASON_CODES = {
    "NO_PREPARED_CANDIDATE",
    "FIVE_MINUTE_OPPOSITION",
    "FIVE_MINUTE_NOT_YET_DIRECTIONAL",
    "ONE_MINUTE_FIVE_MINUTE_DISAGREEMENT",
    "PREMIUM_CONFIRMATION_DELAY",
    "OPTION_DID_NOT_TRANSMIT",
    "FAST_PATH_CONTEXT_STALE",
    "FAST_PATH_DOUBLE_CONFIRMATION_DELAY",
    "CHASE_BECAME_TOO_HIGH",
    "REWARD_RISK_DETERIORATED",
    "TARGET_ROOM_DETERIORATED",
    "SPREAD_DIVERGED",
    "DEPTH_DISAPPEARED",
    "DATA_STALE_OR_GAPPED",
    "RISK_INFEASIBLE",
    "CORRECTLY_REJECTED",
    "UNDETERMINABLE_DATA",
}


class PolicyTimingWaterfallService:
    STAGES = (
        "first_observable_directional_evidence",
        "first_eligible_preparation",
        "underlying_trigger_crossed",
        "option_first_responded",
        "premium_confirmation_passed",
        "fast_path_validation_passed",
        "armed_registration",
        "armed_confirmation_passed",
        "policy_became_enterable",
        "first_executable_ask",
        "order_submission",
    )

    def analyze(self, observations: dict[str, dict[str, Any]]) -> dict[str, Any]:
        stages: list[dict[str, Any]] = []
        prior_time: datetime | None = None
        prior_ask: float | None = None
        first_time: datetime | None = None
        first_ask: float | None = None
        for stage in self.STAGES:
            observation = observations.get(stage) or {}
            timestamp = self._optional_time(observation.get("timestamp"))
            ask = self._optional_float(observation.get("executable_ask"))
            if timestamp is None:
                stages.append({"stage": stage, "timestamp": None, "executable_ask": ask})
                continue
            first_time = first_time or timestamp
            first_ask = first_ask if first_ask is not None else ask
            stages.append(
                {
                    "stage": stage,
                    "timestamp": timestamp.isoformat(),
                    "executable_ask": ask,
                    "delay_from_prior_ms": (
                        round((timestamp - prior_time).total_seconds() * 1000.0, 3)
                        if prior_time is not None
                        else 0.0
                    ),
                    "delay_from_first_ms": round((timestamp - first_time).total_seconds() * 1000.0, 3),
                    "ask_increase_from_prior": round(ask - prior_ask, 4) if ask is not None and prior_ask is not None else None,
                    "ask_increase_from_first": round(ask - first_ask, 4) if ask is not None and first_ask is not None else None,
                }
            )
            prior_time = timestamp
            if ask is not None:
                prior_ask = ask
        return {
            "stages": stages,
            "complete": all((observations.get(stage) or {}).get("timestamp") for stage in self.STAGES),
            "uses_executable_ask": True,
        }

    def _optional_time(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    def _optional_float(self, value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


class MissedOpportunityClassifier:
    def classify(self, episode: dict[str, Any]) -> str:
        outcome = episode.get("outcome") if isinstance(episode.get("outcome"), dict) else {}
        gates = episode.get("gates") if isinstance(episode.get("gates"), dict) else {}
        if not outcome.get("executable_data_sufficient"):
            return "UNDETERMINABLE_DATA"
        if gates.get("risk_feasible") is False:
            return "RISK_INFEASIBLE"
        if gates.get("data_fresh") is False or gates.get("feed_gap"):
            return "DATA_STALE_OR_GAPPED"
        if gates.get("depth_safe") is False:
            return "DEPTH_DISAPPEARED"
        if gates.get("spread_safe") is False:
            return "SPREAD_DIVERGED"
        if gates.get("target_room") is False:
            return "TARGET_ROOM_DETERIORATED"
        if gates.get("remaining_rr") is False:
            return "REWARD_RISK_DETERIORATED"
        if gates.get("chase") is False:
            return "CHASE_BECAME_TOO_HIGH"
        if gates.get("fast_context_fresh") is False:
            return "FAST_PATH_CONTEXT_STALE"
        if gates.get("second_confirmation_delayed"):
            return "FAST_PATH_DOUBLE_CONFIRMATION_DELAY"
        if gates.get("option_transmitted") is False:
            return "OPTION_DID_NOT_TRANSMIT"
        if gates.get("premium_confirmation") is False:
            return "PREMIUM_CONFIRMATION_DELAY"
        if gates.get("one_five_agreement") is False:
            return "ONE_MINUTE_FIVE_MINUTE_DISAGREEMENT"
        if gates.get("five_minute_opposed"):
            return "FIVE_MINUTE_OPPOSITION"
        if gates.get("five_minute_directional") is False:
            return "FIVE_MINUTE_NOT_YET_DIRECTIONAL"
        if gates.get("prepared") is False:
            return "NO_PREPARED_CANDIDATE"
        favourable = bool(outcome.get("target_before_stop") or float(outcome.get("after_cost_result") or 0.0) > 0)
        return "NO_PREPARED_CANDIDATE" if favourable and not episode.get("entered") else "CORRECTLY_REJECTED"


class PolicyEvaluationService:
    """Unique-episode, after-cost policy comparison and chronological validation."""

    SEGMENT_FIELDS = (
        "session_phase",
        "regime",
        "setup_family",
        "direction",
        "dte",
        "expiry_day",
        "premium_band",
        "spread_band",
        "liquidity_band",
    )

    def __init__(self) -> None:
        self.classifier = MissedOpportunityClassifier()

    def evaluate(self, episodes: list[dict[str, Any]], *, dataset_label: str) -> dict[str, Any]:
        label = str(dataset_label).upper()
        if label not in {"IN-SAMPLE", "VALIDATION", "OUT-OF-SAMPLE", "INSUFFICIENT DATA"}:
            raise ValueError("dataset label must disclose the evaluation role")
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for episode in episodes:
            key = (str(episode.get("episode_key") or ""), str(episode.get("policy_version") or "unknown"))
            unique.setdefault(key, episode)
        rows = list(unique.values())
        by_policy: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            row = dict(row)
            row["missed_reason"] = self.classifier.classify(row)
            by_policy[str(row.get("policy_version") or "unknown")].append(row)
        reports = {policy: self._metrics(policy_rows, label) for policy, policy_rows in by_policy.items()}
        return {
            "dataset_label": label,
            "unique_episode_policy_pairs": len(rows),
            "policies": reports,
            "no_scanner_row_independence_assumption": True,
        }

    def chronological_folds(
        self,
        episodes: list[dict[str, Any]],
        *,
        maximum_horizon_seconds: int,
        train_fraction: float = 0.60,
        validation_fraction: float = 0.20,
    ) -> dict[str, list[dict[str, Any]]]:
        ordered = sorted(episodes, key=lambda item: self._time(item.get("decision_at")))
        if len(ordered) < 3:
            return {"training": ordered, "validation": [], "out_of_sample": []}
        train_end_index = max(1, min(len(ordered) - 2, int(len(ordered) * train_fraction)))
        validation_end_index = max(train_end_index + 1, min(len(ordered) - 1, int(len(ordered) * (train_fraction + validation_fraction))))
        train_boundary = self._time(ordered[train_end_index]["decision_at"])
        validation_boundary = self._time(ordered[validation_end_index]["decision_at"])
        embargo = timedelta(seconds=max(0, int(maximum_horizon_seconds)))
        return {
            "training": [item for item in ordered if self._time(item["decision_at"]) < train_boundary - embargo],
            "validation": [
                item
                for item in ordered
                if train_boundary + embargo <= self._time(item["decision_at"]) < validation_boundary - embargo
            ],
            "out_of_sample": [item for item in ordered if self._time(item["decision_at"]) >= validation_boundary + embargo],
        }

    def evaluate_chronological(self, episodes: list[dict[str, Any]], *, maximum_horizon_seconds: int) -> dict[str, Any]:
        folds = self.chronological_folds(episodes, maximum_horizon_seconds=maximum_horizon_seconds)
        reports = {
            "training": self.evaluate(folds["training"], dataset_label="IN-SAMPLE"),
            "validation": self.evaluate(folds["validation"], dataset_label="VALIDATION"),
            "out_of_sample": self.evaluate(
                folds["out_of_sample"],
                dataset_label="OUT-OF-SAMPLE" if folds["out_of_sample"] else "INSUFFICIENT DATA",
            ),
        }
        return {
            "purge_and_embargo_seconds": int(maximum_horizon_seconds),
            "random_shuffle": False,
            "folds": reports,
        }

    def _metrics(self, episodes: list[dict[str, Any]], label: str) -> dict[str, Any]:
        entries = [item for item in episodes if item.get("entered")]
        determined = [item for item in entries if self._determined(item)]
        returns = [float(item.get("outcome", {}).get("after_cost_result") or 0.0) for item in determined]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        mfe = [float(item.get("outcome", {}).get("net_mfe_per_unit")) for item in determined if item.get("outcome", {}).get("net_mfe_per_unit") is not None]
        mae = [float(item.get("outcome", {}).get("net_mae_per_unit")) for item in determined if item.get("outcome", {}).get("net_mae_per_unit") is not None]
        latencies = [float(item.get("trigger_to_entry_ms")) for item in entries if item.get("trigger_to_entry_ms") is not None]
        signal_latencies = [float(item.get("signal_to_entry_ms")) for item in entries if item.get("signal_to_entry_ms") is not None]
        spreads = [float(item.get("spread_paid") or 0.0) for item in entries if item.get("spread_paid") is not None]
        slippage = [float(item.get("slippage") or 0.0) for item in entries if item.get("slippage") is not None]
        equity_curve: list[float] = []
        total = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in returns:
            total += value
            peak = max(peak, total)
            max_drawdown = max(max_drawdown, peak - total)
            equity_curve.append(total)
        missed = [item for item in episodes if not item.get("entered") and item.get("missed_reason") not in {"CORRECTLY_REJECTED", "UNDETERMINABLE_DATA"}]
        correct = [item for item in episodes if not item.get("entered") and item.get("missed_reason") == "CORRECTLY_REJECTED"]
        undetermined = [item for item in episodes if not self._determined(item)]
        metrics = {
            "dataset_label": label,
            "unique_episodes": len({str(item.get("episode_key")) for item in episodes}),
            "entries": len(entries),
            "rejections": len(episodes) - len(entries),
            "entry_rate_percent": self._percent(len(entries), len(episodes)),
            "determined_entries": len(determined),
            "undetermined_outcomes": len(undetermined),
            "after_cost_expectancy": statistics.mean(returns) if returns else None,
            "median_after_cost_return": statistics.median(returns) if returns else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "win_rate_percent": self._percent(len(wins), len(returns)),
            "average_win": statistics.mean(wins) if wins else None,
            "average_loss": statistics.mean(losses) if losses else None,
            "payoff_ratio": statistics.mean(wins) / abs(statistics.mean(losses)) if wins and losses else None,
            "maximum_drawdown": max_drawdown,
            "average_mae": statistics.mean(mae) if mae else None,
            "worst_mae": min(mae) if mae else None,
            "average_mfe": statistics.mean(mfe) if mfe else None,
            "median_mfe": statistics.median(mfe) if mfe else None,
            "stop_before_target_rate_percent": self._percent(sum(1 for item in determined if item.get("outcome", {}).get("stop_before_target")), len(determined)),
            "target_before_stop_rate_percent": self._percent(sum(1 for item in determined if item.get("outcome", {}).get("target_before_stop")), len(determined)),
            "false_breakout_rate_percent": self._percent(sum(1 for item in determined if item.get("false_breakout")), len(determined)),
            "missed_valid_opportunity_rate_percent": self._percent(len(missed), len(episodes)),
            "correct_rejection_rate_percent": self._percent(len(correct), len(episodes)),
            "median_trigger_to_entry_ms": statistics.median(latencies) if latencies else None,
            "median_signal_to_entry_ms": statistics.median(signal_latencies) if signal_latencies else None,
            "median_move_captured_percent": self._median_field(entries, "move_captured_percent"),
            "average_spread_paid": statistics.mean(spreads) if spreads else None,
            "average_slippage": statistics.mean(slippage) if slippage else None,
            "data_coverage_rate_percent": self._percent(len(episodes) - len(undetermined), len(episodes)),
            "undeterminable_outcome_rate_percent": self._percent(len(undetermined), len(episodes)),
            "confidence_interval_95_expectancy": self._confidence_interval(returns),
            "missed_reason_counts": self._counts(item.get("missed_reason") for item in episodes),
            "performance_concentration": self._performance_concentration(determined, returns),
        }
        metrics["segments"] = self._segments(episodes, label)
        return metrics

    def _performance_concentration(self, episodes: list[dict[str, Any]], returns: list[float]) -> dict[str, Any]:
        positive_total = sum(value for value in returns if value > 0)
        largest = sorted((value for value in returns if value > 0), reverse=True)[:5]
        by_day: dict[str, float] = defaultdict(float)
        for episode, value in zip(episodes, returns):
            by_day[str(episode.get("trading_date") or str(episode.get("decision_at") or "")[:10])] += value
        best_day = max(by_day.values()) if by_day else 0.0
        positive_days = sum(value for value in by_day.values() if value > 0)
        return {
            "top_five_wins_percent_of_positive_pnl": round(sum(largest) / positive_total * 100.0, 4) if positive_total else None,
            "best_day_percent_of_positive_day_pnl": round(best_day / positive_days * 100.0, 4) if positive_days else None,
            "session_count": len(by_day),
        }

    def _segments(self, episodes: list[dict[str, Any]], label: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field in self.SEGMENT_FIELDS:
            buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for episode in episodes:
                buckets[str(episode.get(field, "unknown"))].append(episode)
            result[field] = {
                name: {
                    "dataset_label": label,
                    "unique_episodes": len({str(item.get("episode_key")) for item in rows}),
                    "entries": sum(1 for item in rows if item.get("entered")),
                    "determined": sum(1 for item in rows if self._determined(item)),
                    "after_cost_expectancy": self._mean_outcome(rows),
                }
                for name, rows in buckets.items()
            }
        return result

    def _determined(self, episode: dict[str, Any]) -> bool:
        return bool((episode.get("outcome") or {}).get("executable_data_sufficient"))

    def _mean_outcome(self, episodes: list[dict[str, Any]]) -> float | None:
        values = [float(item.get("outcome", {}).get("after_cost_result") or 0.0) for item in episodes if self._determined(item)]
        return statistics.mean(values) if values else None

    def _median_field(self, episodes: list[dict[str, Any]], field: str) -> float | None:
        values = [float(item[field]) for item in episodes if item.get(field) is not None]
        return statistics.median(values) if values else None

    def _confidence_interval(self, values: list[float]) -> list[float] | None:
        if len(values) < 2:
            return None
        mean = statistics.mean(values)
        margin = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
        return [mean - margin, mean + margin]

    def _counts(self, values: Any) -> dict[str, int]:
        result: dict[str, int] = defaultdict(int)
        for value in values:
            result[str(value)] += 1
        return dict(result)

    def _percent(self, numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator * 100.0, 4) if denominator else None

    def _time(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


class RiskTierCounterfactualSimulationService:
    TIERS = (1.0, 2.0, 3.0, 5.0)

    def simulate(self, episodes: list[dict[str, Any]], *, starting_equity: float) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for tier in self.TIERS:
            base = self._simulate_tier(episodes, starting_equity, tier)
            base["sensitivity"] = {
                "worse_slippage": self._simulate_tier(episodes, starting_equity, tier, stress_key="worse_slippage_per_unit"),
                "wider_spread": self._simulate_tier(episodes, starting_equity, tier, stress_key="wider_spread_cost_per_unit"),
                "one_additional_consecutive_loss": self._simulate_tier(
                    episodes,
                    starting_equity,
                    tier,
                    initial_consecutive_losses=1,
                ),
            }
            result[f"{tier:g}_percent"] = base
        return result

    def _simulate_tier(
        self,
        episodes: list[dict[str, Any]],
        starting_equity: float,
        requested_pct: float,
        *,
        stress_key: str | None = None,
        initial_consecutive_losses: int = 0,
    ) -> dict[str, Any]:
        equity = float(starting_equity)
        peak = equity
        maximum_drawdown = 0.0
        consecutive_losses = int(initial_consecutive_losses)
        worst_loss_streak = 0
        rejected_daily = 0
        rejected_open = 0
        downgraded = 0
        one_lot_infeasible = 0
        results: list[float] = []
        daily_loss: dict[str, float] = defaultdict(float)
        daily_planned: dict[str, float] = defaultdict(float)
        worst_day = 0.0
        drawdown_started_at: datetime | None = None
        longest_recovery_seconds = 0.0
        overlap_rejections = 0
        for episode in sorted(episodes, key=lambda item: str(item.get("decision_at") or "")):
            if not (episode.get("outcome") or {}).get("executable_data_sufficient"):
                continue
            date_key = str(episode.get("trading_date") or str(episode.get("decision_at") or "")[:10])
            drawdown_pct = (peak - equity) / max(peak, 0.01) * 100.0
            approved_pct = min(requested_pct, 5.0, float(settings.max_risk_per_trade_percent))
            if consecutive_losses >= 1 or drawdown_pct >= float(settings.risk_reduction_after_drawdown_percent):
                if approved_pct > float(settings.risk_tier_1_base_pct):
                    downgraded += 1
                approved_pct = float(settings.risk_tier_1_base_pct)
            risk_amount = equity * approved_pct / 100.0
            if -daily_loss[date_key] >= equity * float(settings.max_realized_daily_loss_percent) / 100.0:
                rejected_daily += 1
                continue
            if daily_planned[date_key] + risk_amount > equity * float(settings.max_daily_planned_risk_percent) / 100.0:
                rejected_daily += 1
                continue
            if episode.get("overlapping_open_risk", 0.0) + risk_amount > equity * float(settings.max_total_open_risk_percent) / 100.0:
                rejected_open += 1
                continue
            if episode.get("overlaps_forbidden_position"):
                overlap_rejections += 1
                continue
            risk_per_unit = float(episode.get("risk_per_unit") or 0.0)
            lot_size = int(episode.get("lot_size") or 0)
            if risk_per_unit <= 0 or lot_size <= 0:
                one_lot_infeasible += 1
                continue
            lots = math.floor(risk_amount / (risk_per_unit * lot_size))
            entry_price = float(episode.get("entry_price") or 0.0)
            affordable_lots = math.floor(equity / max(entry_price * lot_size, 0.01)) if entry_price > 0 else 0
            lots = min(lots, affordable_lots)
            if lots <= 0:
                one_lot_infeasible += 1
                continue
            quantity = lots * lot_size
            net_per_unit = float(episode.get("outcome", {}).get("after_cost_result_per_unit") or episode.get("outcome", {}).get("after_cost_result") or 0.0)
            if stress_key:
                net_per_unit -= abs(float(episode.get(stress_key) or 0.0))
            pnl = net_per_unit * quantity
            daily_planned[date_key] += risk_amount
            equity += pnl
            daily_loss[date_key] += min(0.0, pnl)
            worst_day = min(worst_day, daily_loss[date_key])
            results.append(pnl)
            if pnl < 0:
                consecutive_losses += 1
                worst_loss_streak = max(worst_loss_streak, consecutive_losses)
            else:
                consecutive_losses = 0
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, (peak - equity) / max(peak, 0.01) * 100.0)
            decision_at = self._time(episode.get("decision_at"))
            if equity < peak and drawdown_started_at is None:
                drawdown_started_at = decision_at
            elif equity >= peak and drawdown_started_at is not None:
                longest_recovery_seconds = max(longest_recovery_seconds, (decision_at - drawdown_started_at).total_seconds())
                drawdown_started_at = None
        return {
            "hypothetical_only": True,
            "requested_risk_percent": requested_pct,
            "ending_equity": round(equity, 2),
            "return_on_equity_percent": round((equity - starting_equity) / max(starting_equity, 0.01) * 100.0, 4),
            "maximum_drawdown_percent": round(maximum_drawdown, 4),
            "worst_day_rupees": round(worst_day, 2),
            "worst_loss_streak": worst_loss_streak,
            "longest_recovery_seconds": round(longest_recovery_seconds, 3) if longest_recovery_seconds else None,
            "trades": len(results),
            "one_lot_infeasible_count": one_lot_infeasible,
            "downgraded_count": downgraded,
            "daily_limit_rejection_count": rejected_daily,
            "open_risk_rejection_count": rejected_open,
            "overlap_rejection_count": overlap_rejections,
            "stress_case": stress_key,
            "active_policy_changed": False,
        }

    def _time(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return datetime.min


class ShadowPolicyPromotionService:
    LEVELS = (
        "SHADOW_ONLY",
        "CONTROLLED_PAPER",
        "LIMITED_LIVE_TIER_1",
        "ELIGIBLE_FOR_TIER_2_REVIEW",
        "ELIGIBLE_FOR_TIER_3_REVIEW",
        "ELIGIBLE_FOR_TIER_4_REVIEW",
    )

    def assess(self, report: dict[str, Any], *, requested_level: str = "CONTROLLED_PAPER") -> dict[str, Any]:
        reasons: list[str] = []
        if requested_level not in self.LEVELS:
            raise ValueError("unknown promotion level")
        metrics = report
        if int(metrics.get("unique_episodes") or 0) < 100:
            reasons.append("INSUFFICIENT_UNIQUE_EPISODES")
        if metrics.get("after_cost_expectancy") is None or float(metrics["after_cost_expectancy"]) <= 0:
            reasons.append("AFTER_COST_EXPECTANCY_NOT_POSITIVE")
        if float(metrics.get("data_coverage_rate_percent") or 0.0) < 90.0:
            reasons.append("DATA_COMPLETENESS_INADEQUATE")
        if metrics.get("worst_fold_positive") is not True:
            reasons.append("WORST_CHRONOLOGICAL_FOLD_NOT_ACCEPTABLE")
        if metrics.get("stress_costs_stable") is not True:
            reasons.append("WORSE_COST_STRESS_NOT_PASSED")
        if metrics.get("lookahead_audit_passed") is not True:
            reasons.append("LOOKAHEAD_AUDIT_NOT_PASSED")
        if metrics.get("replay_reproducible") is not True:
            reasons.append("REPLAY_NOT_REPRODUCIBLE")
        return {
            "current_level": "SHADOW_ONLY",
            "requested_level": requested_level,
            "eligible": not reasons,
            "reasons": reasons,
            "automatic_promotion": False,
        }
