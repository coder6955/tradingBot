from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.config import settings
from app.services.database import OpportunityRecord, get_session
from app.services.time_utils import ist_now_naive


class OutcomeLearningService:
    """Use closed opportunity outcomes to avoid setups that have actually failed."""

    WIN_OUTCOMES = {"target_1", "target_2", "target_3", "winner"}
    LOSS_OUTCOMES = {"stop_loss", "false_signal", "loser", "expired"}

    def __init__(self, *, clock=None) -> None:
        self.clock = clock or ist_now_naive
        self._history_cache: list[dict[str, Any]] | None = None
        self._history_cache_at: datetime | None = None

    def evaluate(
        self, *, symbol: str, action: str, factor_scores: dict[str, Any]
    ) -> dict[str, Any]:
        if not settings.enable_outcome_learning_guard:
            return {"enabled": False, "passed": True, "reasons": [], "details": {}}

        groups = self._candidate_groups(
            symbol=symbol, action=action, factor_scores=factor_scores
        )
        history = self._closed_history()
        stats = self._group_stats(history)
        matched = []
        reasons: list[str] = []
        passed = True

        for group in groups:
            item = stats.get(group)
            if not item:
                continue
            confidence = self._sample_confidence(int(item["trades"]))
            if confidence in {"insufficient", "weak"}:
                matched.append(
                    {
                        **item,
                        "group": group,
                        "used_as_guard": False,
                        "sample_confidence": confidence,
                        "reason": "sample size too small for blocking",
                    }
                )
                continue
            item_passed = (
                item["expectancy_pct"] >= settings.min_outcome_learning_expectancy_pct
                and item["win_rate_pct"] >= settings.min_outcome_learning_win_rate_pct
            )
            matched.append(
                {
                    **item,
                    "group": group,
                    "used_as_guard": True,
                    "passed": item_passed,
                    "sample_confidence": confidence,
                }
            )
            if not item_passed:
                passed = False
                reasons.append(
                    f"outcome history is weak for {group}: "
                    f"{item['trades']} trades, expectancy {item['expectancy_pct']}%, win rate {item['win_rate_pct']}%"
                )

        return {
            "enabled": True,
            "passed": passed,
            "reasons": reasons,
            "details": {
                "min_trades": settings.min_outcome_learning_trades,
                "confidence_rules": {
                    "below_50": "weak confidence only; never blocks",
                    "50_to_99": "moderate confidence",
                    "100_to_199": "stronger confidence",
                    "200_plus": "high confidence",
                },
                "min_expectancy_pct": settings.min_outcome_learning_expectancy_pct,
                "min_win_rate_pct": settings.min_outcome_learning_win_rate_pct,
                "groups_checked": groups,
                "matched_groups": matched,
                "history_count": len(history),
            },
        }

    def analyze(self) -> dict[str, Any]:
        history = self._closed_history()
        stats = sorted(
            self._group_stats(history).items(),
            key=lambda item: (item[1]["trades"], item[1]["expectancy_pct"]),
            reverse=True,
        )
        credible = [
            {"group": group, **item} for group, item in stats if item["trades"] >= 50
        ]
        weak = [
            item
            for item in credible
            if item["expectancy_pct"] < settings.min_outcome_learning_expectancy_pct
            or item["win_rate_pct"] < settings.min_outcome_learning_win_rate_pct
        ]
        strong = [
            item
            for item in credible
            if item["expectancy_pct"] >= settings.min_outcome_learning_expectancy_pct
            and item["win_rate_pct"] >= settings.min_outcome_learning_win_rate_pct
        ]
        return {
            "enabled": settings.enable_outcome_learning_guard,
            "history_count": len(history),
            "thresholds": {
                "min_trades": settings.min_outcome_learning_trades,
                "blocking_min_trades": 50,
                "min_expectancy_pct": settings.min_outcome_learning_expectancy_pct,
                "min_win_rate_pct": settings.min_outcome_learning_win_rate_pct,
                "lookback": settings.outcome_learning_lookback,
            },
            "strong_groups": strong[:20],
            "weak_groups": weak[:20],
            "all_credible_groups": credible[:50],
        }

    def _closed_history(self) -> list[dict[str, Any]]:
        now = self.clock().replace(tzinfo=None)
        ttl_seconds = max(1, int(settings.outcome_learning_cache_ttl_seconds))
        if self._history_cache is not None and self._history_cache_at is not None:
            if (now - self._history_cache_at).total_seconds() <= ttl_seconds:
                return list(self._history_cache)
        session = get_session()
        try:
            records = (
                session.query(OpportunityRecord)
                .filter(OpportunityRecord.status == "closed")
                .order_by(OpportunityRecord.id.desc())
                .limit(settings.outcome_learning_lookback)
                .all()
            )
            history = [
                self._history_item(record)
                for record in records
                if record.outcome in self.WIN_OUTCOMES | self.LOSS_OUTCOMES
            ]
            self._history_cache = list(history)
            self._history_cache_at = now
            return history
        finally:
            session.close()

    def _history_item(self, record: OpportunityRecord) -> dict[str, Any]:
        factors = self._json(record.factor_scores_json)
        pnl_pct = 0.0
        if record.entry_price and record.exit_price is not None:
            multiplier = 1 if record.side == "BUY" else -1
            pnl_pct = (
                (
                    (record.exit_price - record.entry_price)
                    / max(record.entry_price, 0.01)
                )
                * 100
                * multiplier
            )
        return {
            "symbol": record.symbol,
            "action": record.action,
            "outcome": record.outcome,
            "pnl_pct": pnl_pct,
            "factor_scores": factors,
            "groups": self._candidate_groups(
                symbol=record.symbol, action=record.action, factor_scores=factors
            ),
        }

    def _group_stats(self, history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in history:
            for group in item["groups"]:
                grouped[group].append(item)

        stats: dict[str, dict[str, Any]] = {}
        for group, items in grouped.items():
            wins = [item for item in items if item["outcome"] in self.WIN_OUTCOMES]
            losses = [item for item in items if item["outcome"] in self.LOSS_OUTCOMES]
            pnl_values = [float(item["pnl_pct"]) for item in items]
            avg_win = (
                sum(float(item["pnl_pct"]) for item in wins) / len(wins)
                if wins
                else 0.0
            )
            avg_loss = (
                abs(sum(float(item["pnl_pct"]) for item in losses) / len(losses))
                if losses
                else 0.0
            )
            gross_win = sum(max(float(item["pnl_pct"]), 0.0) for item in items)
            gross_loss = abs(sum(min(float(item["pnl_pct"]), 0.0) for item in items))
            stats[group] = {
                "trades": len(items),
                "sample_confidence": self._sample_confidence(len(items)),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": round((len(wins) / len(items)) * 100, 2)
                if items
                else 0.0,
                "expectancy_pct": round(sum(pnl_values) / len(pnl_values), 3)
                if pnl_values
                else 0.0,
                "avg_win_pct": round(avg_win, 3),
                "avg_loss_pct": round(avg_loss, 3),
                "profit_factor": round(gross_win / gross_loss, 3)
                if gross_loss
                else None,
            }
        return stats

    def _sample_confidence(self, trades: int) -> str:
        if trades < 50:
            return "weak" if trades > 0 else "insufficient"
        if trades < 100:
            return "moderate"
        if trades < 200:
            return "stronger"
        return "high"

    def _candidate_groups(
        self, *, symbol: str, action: str, factor_scores: dict[str, Any]
    ) -> list[str]:
        groups = [
            f"symbol:{symbol.upper()}",
            f"action:{action.upper()}",
            f"symbol_action:{symbol.upper()}:{action.upper()}",
        ]
        setup_type = self._nested_str(
            factor_scores, "signal", "setup_type"
        ) or self._nested_str(factor_scores, "setup_type")
        if not setup_type:
            setup_type = (
                "directional_put_buy"
                if action.upper() == "BUY_PE"
                else "directional_call_buy"
                if action.upper() == "BUY_CE"
                else ""
            )
        if setup_type:
            groups.append(f"setup:{setup_type}")

        day_type = self._nested_str(factor_scores, "day_type", "details", "day_type")
        if day_type:
            groups.append(f"day_type:{day_type}")

        time_bucket = self._nested_str(
            factor_scores, "time_bucket_edge", "details", "bucket"
        )
        if time_bucket:
            groups.append(f"time_bucket:{time_bucket}")
            if action:
                groups.append(f"time_bucket_action:{time_bucket}:{action.upper()}")

        premium_score = self._nested_float(
            factor_scores, "option_premium_confirmation", "score"
        )
        if premium_score is not None:
            groups.append(f"premium_confirmation:{self._score_bucket(premium_score)}")

        quality_score = self._nested_float(factor_scores, "option_quality", "score")
        if quality_score is not None:
            groups.append(f"option_quality:{self._score_bucket(quality_score)}")

        return list(dict.fromkeys(groups))

    def _score_bucket(self, score: float) -> str:
        if score >= 80:
            return "strong"
        if score >= 60:
            return "usable"
        return "weak"

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _nested_str(self, value: dict[str, Any], *keys: str) -> str:
        current: Any = value
        for key in keys:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return str(current) if current not in {None, ""} else ""

    def _nested_float(self, value: dict[str, Any], *keys: str) -> float | None:
        current: Any = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        try:
            return float(current)
        except (TypeError, ValueError):
            return None
