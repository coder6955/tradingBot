from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import func

from app.services.database import Candle, OpportunityRecord, OptionQuoteSnapshot, RejectedOpportunityRecord, TradeRecord, get_session
from app.services.time_utils import ist_today
from app.services.trading_metrics import LOSS_OUTCOMES, WIN_OUTCOMES, max_drawdown, score_bucket, time_bucket


class ProfessionalInsightsService:
    """Read-only research reports for Bank Nifty option-buying operations.

    The service studies saved decisions and executions. It does not alter the
    scanner strategy, hard gates, score calculation, or order flow.
    """

    REJECTION_REASON_CATEGORIES = {
        "premium_candles_stale_or_missing": ("premium_candles_stale_or_missing", "premium candles stale", "previous-day option candles"),
        "insufficient_current_session_premium_candles": ("insufficient_current_session_premium_candles",),
        "option_premium_confirmation_score_below_threshold": (
            "option premium confirmation score is below threshold",
            "option_premium_confirmation_score_below_threshold",
        ),
        "top_banks_mixed": ("top banks are mixed", "not enough top banks", "hdfc and icici are opposite"),
        "insufficient_room_to_level": ("insufficient room", "too little room", "nearest support/resistance leaves too little room"),
        "expected_move_too_small": ("expected move is smaller", "expected_move_coverage_weak"),
        "day_type_score_below_threshold": ("day type score is below", "day type filter failed"),
        "entry_timing_price_inputs_missing": ("entry_timing_price_inputs_missing",),
        "market_closed": ("market_closed", "market closed", "outside market hours"),
        "data_stale": ("data is stale", "quote is stale", "quotes are stale", "tick_stale"),
        "quote_invalid": ("selected_option_quote_invalid", "quote invalid", "quote_unavailable", "invalid quote"),
    }

    def analyze(self, *, symbol: str | None = "BANKNIFTY", limit: int = 1000) -> dict[str, Any]:
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "accepted_vs_rejected": self._accepted_vs_rejected(opportunities, rejections),
            "time_bucket_edge": self._time_bucket_edge(opportunities, rejections, trades),
            "expiry_dte_segmentation": self._dte_segmentation(opportunities, rejections, trades),
            "factor_attribution": self._factor_attribution(opportunities, rejections),
            "live_execution_quality": self._live_execution_quality(trades),
            "exit_policy_analytics": self._exit_policy_analytics(trades, opportunities),
            "no_trade_regime_detection": self._no_trade_regime_detection(rejections),
            "strategy_versions": self._strategy_versions(opportunities, rejections),
            "shadow_mode_comparison": self._shadow_mode_comparison(trades),
            "notes": [
                "These reports are evidence dashboards only; they do not change scanner logic.",
                "Use larger paper/live-shadow samples before promoting any finding into a hard rule.",
            ],
        }

    def daily_banknifty_summary(self, *, summary_date: date | None = None) -> dict[str, Any]:
        day = summary_date or ist_today()
        opportunities, rejections, trades = self._load_day(symbol="BANKNIFTY", day=day)
        paper_trades = [trade for trade in trades if str(trade.mode or "").lower() == "paper"]
        closed_paper = [trade for trade in paper_trades if str(trade.status or "").lower() == "closed"]
        open_paper = [trade for trade in paper_trades if str(trade.status or "").lower() != "closed"]
        winning_trades = [trade for trade in closed_paper if self._trade_is_win(trade)]
        losing_trades = [trade for trade in closed_paper if self._trade_is_loss(trade)]
        gross_values = [float(trade.gross_pnl) for trade in closed_paper if trade.gross_pnl is not None]
        net_values = [value for trade in closed_paper if (value := self._trade_net_pnl(trade)) is not None]
        win_values = [value for trade in winning_trades if (value := self._trade_net_pnl(trade)) is not None]
        loss_values = [value for trade in losing_trades if (value := self._trade_net_pnl(trade)) is not None]
        reason_counts = self._grouped_rejection_reason_counts(rejections)
        low_sample = len(closed_paper) < 30
        data_health_warnings = self._daily_data_health_warnings(
            paper_trades=paper_trades,
            opportunities=opportunities,
            rejections=rejections,
            reason_counts=reason_counts,
        )
        return {
            "status": "ok",
            "date": day.isoformat(),
            "symbol": "BANKNIFTY",
            "total_paper_trades": len(paper_trades),
            "total_closed_paper_trades": len(closed_paper),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "open_trades": len(open_paper),
            "gross_pnl": round(sum(gross_values), 2) if gross_values else None,
            "net_pnl": round(sum(net_values), 2) if net_values else None,
            "average_win": round(sum(win_values) / len(win_values), 2) if win_values else None,
            "average_loss": round(abs(sum(loss_values)) / len(loss_values), 2) if loss_values else None,
            "total_rejected_opportunities": len(rejections),
            "rejection_reasons_count": reason_counts,
            "top_5_rejection_reasons": [
                {"reason": reason, "count": count}
                for reason, count in Counter(reason_counts).most_common(5)
                if count > 0
            ],
            "total_scanner_no_trade_count": len(rejections),
            "total_signal_count": len(opportunities),
            "data_health_warnings": data_health_warnings,
            "low_sample_warning": low_sample,
            "recommendation": {
                "safe_to_change_strategy": False,
                "safe_to_enable_live": False,
                "reason": "Need more paper/live-shadow samples before changing strategy.",
                "next_action": "Collect more sessions.",
            },
        }

    def daily_review(self, *, symbol: str | None = "BANKNIFTY", review_date: date | None = None, limit: int = 1000) -> dict[str, Any]:
        day = review_date or ist_today()
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        opportunities = [row for row in opportunities if self._same_day(row.created_at, day)]
        rejections = [row for row in rejections if self._same_day(row.created_at, day)]
        trades = [row for row in trades if self._same_day(row.created_at, day)]
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "date": day.isoformat(),
            "sample": {
                "accepted_opportunities": len(opportunities),
                "rejected_setups": len(rejections),
                "trades": len(trades),
                "closed_trades": len([trade for trade in trades if trade.status == "closed"]),
            },
            "accepted_performance": self._opportunity_summary(opportunities),
            "trade_performance": self._trade_summary(trades),
            "top_rejection_gates": dict(Counter(str(row.primary_gate or "unknown") for row in rejections).most_common(20)),
            "top_rejection_reasons": dict(self._reason_counter(rejections).most_common(25)),
            "exit_outcomes": dict(Counter(str(row.outcome or "open") for row in trades).most_common()),
            "timeline": self._timeline(opportunities, rejections, trades, limit=100),
        }

    def trade_journal(self, *, symbol: str | None = "BANKNIFTY", limit: int = 200) -> dict[str, Any]:
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "count": min(limit, len(opportunities) + len(rejections) + len(trades)),
            "timeline": self._timeline(opportunities, rejections, trades, limit=limit),
        }

    def data_completeness(self, *, symbol: str | None = "BANKNIFTY") -> dict[str, Any]:
        symbol_value = symbol.upper() if symbol else None
        session = get_session()
        try:
            candle_query = session.query(Candle)
            option_query = session.query(OptionQuoteSnapshot)
            if symbol_value:
                candle_query = candle_query.filter(Candle.symbol == symbol_value)
                option_query = option_query.filter(OptionQuoteSnapshot.underlying == symbol_value)
            candle_count = candle_query.count()
            option_count = option_query.count()
            latest_candle = candle_query.order_by(Candle.timestamp.desc()).first()
            latest_option = option_query.order_by(OptionQuoteSnapshot.timestamp.desc()).first()
            candle_timeframes = {
                str(row[0]): int(row[1])
                for row in candle_query.with_entities(Candle.timeframe, func.count(Candle.id)).group_by(Candle.timeframe).all()
            }
            option_dates = {
                str(row[0]): int(row[1])
                for row in option_query.with_entities(func.date(OptionQuoteSnapshot.timestamp), func.count(OptionQuoteSnapshot.id))
                .group_by(func.date(OptionQuoteSnapshot.timestamp))
                .order_by(func.date(OptionQuoteSnapshot.timestamp).desc())
                .limit(10)
                .all()
            }
            return {
                "status": "ok",
                "symbol": symbol_value or "ALL",
                "candles": {
                    "rows": candle_count,
                    "latest_timestamp": self._dt(latest_candle.timestamp if latest_candle else None),
                    "timeframes": candle_timeframes,
                },
                "option_snapshots": {
                    "rows": option_count,
                    "latest_timestamp": self._dt(latest_option.timestamp if latest_option else None),
                    "recent_session_counts": option_dates,
                },
                "readiness": {
                    "has_underlying_candles": candle_count > 0,
                    "has_option_snapshots": option_count > 0,
                    "warning": None if candle_count and option_count else "Data collection sample is still incomplete.",
                },
            }
        finally:
            session.close()

    def _load(
        self, *, symbol: str | None, limit: int
    ) -> tuple[list[OpportunityRecord], list[RejectedOpportunityRecord], list[TradeRecord]]:
        session = get_session()
        try:
            opportunity_query = session.query(OpportunityRecord).order_by(OpportunityRecord.id.desc())
            rejection_query = session.query(RejectedOpportunityRecord).order_by(RejectedOpportunityRecord.id.desc())
            trade_query = session.query(TradeRecord).order_by(TradeRecord.id.desc())
            if symbol:
                symbol_value = symbol.upper()
                opportunity_query = opportunity_query.filter(OpportunityRecord.symbol == symbol_value)
                rejection_query = rejection_query.filter(RejectedOpportunityRecord.symbol == symbol_value)
                trade_query = trade_query.filter(TradeRecord.symbol == symbol_value)
            return opportunity_query.limit(limit).all(), rejection_query.limit(limit).all(), trade_query.limit(limit).all()
        finally:
            session.close()

    def _load_day(
        self, *, symbol: str, day: date
    ) -> tuple[list[OpportunityRecord], list[RejectedOpportunityRecord], list[TradeRecord]]:
        start = datetime.combine(day, datetime.min.time())
        end = datetime.combine(day, datetime.max.time())
        session = get_session()
        try:
            opportunities = (
                session.query(OpportunityRecord)
                .filter(OpportunityRecord.symbol == symbol.upper())
                .filter(OpportunityRecord.created_at >= start)
                .filter(OpportunityRecord.created_at <= end)
                .order_by(OpportunityRecord.id.desc())
                .all()
            )
            rejections = (
                session.query(RejectedOpportunityRecord)
                .filter(RejectedOpportunityRecord.symbol == symbol.upper())
                .filter(RejectedOpportunityRecord.created_at >= start)
                .filter(RejectedOpportunityRecord.created_at <= end)
                .order_by(RejectedOpportunityRecord.id.desc())
                .all()
            )
            trades = (
                session.query(TradeRecord)
                .filter(TradeRecord.symbol == symbol.upper())
                .filter(TradeRecord.created_at >= start)
                .filter(TradeRecord.created_at <= end)
                .order_by(TradeRecord.id.desc())
                .all()
            )
            return opportunities, rejections, trades
        finally:
            session.close()

    def _accepted_vs_rejected(
        self, opportunities: list[OpportunityRecord], rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        closed = [row for row in opportunities if row.status == "closed"]
        rejected_reviewed = [row for row in rejections if row.later_outcome]
        missed_winners = [row for row in rejected_reviewed if self._is_win(row.later_outcome)]
        saved_losers = [row for row in rejected_reviewed if self._is_loss(row.later_outcome)]
        return {
            "accepted": self._opportunity_summary(closed),
            "rejected": {
                "total": len(rejections),
                "reviewed_later": len(rejected_reviewed),
                "missed_winners": len(missed_winners),
                "saved_losers": len(saved_losers),
                "missed_winner_rate_pct": round((len(missed_winners) / len(rejected_reviewed)) * 100, 2) if rejected_reviewed else 0.0,
                "top_gates_on_missed_winners": dict(Counter(str(row.primary_gate or "unknown") for row in missed_winners).most_common(15)),
            },
            "balance": {
                "accept_rate_pct": round((len(opportunities) / (len(opportunities) + len(rejections))) * 100, 2)
                if opportunities or rejections
                else 0.0,
                "interpretation": self._filtering_interpretation(opportunities, rejected_reviewed),
            },
        }

    def _time_bucket_edge(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        trades: list[TradeRecord],
    ) -> dict[str, Any]:
        return {
            "accepted_opportunities": self._group_summary(opportunities, lambda row: time_bucket(row.created_at), "opportunity"),
            "executed_trades": self._group_summary(trades, lambda row: time_bucket(row.created_at), "trade"),
            "rejected_later_outcomes": self._group_summary(rejections, lambda row: time_bucket(row.created_at), "rejection"),
        }

    def _dte_segmentation(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        trades: list[TradeRecord],
    ) -> dict[str, Any]:
        return {
            "accepted_opportunities": self._group_summary(opportunities, self._dte_bucket, "opportunity"),
            "executed_trades": self._group_summary(trades, self._dte_bucket, "trade"),
            "rejected_later_outcomes": self._group_summary(rejections, self._dte_bucket, "rejection"),
        }

    def _factor_attribution(
        self, opportunities: list[OpportunityRecord], rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        rows: dict[str, list[tuple[str, float, str | None]]] = defaultdict(list)
        for opportunity in opportunities:
            factors = self._json(opportunity.factor_scores_json)
            pnl = float(opportunity.pnl or 0.0)
            outcome = opportunity.outcome
            for label in self._factor_labels(factors, accepted=True, row=opportunity):
                rows[label].append(("accepted", pnl, outcome))
        for rejection in rejections:
            factors = self._json(rejection.factor_scores_json)
            move = self._rejected_move_value(rejection)
            outcome = rejection.later_outcome
            for label in self._factor_labels(factors, accepted=False, row=rejection):
                rows[label].append(("rejected", move, outcome))
        result: dict[str, Any] = {}
        for label, samples in sorted(rows.items()):
            result[label] = self._summary_from_values([sample[1] for sample in samples], [sample[2] for sample in samples])
            result[label]["accepted_count"] = len([sample for sample in samples if sample[0] == "accepted"])
            result[label]["rejected_count"] = len([sample for sample in samples if sample[0] == "rejected"])
        return result

    def _live_execution_quality(self, trades: list[TradeRecord]) -> dict[str, Any]:
        live = [trade for trade in trades if trade.mode == "live"]
        deviations: list[float] = []
        price_sources: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        stuck = []
        for trade in live:
            statuses.update([str(trade.status or "unknown")])
            price_sources.update([str(trade.price_source or "unknown")])
            if float(trade.entry_price or 0.0) > 0 and float(trade.average_price or 0.0) > 0:
                deviations.append(((float(trade.average_price or 0.0) - float(trade.entry_price or 0.0)) / float(trade.entry_price or 1.0)) * 100)
            if trade.status in {"closing", "exit_failed", "reconciliation_mismatch"}:
                stuck.append(self._trade_event(trade))
        return {
            "live_trades": len(live),
            "statuses": dict(statuses.most_common()),
            "price_sources": dict(price_sources.most_common()),
            "avg_entry_deviation_pct": round(sum(deviations) / len(deviations), 3) if deviations else 0.0,
            "max_abs_entry_deviation_pct": round(max(abs(value) for value in deviations), 3) if deviations else 0.0,
            "stuck_or_alert_trades": stuck[:20],
        }

    def _exit_policy_analytics(self, trades: list[TradeRecord], opportunities: list[OpportunityRecord]) -> dict[str, Any]:
        closed_trades = [trade for trade in trades if trade.status == "closed"]
        closed_opportunities = [row for row in opportunities if row.status == "closed"]
        return {
            "trade_outcomes": dict(Counter(str(trade.outcome or "unknown") for trade in closed_trades).most_common()),
            "opportunity_outcomes": dict(Counter(str(row.outcome or "unknown") for row in closed_opportunities).most_common()),
            "partial_booking_rows": len([trade for trade in trades if trade.partial_exit_json]),
            "trailing_stop_rows": len([trade for trade in trades if str(trade.outcome or "").lower() == "trailing_stop"]),
            "time_stop_rows": len([trade for trade in trades if str(trade.outcome or "").lower() == "time_stop"]),
            "avg_time_to_confirm_exit_minutes": self._avg_exit_confirmation_minutes(closed_trades),
        }

    def _no_trade_regime_detection(self, rejections: list[RejectedOpportunityRecord]) -> dict[str, Any]:
        reviewed = [row for row in rejections if row.later_outcome]
        by_gate: dict[str, list[RejectedOpportunityRecord]] = defaultdict(list)
        by_reason: dict[str, list[RejectedOpportunityRecord]] = defaultdict(list)
        for row in reviewed:
            by_gate[str(row.primary_gate or "unknown")].append(row)
            for reason in self._json_list(row.reasons_json):
                by_reason[reason].append(row)
        return {
            "reviewed_rejections": len(reviewed),
            "primary_gate_quality": self._later_group_quality(by_gate),
            "reason_quality": self._later_group_quality(by_reason),
            "top_unreviewed_gates": dict(Counter(str(row.primary_gate or "unknown") for row in rejections if not row.later_outcome).most_common(15)),
        }

    def _strategy_versions(
        self, opportunities: list[OpportunityRecord], rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        versions: Counter[str] = Counter()
        for row in opportunities:
            versions.update([self._strategy_version(self._json(row.factor_scores_json))])
        for row in rejections:
            versions.update([self._strategy_version(self._json(row.factor_scores_json))])
        return {
            "versions": dict(versions.most_common()),
            "warning": None if len(versions) <= 1 else "Multiple strategy versions are mixed in this sample; compare them separately.",
        }

    def _shadow_mode_comparison(self, trades: list[TradeRecord]) -> dict[str, Any]:
        shadow = [trade for trade in trades if self._is_shadow_trade(trade)]
        paper = [trade for trade in trades if trade.mode == "paper" and trade not in shadow]
        live = [trade for trade in trades if trade.mode == "live"]
        return {
            "shadow_sample": len(shadow),
            "paper_sample": len(paper),
            "live_sample": len(live),
            "shadow": self._trade_summary(shadow),
            "paper": self._trade_summary(paper),
            "live": self._trade_summary(live),
            "note": "Shadow detection depends on saved order metadata; older rows may not be classifiable.",
        }

    def _timeline(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        trades: list[TradeRecord],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        events = [self._opportunity_event(row) for row in opportunities]
        events.extend(self._rejection_event(row) for row in rejections)
        events.extend(self._trade_event(row) for row in trades)
        return sorted(events, key=lambda item: str(item.get("timestamp") or ""), reverse=True)[:limit]

    def _opportunity_summary(self, rows: list[OpportunityRecord]) -> dict[str, Any]:
        return self._summary_from_values([float(row.pnl or 0.0) for row in rows], [row.outcome for row in rows])

    def _trade_summary(self, rows: list[TradeRecord]) -> dict[str, Any]:
        return self._summary_from_values(
            [float(row.net_pnl if row.net_pnl is not None else row.pnl or 0.0) for row in rows],
            [row.outcome for row in rows],
        )

    def _group_summary(self, rows: Iterable[Any], key_fn: Any, row_type: str) -> dict[str, Any]:
        groups: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            groups[str(key_fn(row))].append(row)
        result: dict[str, Any] = {}
        for key, items in sorted(groups.items()):
            if row_type == "trade":
                result[key] = self._trade_summary(items)
            elif row_type == "rejection":
                result[key] = self._summary_from_values(
                    [self._rejected_move_value(row) for row in items],
                    [row.later_outcome for row in items],
                )
            else:
                result[key] = self._opportunity_summary(items)
        return result

    def _summary_from_values(self, values: list[float], outcomes: list[str | None]) -> dict[str, Any]:
        wins: list[float] = []
        losses: list[float] = []
        unclassified = 0
        for value, outcome in zip(values, outcomes):
            if self._is_win(outcome) or value > 0:
                wins.append(value)
            elif self._is_loss(outcome) or value < 0:
                losses.append(value)
            else:
                unclassified += 1
        gross_win = sum(value for value in wins if value > 0)
        gross_loss = abs(sum(value for value in losses if value < 0))
        classified = len(wins) + len(losses)
        return {
            "trades": len(values),
            "wins": len(wins),
            "losses": len(losses),
            "open_or_unclassified": unclassified,
            "win_rate_pct": round((len(wins) / classified) * 100, 2) if classified else 0.0,
            "average_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "average_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
            "expectancy": round((sum(wins) + sum(losses)) / classified, 2) if classified else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown": round(max_drawdown(values), 2),
            "total_pnl": round(sum(values), 2),
        }

    def _grouped_rejection_reason_counts(self, rows: list[RejectedOpportunityRecord]) -> dict[str, int]:
        counts = {category: 0 for category in self.REJECTION_REASON_CATEGORIES}
        for row in rows:
            for reason in self._json_list(row.reasons_json):
                category = self._rejection_reason_category(reason)
                if category:
                    counts[category] += 1
        return counts

    def _rejection_reason_category(self, reason: str) -> str | None:
        normalized = str(reason or "").strip().lower().replace("-", "_")
        for category, markers in self.REJECTION_REASON_CATEGORIES.items():
            if any(marker in normalized for marker in markers):
                return category
        return None

    def _daily_data_health_warnings(
        self,
        *,
        paper_trades: list[TradeRecord],
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        reason_counts: dict[str, int],
    ) -> list[str]:
        warnings: list[str] = []
        if not paper_trades and not opportunities and not rejections:
            warnings.append("No Bank Nifty paper/live-shadow evidence was stored for this date.")
        if reason_counts.get("premium_candles_stale_or_missing", 0) or reason_counts.get("insufficient_current_session_premium_candles", 0):
            warnings.append("Premium confirmation candles were stale or missing.")
        if reason_counts.get("quote_invalid", 0):
            warnings.append("Invalid or unavailable option quotes were seen.")
        if reason_counts.get("data_stale", 0):
            warnings.append("Stale market data was seen.")
        return warnings

    def _trade_net_pnl(self, trade: TradeRecord) -> float | None:
        if trade.net_pnl is not None:
            return float(trade.net_pnl)
        if trade.pnl is not None:
            return float(trade.pnl)
        return None

    def _trade_is_win(self, trade: TradeRecord) -> bool:
        pnl = self._trade_net_pnl(trade)
        outcome = str(trade.outcome or "").lower()
        return outcome in WIN_OUTCOMES or (pnl is not None and pnl > 0)

    def _trade_is_loss(self, trade: TradeRecord) -> bool:
        pnl = self._trade_net_pnl(trade)
        outcome = str(trade.outcome or "").lower()
        return outcome in LOSS_OUTCOMES or (pnl is not None and pnl < 0)

    def _factor_labels(self, factors: dict[str, Any], *, accepted: bool, row: Any) -> list[str]:
        labels = [f"decision:{'accepted' if accepted else 'rejected'}", f"score:{score_bucket(getattr(row, 'score', 0))}"]
        metadata = factors.get("strategy_metadata", {}) if isinstance(factors.get("strategy_metadata"), dict) else {}
        labels.append(f"strategy_version:{metadata.get('strategy_version') or 'unknown'}")
        premium = factors.get("option_premium_confirmation", {}) if isinstance(factors.get("option_premium_confirmation"), dict) else {}
        if premium:
            labels.append(f"premium_source:{premium.get('source') or premium.get('premium_candle_source') or 'unknown'}")
            labels.append(f"premium_passed:{bool(premium.get('passed'))}")
        day_type = factors.get("day_type", {}) if isinstance(factors.get("day_type"), dict) else {}
        if day_type:
            labels.append(f"day_type:{day_type.get('day_type') or day_type.get('classification') or 'unknown'}")
        if not accepted:
            labels.append(f"rejection_gate:{getattr(row, 'primary_gate', None) or 'unknown'}")
        return labels

    def _later_group_quality(self, groups: dict[str, list[RejectedOpportunityRecord]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, rows in sorted(groups.items()):
            missed_winners = len([row for row in rows if self._is_win(row.later_outcome)])
            saved_losers = len([row for row in rows if self._is_loss(row.later_outcome)])
            result[key] = {
                "reviewed": len(rows),
                "missed_winners": missed_winners,
                "saved_losers": saved_losers,
                "missed_winner_rate_pct": round((missed_winners / len(rows)) * 100, 2) if rows else 0.0,
            }
        return result

    def _rejected_move_value(self, row: RejectedOpportunityRecord) -> float:
        factors = self._json(row.factor_scores_json)
        prices = factors.get("prices", {}) if isinstance(factors.get("prices"), dict) else {}
        entry = float(prices.get("entry_price") or 0.0)
        exit_price = float(row.later_exit_price or 0.0)
        if entry <= 0 or exit_price <= 0:
            if self._is_win(row.later_outcome):
                return 1.0
            if self._is_loss(row.later_outcome):
                return -1.0
            return 0.0
        multiplier = 1 if str(row.side or "BUY").upper() == "BUY" else -1
        return round((exit_price - entry) * multiplier, 2)

    def _filtering_interpretation(self, opportunities: list[OpportunityRecord], reviewed_rejections: list[RejectedOpportunityRecord]) -> str:
        accepted_summary = self._opportunity_summary([row for row in opportunities if row.status == "closed"])
        missed_winner_rate = (
            len([row for row in reviewed_rejections if self._is_win(row.later_outcome)]) / len(reviewed_rejections)
            if reviewed_rejections
            else 0
        )
        expectancy = float(accepted_summary.get("expectancy") or 0.0)
        if expectancy > 0 and missed_winner_rate <= 0.25:
            return "balanced_or_conservative"
        if expectancy <= 0 and missed_winner_rate <= 0.25:
            return "under_filtered_or_exit_quality_issue"
        if missed_winner_rate > 0.4:
            return "possibly_over_filtered"
        return "needs_more_reviewed_rejections"

    def _dte_bucket(self, row: Any) -> str:
        expiry_value = getattr(row, "expiry", None)
        created_at = getattr(row, "created_at", None)
        expiry = self._parse_date(expiry_value)
        if not expiry or not created_at:
            return "unknown_dte"
        days = (expiry - created_at.date()).days
        if days <= 0:
            return "expiry_day"
        if days == 1:
            return "one_dte"
        if days <= 3:
            return "two_three_dte"
        return "four_plus_dte"

    def _avg_exit_confirmation_minutes(self, trades: list[TradeRecord]) -> float:
        values: list[float] = []
        for trade in trades:
            if trade.exit_requested_at and trade.exit_confirmed_at:
                values.append(max(0.0, (trade.exit_confirmed_at - trade.exit_requested_at).total_seconds() / 60))
        return round(sum(values) / len(values), 2) if values else 0.0

    def _is_shadow_trade(self, trade: TradeRecord) -> bool:
        payload = self._json(trade.order_response_json)
        return bool(payload.get("shadow_for_live") or payload.get("live_shadow") or "shadow" in str(trade.notes or "").lower())

    def _strategy_version(self, factors: dict[str, Any]) -> str:
        metadata = factors.get("strategy_metadata", {}) if isinstance(factors.get("strategy_metadata"), dict) else {}
        return str(metadata.get("strategy_version") or "unknown")

    def _opportunity_event(self, row: OpportunityRecord) -> dict[str, Any]:
        return {
            "timestamp": self._dt(row.created_at),
            "type": "accepted_opportunity",
            "id": row.id,
            "tradingsymbol": row.tradingsymbol,
            "action": row.action,
            "score": row.score,
            "status": row.status,
            "outcome": row.outcome,
            "entry_price": row.entry_price,
            "exit_price": row.exit_price,
            "pnl": row.pnl,
        }

    def _rejection_event(self, row: RejectedOpportunityRecord) -> dict[str, Any]:
        return {
            "timestamp": self._dt(row.created_at),
            "type": "rejected_opportunity",
            "id": row.id,
            "tradingsymbol": row.tradingsymbol,
            "action": row.action,
            "score": row.score,
            "primary_gate": row.primary_gate,
            "later_outcome": row.later_outcome,
            "later_exit_price": row.later_exit_price,
        }

    def _trade_event(self, row: TradeRecord) -> dict[str, Any]:
        return {
            "timestamp": self._dt(row.created_at),
            "type": "trade",
            "id": row.id,
            "mode": row.mode,
            "tradingsymbol": row.tradingsymbol,
            "action": row.action,
            "status": row.status,
            "outcome": row.outcome,
            "entry_price": row.entry_price,
            "exit_price": row.exit_price,
            "net_pnl": row.net_pnl,
            "price_source": row.price_source,
        }

    def _reason_counter(self, rows: list[RejectedOpportunityRecord]) -> Counter[str]:
        counter: Counter[str] = Counter()
        for row in rows:
            counter.update(self._json_list(row.reasons_json))
        return counter

    def _same_day(self, value: datetime | None, day: date) -> bool:
        return bool(value and value.date() == day)

    def _parse_date(self, value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(str(value)[:10]).date()
        except ValueError:
            return None

    def _is_win(self, outcome: str | None) -> bool:
        normalized = str(outcome or "").lower()
        return normalized in WIN_OUTCOMES or "target" in normalized or normalized.startswith("would_have_hit_target")

    def _is_loss(self, outcome: str | None) -> bool:
        normalized = str(outcome or "").lower()
        return normalized in LOSS_OUTCOMES or "stop" in normalized or normalized.startswith("would_have_hit_stop")

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _json_list(self, value: str | None) -> list[str]:
        if not value:
            return []
        try:
            data = json.loads(value)
            return [str(item) for item in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def _dt(self, value: datetime | None) -> str | None:
        return value.isoformat(sep=" ") + " IST" if value else None
