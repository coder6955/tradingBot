from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import func

from app.config import settings
from app.services.database import (
    Candle,
    OpportunityRecord,
    OptionQuoteSnapshot,
    RejectedOpportunityRecord,
    TradeRecord,
    get_session,
)
from app.services.execution_realism_service import ExecutionRealismService
from app.services.time_utils import ist_today
from app.services.trading_metrics import (
    LOSS_OUTCOMES,
    WIN_OUTCOMES,
    max_drawdown,
    score_bucket,
    time_bucket,
)


class ProfessionalInsightsService:
    """Read-only research reports for Bank Nifty option-buying operations.

    The service studies saved decisions and executions. It does not alter the
    scanner strategy, hard gates, score calculation, or order flow.
    """

    REJECTION_REASON_CATEGORIES = {
        "premium_candles_stale_or_missing": (
            "premium_candles_stale_or_missing",
            "premium candles stale",
            "previous-day option candles",
        ),
        "insufficient_current_session_premium_candles": (
            "insufficient_current_session_premium_candles",
        ),
        "option_premium_confirmation_score_below_threshold": (
            "option premium confirmation score is below threshold",
            "option_premium_confirmation_score_below_threshold",
        ),
        "top_banks_mixed": (
            "top banks are mixed",
            "not enough top banks",
            "hdfc and icici are opposite",
        ),
        "insufficient_room_to_level": (
            "insufficient room",
            "too little room",
            "nearest support/resistance leaves too little room",
        ),
        "expected_move_too_small": (
            "expected move is smaller",
            "expected_move_coverage_weak",
        ),
        "day_type_score_below_threshold": (
            "day type score is below",
            "day type filter failed",
        ),
        "entry_timing_price_inputs_missing": ("entry_timing_price_inputs_missing",),
        "market_closed": ("market_closed", "market closed", "outside market hours"),
        "data_stale": (
            "data is stale",
            "quote is stale",
            "quotes are stale",
            "tick_stale",
        ),
        "quote_invalid": (
            "selected_option_quote_invalid",
            "quote invalid",
            "quote_unavailable",
            "invalid quote",
        ),
    }

    def analyze(
        self, *, symbol: str | None = "BANKNIFTY", limit: int = 1000
    ) -> dict[str, Any]:
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        eligible_rejections = self._learning_eligible_rejections(rejections)
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "rejection_learning_filter": self._rejection_learning_filter_summary(
                rejections
            ),
            "accepted_vs_rejected": self._accepted_vs_rejected(
                opportunities, eligible_rejections
            ),
            "rejected_opportunity_quality": self.rejected_opportunity_quality_report(
                symbol=symbol, limit=limit
            ),
            "time_bucket_edge": self._time_bucket_edge(
                opportunities, eligible_rejections, trades
            ),
            "expiry_dte_segmentation": self._dte_segmentation(
                opportunities, eligible_rejections, trades
            ),
            "factor_attribution": self._factor_attribution(
                opportunities, eligible_rejections
            ),
            "live_execution_quality": self._live_execution_quality(trades),
            "exit_policy_analytics": self._exit_policy_analytics(trades, opportunities),
            "no_trade_regime_detection": self._no_trade_regime_detection(
                eligible_rejections
            ),
            "strategy_versions": self._strategy_versions(
                opportunities, eligible_rejections
            ),
            "shadow_mode_comparison": self._shadow_mode_comparison(trades),
            "notes": [
                "These reports are evidence dashboards only; they do not change scanner logic.",
                "Rejected-opportunity learning metrics exclude manual diagnostics, market-closed rows, stale data, invalid quotes, and other non-learning rows.",
                "Use larger paper/live-shadow samples before promoting any finding into a hard rule.",
            ],
        }

    def research_engine_report(
        self, *, symbol: str | None = "BANKNIFTY", limit: int = 2000
    ) -> dict[str, Any]:
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        eligible_rejections = self._learning_eligible_rejections(rejections)
        closed_opportunities = [
            row for row in opportunities if str(row.status or "").lower() == "closed"
        ]
        closed_trades = [
            row for row in trades if str(row.status or "").lower() == "closed"
        ]
        reviewed_rejections = [row for row in eligible_rejections if row.later_outcome]
        accepted_rows = closed_trades or closed_opportunities
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "sample": {
                "accepted_opportunities": len(opportunities),
                "closed_accepted_opportunities": len(closed_opportunities),
                "trades": len(trades),
                "closed_trades": len(closed_trades),
                "rejected_opportunities": len(rejections),
                "learning_eligible_rejections": len(eligible_rejections),
                "reviewed_rejections_with_later_outcome": len(reviewed_rejections),
            },
            "filter_rejection_quality": self._filter_rejection_quality(
                eligible_rejections
            ),
            "gate_effectiveness": self.gate_effectiveness_report(
                symbol=symbol, limit=limit
            ),
            "accepted_trade_loss_impact": self._accepted_loss_impact(
                closed_trades, closed_opportunities
            ),
            "mfe_mae": self._mfe_mae_summary(closed_trades),
            "segment_expectancy": {
                "setup_family": self._segment_report(
                    accepted_rows, reviewed_rejections, self._setup_family
                ),
                "weekday": self._segment_report(
                    accepted_rows, reviewed_rejections, self._weekday_bucket
                ),
                "time_block": self._segment_report(
                    accepted_rows,
                    reviewed_rejections,
                    lambda row: time_bucket(getattr(row, "created_at", None)),
                ),
                "expiry_proximity": self._segment_report(
                    accepted_rows, reviewed_rejections, self._dte_bucket
                ),
                "iv_regime": self._segment_report(
                    accepted_rows, reviewed_rejections, self._iv_regime
                ),
                "trend_regime": self._segment_report(
                    accepted_rows, reviewed_rejections, self._trend_regime
                ),
            },
            "setup_family_ranking": self._setup_family_ranking(
                accepted_rows, reviewed_rejections
            ),
            "research_readiness": self._research_readiness(
                closed_trades, closed_opportunities, reviewed_rejections
            ),
            "notes": [
                "This is a read-only research report; it does not alter scanner gates, scores, or orders.",
                "Rejected-trade quality depends on /opportunities/rejections/evaluate-open or the automatic rejected-outcome evaluator having populated later_outcome.",
                "Unknown segment buckets mean older rows did not store enough factor metadata for that dimension.",
            ],
        }

    def threshold_validation_report(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        limit: int = 3000,
        start_date: date | None = None,
        end_date: date | None = None,
        setup_family: str | None = None,
        mode: str = "all",
    ) -> dict[str, Any]:
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        opportunities = self._filter_threshold_rows(
            opportunities,
            start_date=start_date,
            end_date=end_date,
            setup_family=setup_family,
        )
        rejections = self._filter_threshold_rows(
            rejections,
            start_date=start_date,
            end_date=end_date,
            setup_family=setup_family,
        )
        trades = self._filter_threshold_rows(
            trades, start_date=start_date, end_date=end_date, setup_family=setup_family
        )
        trades = self._filter_trades_by_mode(trades, mode)
        eligible_rejections = self._learning_eligible_rejections(rejections)
        reviewed_rejections = [row for row in eligible_rejections if row.later_outcome]
        accepted_rows: list[Any] = [
            row for row in trades if str(row.status or "").lower() == "closed"
        ]
        if not accepted_rows:
            accepted_rows = [
                row
                for row in opportunities
                if str(row.status or "").lower() == "closed"
            ]
        decision_rows: list[Any] = list(opportunities) + list(eligible_rejections)
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "filters": {
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "setup_family": setup_family,
                "mode": mode,
            },
            "threshold_inventory": self._threshold_inventory(),
            "data_support": self._threshold_data_support(
                opportunities, rejections, trades
            ),
            "score_threshold_validation": self._score_threshold_validation(
                decision_rows, accepted_rows, reviewed_rejections
            ),
            "rejection_threshold_validation": self._rejection_threshold_validation(
                eligible_rejections
            ),
            "setup_family_threshold_validation": self._setup_family_threshold_validation(
                accepted_rows, eligible_rejections
            ),
            "time_and_regime_validation": self._time_and_regime_validation(
                accepted_rows, reviewed_rejections
            ),
            "threshold_sensitivity": {
                "minimum_score": self._score_sensitivity(
                    decision_rows, accepted_rows, reviewed_rejections
                ),
            },
            "verdicts": self._threshold_verdicts(accepted_rows, eligible_rejections),
            "not_measurable_yet": self._threshold_not_measurable_yet(
                opportunities, rejections, trades
            ),
            "notes": [
                "This report is read-only; it does not change strategy thresholds, entries, exits, or order placement.",
                "Rejected-trade validation depends on later_outcome/later_exit_price being populated by rejected-outcome evaluation.",
                "Small samples are intentionally classified as NOT_ENOUGH_DATA or INCONCLUSIVE.",
            ],
        }

    def execution_realism_report(
        self, *, symbol: str | None = "BANKNIFTY", limit: int = 1000
    ) -> dict[str, Any]:
        _, _, trades = self._load(symbol=symbol, limit=limit)
        closed = [row for row in trades if str(row.status or "").lower() == "closed"]
        paper = [row for row in closed if str(row.mode or "").lower() == "paper"]
        live = [row for row in closed if str(row.mode or "").lower() == "live"]
        detailed = [
            row
            for row in closed
            if self._json(row.order_response_json).get("execution_realism")
        ]
        charges = [
            float(row.charges or 0.0) for row in closed if row.charges is not None
        ]
        slippage = [
            float(row.slippage_cost or 0.0)
            for row in closed
            if row.slippage_cost is not None
        ]
        spread = [
            float(row.spread_cost or 0.0)
            for row in closed
            if row.spread_cost is not None
        ]
        gross = [
            float(row.gross_pnl or 0.0) for row in closed if row.gross_pnl is not None
        ]
        net = [
            value for row in closed if (value := self._trade_net_pnl(row)) is not None
        ]
        examples = []
        for row in detailed[:10]:
            response = self._json(row.order_response_json)
            examples.append(
                {
                    "trade_id": row.id,
                    "mode": row.mode,
                    "tradingsymbol": row.tradingsymbol,
                    "outcome": row.outcome,
                    "intended_entry_price": response.get("intended_entry_price"),
                    "filled_entry_price": row.average_price or row.entry_price,
                    "exit_price": row.exit_price,
                    "execution_realism": response.get("execution_realism"),
                }
            )
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "assumptions": ExecutionRealismService().assumptions(),
            "sample": {
                "closed_trades": len(closed),
                "paper_closed_trades": len(paper),
                "live_closed_trades": len(live),
                "trades_with_detailed_execution_realism": len(detailed),
            },
            "execution_drag": {
                "gross_pnl": round(sum(gross), 2),
                "net_pnl": round(sum(net), 2),
                "charges": round(sum(charges), 2),
                "slippage_cost": round(sum(slippage), 2),
                "spread_cost": round(sum(spread), 2),
                "gross_to_net_drag": round(sum(gross) - sum(net), 2)
                if gross and net
                else 0.0,
                "avg_charges_per_trade": round(sum(charges) / len(charges), 2)
                if charges
                else 0.0,
            },
            "mfe_mae": self._mfe_mae_summary(closed),
            "examples": examples,
            "notes": [
                "This report is read-only and does not alter entries, exits, or live order flow.",
                "Paper/backtest fills are intentionally conservative; live trades still depend on broker-confirmed average prices.",
                "If detailed execution examples are empty, older rows were created before execution-realism metadata was stored.",
            ],
        }

    def daily_banknifty_summary(
        self, *, summary_date: date | None = None
    ) -> dict[str, Any]:
        day = summary_date or ist_today()
        opportunities, rejections, trades = self._load_day(symbol="BANKNIFTY", day=day)
        eligible_rejections = self._learning_eligible_rejections(rejections)
        paper_trades = [
            trade for trade in trades if str(trade.mode or "").lower() == "paper"
        ]
        closed_paper = [
            trade
            for trade in paper_trades
            if str(trade.status or "").lower() == "closed"
        ]
        open_paper = [
            trade
            for trade in paper_trades
            if str(trade.status or "").lower() != "closed"
        ]
        winning_trades = [trade for trade in closed_paper if self._trade_is_win(trade)]
        losing_trades = [trade for trade in closed_paper if self._trade_is_loss(trade)]
        gross_values = [
            float(trade.gross_pnl)
            for trade in closed_paper
            if trade.gross_pnl is not None
        ]
        net_values = [
            value
            for trade in closed_paper
            if (value := self._trade_net_pnl(trade)) is not None
        ]
        win_values = [
            value
            for trade in winning_trades
            if (value := self._trade_net_pnl(trade)) is not None
        ]
        loss_values = [
            value
            for trade in losing_trades
            if (value := self._trade_net_pnl(trade)) is not None
        ]
        reason_counts = self._grouped_rejection_reason_counts(rejections)
        eligible_reason_counts = self._grouped_rejection_reason_counts(
            eligible_rejections
        )
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
            "average_win": round(sum(win_values) / len(win_values), 2)
            if win_values
            else None,
            "average_loss": round(abs(sum(loss_values)) / len(loss_values), 2)
            if loss_values
            else None,
            "total_rejected_opportunities": len(rejections),
            "learning_eligible_rejections": len(eligible_rejections),
            "learning_excluded_rejections": len(rejections) - len(eligible_rejections),
            "learning_exclusion_reasons": dict(
                Counter(
                    str(row.learning_exclusion_reason or "unknown")
                    for row in rejections
                    if not bool(row.learning_eligible)
                ).most_common(10)
            ),
            "rejection_reasons_count": reason_counts,
            "learning_eligible_rejection_reasons_count": eligible_reason_counts,
            "rejected_opportunity_quality": self._rejected_quality_summary(
                eligible_rejections
            ),
            "gate_effectiveness_top": self._gate_effectiveness_rows(
                eligible_rejections
            )[:10],
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

    def daily_review(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        review_date: date | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        day = review_date or ist_today()
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        opportunities = [
            row for row in opportunities if self._same_day(row.created_at, day)
        ]
        rejections = [row for row in rejections if self._same_day(row.created_at, day)]
        eligible_rejections = self._learning_eligible_rejections(rejections)
        trades = [row for row in trades if self._same_day(row.created_at, day)]
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "date": day.isoformat(),
            "sample": {
                "accepted_opportunities": len(opportunities),
                "rejected_setups": len(rejections),
                "learning_eligible_rejections": len(eligible_rejections),
                "learning_excluded_rejections": len(rejections)
                - len(eligible_rejections),
                "trades": len(trades),
                "closed_trades": len(
                    [trade for trade in trades if trade.status == "closed"]
                ),
            },
            "accepted_performance": self._opportunity_summary(opportunities),
            "trade_performance": self._trade_summary(trades),
            "top_rejection_gates": dict(
                Counter(
                    str(row.primary_gate or "unknown") for row in eligible_rejections
                ).most_common(20)
            ),
            "top_rejection_reasons": dict(
                self._reason_counter(eligible_rejections).most_common(25)
            ),
            "rejected_opportunity_quality": self._rejected_quality_summary(
                eligible_rejections
            ),
            "gate_effectiveness_top": self._gate_effectiveness_rows(
                eligible_rejections
            )[:15],
            "learning_exclusion_reasons": dict(
                Counter(
                    str(row.learning_exclusion_reason or "unknown")
                    for row in rejections
                    if not bool(row.learning_eligible)
                ).most_common(15)
            ),
            "exit_outcomes": dict(
                Counter(str(row.outcome or "open") for row in trades).most_common()
            ),
            "timeline": self._timeline(opportunities, rejections, trades, limit=100),
        }

    def rejected_opportunity_quality_report(
        self, *, symbol: str | None = "BANKNIFTY", limit: int = 3000
    ) -> dict[str, Any]:
        _, rejections, _ = self._load(symbol=symbol, limit=limit)
        eligible = self._learning_eligible_rejections(rejections)
        return {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "summary": self._rejected_quality_summary(eligible),
            "gate_effectiveness": self._gate_effectiveness_rows(eligible),
            "top_missed_winner_gates": self._top_gates(
                eligible, outcome_group="missed_winner"
            ),
            "top_saved_loser_gates": self._top_gates(
                eligible, outcome_group="saved_loser"
            ),
            "top_unresolved_gates": self._top_gates(
                eligible, outcome_group="unresolved"
            ),
            "notes": [
                "Missed winners are rejected rows whose later_outcome hit a target.",
                "Saved losers are rejected rows whose later_outcome hit stop loss.",
                "Ambiguous rows touched stop and target inside the same candle and are not counted as wins or losses.",
            ],
        }

    def gate_effectiveness_report(
        self,
        *,
        symbol: str | None = "BANKNIFTY",
        limit: int = 3000,
        summary_only: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        if summary_only:
            rejections = self._load_rejections(symbol=symbol, limit=limit)
            eligible = self._learning_eligible_rejections(rejections)
            gates = self._gate_effectiveness_rows(eligible)
            if top_n is not None:
                gates = gates[: max(1, int(top_n))]
            return {
                "status": "ok",
                "symbol": symbol.upper() if symbol else "ALL",
                "limit": limit,
                "summary_only": True,
                "rejected_summary": self._rejected_quality_summary(eligible),
                "gates": gates,
            }
        opportunities, rejections, trades = self._load(symbol=symbol, limit=limit)
        eligible = self._learning_eligible_rejections(rejections)
        accepted_rows: list[Any] = [
            row for row in trades if str(row.status or "").lower() == "closed"
        ]
        if not accepted_rows:
            accepted_rows = [
                row
                for row in opportunities
                if str(row.status or "").lower() == "closed"
            ]
        result = {
            "status": "ok",
            "symbol": symbol.upper() if symbol else "ALL",
            "limit": limit,
            "accepted_summary": self._summary_for_rows(accepted_rows),
            "rejected_summary": self._rejected_quality_summary(eligible),
            "gates": self._gate_effectiveness_rows(eligible),
            "accepted_vs_rejected": self._accepted_vs_rejected(opportunities, eligible),
            "comparison": self._accepted_rejected_comparison(accepted_rows, eligible),
        }
        if top_n is not None:
            result["gates"] = result["gates"][: max(1, int(top_n))]
        return result

    def trade_journal(
        self, *, symbol: str | None = "BANKNIFTY", limit: int = 200
    ) -> dict[str, Any]:
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
                option_query = option_query.filter(
                    OptionQuoteSnapshot.underlying == symbol_value
                )
            candle_count = candle_query.count()
            option_count = option_query.count()
            latest_candle = candle_query.order_by(Candle.timestamp.desc()).first()
            latest_option = option_query.order_by(
                OptionQuoteSnapshot.timestamp.desc()
            ).first()
            candle_timeframes = {
                str(row[0]): int(row[1])
                for row in candle_query.with_entities(
                    Candle.timeframe, func.count(Candle.id)
                )
                .group_by(Candle.timeframe)
                .all()
            }
            option_dates = {
                str(row[0]): int(row[1])
                for row in option_query.with_entities(
                    func.date(OptionQuoteSnapshot.timestamp),
                    func.count(OptionQuoteSnapshot.id),
                )
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
                    "latest_timestamp": self._dt(
                        latest_candle.timestamp if latest_candle else None
                    ),
                    "timeframes": candle_timeframes,
                },
                "option_snapshots": {
                    "rows": option_count,
                    "latest_timestamp": self._dt(
                        latest_option.timestamp if latest_option else None
                    ),
                    "recent_session_counts": option_dates,
                },
                "readiness": {
                    "has_underlying_candles": candle_count > 0,
                    "has_option_snapshots": option_count > 0,
                    "warning": None
                    if candle_count and option_count
                    else "Data collection sample is still incomplete.",
                },
            }
        finally:
            session.close()

    def _load(
        self, *, symbol: str | None, limit: int
    ) -> tuple[
        list[OpportunityRecord], list[RejectedOpportunityRecord], list[TradeRecord]
    ]:
        session = get_session()
        try:
            opportunity_query = session.query(OpportunityRecord).order_by(
                OpportunityRecord.id.desc()
            )
            rejection_query = session.query(RejectedOpportunityRecord).order_by(
                RejectedOpportunityRecord.id.desc()
            )
            trade_query = session.query(TradeRecord).order_by(TradeRecord.id.desc())
            if symbol:
                symbol_value = symbol.upper()
                opportunity_query = opportunity_query.filter(
                    OpportunityRecord.symbol == symbol_value
                )
                rejection_query = rejection_query.filter(
                    RejectedOpportunityRecord.symbol == symbol_value
                )
                trade_query = trade_query.filter(TradeRecord.symbol == symbol_value)
            return (
                opportunity_query.limit(limit).all(),
                rejection_query.limit(limit).all(),
                trade_query.limit(limit).all(),
            )
        finally:
            session.close()

    def _load_rejections(
        self, *, symbol: str | None, limit: int
    ) -> list[RejectedOpportunityRecord]:
        session = get_session()
        try:
            query = session.query(RejectedOpportunityRecord).order_by(
                RejectedOpportunityRecord.id.desc()
            )
            if symbol:
                query = query.filter(RejectedOpportunityRecord.symbol == symbol.upper())
            return query.limit(limit).all()
        finally:
            session.close()

    def _load_day(
        self, *, symbol: str, day: date
    ) -> tuple[
        list[OpportunityRecord], list[RejectedOpportunityRecord], list[TradeRecord]
    ]:
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

    def _learning_eligible_rejections(
        self, rows: list[RejectedOpportunityRecord]
    ) -> list[RejectedOpportunityRecord]:
        return [row for row in rows if bool(getattr(row, "learning_eligible", 0))]

    def _independent_rejections(
        self, rows: list[RejectedOpportunityRecord]
    ) -> list[RejectedOpportunityRecord]:
        independent: dict[str, RejectedOpportunityRecord] = {}
        for row in rows:
            key = str(getattr(row, "episode_key", None) or f"legacy-row:{row.id}")
            independent.setdefault(key, row)
        return list(independent.values())

    def _rejection_learning_filter_summary(
        self, rows: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        eligible = self._learning_eligible_rejections(rows)
        excluded = [
            row for row in rows if not bool(getattr(row, "learning_eligible", 0))
        ]
        return {
            "total_rejections": len(rows),
            "learning_eligible": len(eligible),
            "learning_excluded": len(excluded),
            "contexts": dict(
                Counter(
                    str(row.rejection_context or "unknown") for row in rows
                ).most_common()
            ),
            "sources": dict(
                Counter(
                    str(row.rejection_source or "unknown") for row in rows
                ).most_common()
            ),
            "market_sessions": dict(
                Counter(
                    str(row.market_session or "unknown") for row in rows
                ).most_common()
            ),
            "exclusion_reasons": dict(
                Counter(
                    str(row.learning_exclusion_reason or "unknown") for row in excluded
                ).most_common(20)
            ),
        }

    def _accepted_vs_rejected(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
    ) -> dict[str, Any]:
        rejections = self._independent_rejections(rejections)
        closed = [row for row in opportunities if row.status == "closed"]
        rejected_reviewed = [row for row in rejections if row.later_outcome]
        missed_winners = [
            row for row in rejected_reviewed if self._is_win(row.later_outcome)
        ]
        saved_losers = [
            row for row in rejected_reviewed if self._is_loss(row.later_outcome)
        ]
        return {
            "accepted": self._opportunity_summary(closed),
            "rejected": {
                "total": len(rejections),
                "reviewed_later": len(rejected_reviewed),
                "missed_winners": len(missed_winners),
                "saved_losers": len(saved_losers),
                "missed_winner_rate_pct": round(
                    (len(missed_winners) / len(rejected_reviewed)) * 100, 2
                )
                if rejected_reviewed
                else 0.0,
                "top_gates_on_missed_winners": dict(
                    Counter(
                        str(row.primary_gate or "unknown") for row in missed_winners
                    ).most_common(15)
                ),
            },
            "balance": {
                "accept_rate_pct": round(
                    (len(opportunities) / (len(opportunities) + len(rejections))) * 100,
                    2,
                )
                if opportunities or rejections
                else 0.0,
                "interpretation": self._filtering_interpretation(
                    opportunities, rejected_reviewed
                ),
            },
        }

    def _time_bucket_edge(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        trades: list[TradeRecord],
    ) -> dict[str, Any]:
        return {
            "accepted_opportunities": self._group_summary(
                opportunities, lambda row: time_bucket(row.created_at), "opportunity"
            ),
            "executed_trades": self._group_summary(
                trades, lambda row: time_bucket(row.created_at), "trade"
            ),
            "rejected_later_outcomes": self._group_summary(
                rejections, lambda row: time_bucket(row.created_at), "rejection"
            ),
        }

    def _dte_segmentation(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        trades: list[TradeRecord],
    ) -> dict[str, Any]:
        return {
            "accepted_opportunities": self._group_summary(
                opportunities, self._dte_bucket, "opportunity"
            ),
            "executed_trades": self._group_summary(trades, self._dte_bucket, "trade"),
            "rejected_later_outcomes": self._group_summary(
                rejections, self._dte_bucket, "rejection"
            ),
        }

    def _factor_attribution(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
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
            result[label] = self._summary_from_values(
                [sample[1] for sample in samples], [sample[2] for sample in samples]
            )
            result[label]["accepted_count"] = len(
                [sample for sample in samples if sample[0] == "accepted"]
            )
            result[label]["rejected_count"] = len(
                [sample for sample in samples if sample[0] == "rejected"]
            )
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
            if (
                float(trade.entry_price or 0.0) > 0
                and float(trade.average_price or 0.0) > 0
            ):
                deviations.append(
                    (
                        (
                            float(trade.average_price or 0.0)
                            - float(trade.entry_price or 0.0)
                        )
                        / float(trade.entry_price or 1.0)
                    )
                    * 100
                )
            if trade.status in {"closing", "exit_failed", "reconciliation_mismatch"}:
                stuck.append(self._trade_event(trade))
        return {
            "live_trades": len(live),
            "statuses": dict(statuses.most_common()),
            "price_sources": dict(price_sources.most_common()),
            "avg_entry_deviation_pct": round(sum(deviations) / len(deviations), 3)
            if deviations
            else 0.0,
            "max_abs_entry_deviation_pct": round(
                max(abs(value) for value in deviations), 3
            )
            if deviations
            else 0.0,
            "stuck_or_alert_trades": stuck[:20],
        }

    def _exit_policy_analytics(
        self, trades: list[TradeRecord], opportunities: list[OpportunityRecord]
    ) -> dict[str, Any]:
        closed_trades = [trade for trade in trades if trade.status == "closed"]
        closed_opportunities = [row for row in opportunities if row.status == "closed"]
        return {
            "trade_outcomes": dict(
                Counter(
                    str(trade.outcome or "unknown") for trade in closed_trades
                ).most_common()
            ),
            "opportunity_outcomes": dict(
                Counter(
                    str(row.outcome or "unknown") for row in closed_opportunities
                ).most_common()
            ),
            "partial_booking_rows": len(
                [trade for trade in trades if trade.partial_exit_json]
            ),
            "trailing_stop_rows": len(
                [
                    trade
                    for trade in trades
                    if str(trade.outcome or "").lower() == "trailing_stop"
                ]
            ),
            "time_stop_rows": len(
                [
                    trade
                    for trade in trades
                    if str(trade.outcome or "").lower() == "time_stop"
                ]
            ),
            "avg_time_to_confirm_exit_minutes": self._avg_exit_confirmation_minutes(
                closed_trades
            ),
        }

    def _no_trade_regime_detection(
        self, rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
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
            "top_unreviewed_gates": dict(
                Counter(
                    str(row.primary_gate or "unknown")
                    for row in rejections
                    if not row.later_outcome
                ).most_common(15)
            ),
        }

    def _strategy_versions(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
    ) -> dict[str, Any]:
        versions: Counter[str] = Counter()
        for row in opportunities:
            versions.update(
                [self._strategy_version(self._json(row.factor_scores_json))]
            )
        for row in rejections:
            versions.update(
                [self._strategy_version(self._json(row.factor_scores_json))]
            )
        return {
            "versions": dict(versions.most_common()),
            "warning": None
            if len(versions) <= 1
            else "Multiple strategy versions are mixed in this sample; compare them separately.",
        }

    def _shadow_mode_comparison(self, trades: list[TradeRecord]) -> dict[str, Any]:
        shadow = [trade for trade in trades if self._is_shadow_trade(trade)]
        paper = [
            trade for trade in trades if trade.mode == "paper" and trade not in shadow
        ]
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
        return sorted(
            events, key=lambda item: str(item.get("timestamp") or ""), reverse=True
        )[:limit]

    def _opportunity_summary(self, rows: list[OpportunityRecord]) -> dict[str, Any]:
        return self._summary_from_values(
            [float(row.pnl or 0.0) for row in rows], [row.outcome for row in rows]
        )

    def _trade_summary(self, rows: list[TradeRecord]) -> dict[str, Any]:
        summary = self._summary_from_values(
            [
                float(row.net_pnl if row.net_pnl is not None else row.pnl or 0.0)
                for row in rows
            ],
            [row.outcome for row in rows],
        )
        summary["mfe_mae"] = self._mfe_mae_summary(rows)
        return summary

    def _threshold_inventory(self) -> list[dict[str, Any]]:
        items = [
            (
                "min_signal_score",
                "entry",
                "blocks entry below minimum score",
                True,
                "scanner_service.py",
                "ScannerService",
                "generic",
            ),
            (
                "min_market_regime_score",
                "entry",
                "blocks weak market-regime score",
                True,
                "scanner_service.py",
                "ScannerService",
                "generic",
            ),
            (
                "min_price_action_score",
                "entry",
                "blocks weak price-action score",
                True,
                "scanner_service.py",
                "ScannerService",
                "generic",
            ),
            (
                "min_option_chain_score",
                "entry",
                "blocks weak option-chain score",
                True,
                "scanner_service.py",
                "ScannerService",
                "generic",
            ),
            (
                "min_option_liquidity_score",
                "option_selection",
                "blocks low option-liquidity score",
                True,
                "trade_setup_service.py",
                "TradeSetupService.risk_checks",
                "generic",
            ),
            (
                "max_bid_ask_spread_pct",
                "option_selection",
                "rejects wide bid/ask spread",
                True,
                "trade_setup_service.py",
                "TradeSetupService.risk_checks",
                "generic",
            ),
            (
                "min_option_volume",
                "option_selection",
                "rejects low option volume",
                True,
                "trade_setup_service.py",
                "TradeSetupService.risk_checks",
                "generic",
            ),
            (
                "min_option_oi",
                "option_selection",
                "rejects low option open interest",
                True,
                "trade_setup_service.py",
                "TradeSetupService.risk_checks",
                "generic",
            ),
            (
                "min_option_buy_premium",
                "option_selection",
                "rejects very low option premium",
                True,
                "trade_setup_service.py",
                "TradeSetupService.risk_checks",
                "generic",
            ),
            (
                "min_option_quality_score",
                "option_selection",
                "blocks poor Greeks/quality score",
                True,
                "option_quality_service.py",
                "OptionQualityService",
                "generic",
            ),
            (
                "min_option_buy_delta",
                "option_selection",
                "option buying delta lower bound",
                True,
                "option_quality_service.py",
                "OptionQualityService",
                "generic",
            ),
            (
                "max_option_buy_delta",
                "option_selection",
                "option buying delta upper bound",
                True,
                "option_quality_service.py",
                "OptionQualityService",
                "generic",
            ),
            (
                "max_option_buy_theta_pct",
                "option_selection",
                "rejects excessive theta decay",
                True,
                "option_quality_service.py",
                "OptionQualityService",
                "generic",
            ),
            (
                "min_option_buy_iv",
                "option_selection",
                "option IV lower bound",
                True,
                "option_quality_service.py",
                "OptionQualityService",
                "generic",
            ),
            (
                "max_option_buy_iv",
                "option_selection",
                "option IV upper bound",
                True,
                "option_quality_service.py",
                "OptionQualityService",
                "generic",
            ),
            (
                "min_risk_reward",
                "risk",
                "blocks poor risk-reward setup",
                True,
                "scanner_service.py",
                "ScannerService",
                "generic",
            ),
            (
                "max_risk_per_trade_pct",
                "risk",
                "position sizing risk cap",
                True,
                "trade_setup_service.py",
                "TradeSetupService.position_size",
                "generic",
            ),
            (
                "max_option_premium_pct",
                "risk",
                "budget cap for live option premium",
                True,
                "trade_setup_service.py",
                "TradeSetupService.risk_checks",
                "generic",
            ),
            (
                "max_daily_loss_pct",
                "risk",
                "daily loss guard",
                True,
                "risk_service.py",
                "RiskService",
                "generic",
            ),
            (
                "max_trades_per_day",
                "risk",
                "daily trade-count guard",
                True,
                "risk_service.py",
                "RiskService",
                "generic",
            ),
            (
                "max_stop_losses_per_day",
                "risk",
                "daily stop-loss guard",
                True,
                "risk_service.py",
                "RiskService",
                "generic",
            ),
            (
                "max_open_trades",
                "risk",
                "open trade limit",
                True,
                "risk_service.py",
                "RiskService",
                "generic",
            ),
            (
                "min_option_premium_confirmation_score",
                "entry",
                "premium confirmation hard gate",
                True,
                "option_premium_confirmation_service.py",
                "OptionPremiumConfirmationService",
                "generic",
            ),
            (
                "option_premium_lookback_candles",
                "entry",
                "premium confirmation lookback",
                True,
                "option_premium_confirmation_service.py",
                "OptionPremiumConfirmationService",
                "generic",
            ),
            (
                "max_premium_confirmation_candle_age_seconds",
                "data_quality",
                "premium candle freshness gate",
                True,
                "option_premium_confirmation_service.py",
                "OptionPremiumConfirmationService",
                "generic",
            ),
            (
                "option_quote_premium_mismatch_tolerance_pct",
                "data_quality",
                "quote/candle mismatch gate",
                True,
                "scanner_service.py",
                "ScannerService",
                "generic",
            ),
            (
                "min_day_type_score",
                "entry",
                "day-type filter threshold",
                True,
                "day_type_service.py",
                "DayTypeService",
                "generic",
            ),
            (
                "opening_range_minutes",
                "entry",
                "opening range classification window",
                True,
                "day_type_service.py",
                "DayTypeService",
                "generic",
            ),
            (
                "min_banknifty_regime_score",
                "entry",
                "Bank Nifty regime hard gate",
                True,
                "banknifty_regime_filter_service.py",
                "BankNiftyRegimeFilterService",
                "banknifty",
            ),
            (
                "banknifty_significant_gap_pct",
                "entry",
                "gap day context threshold",
                True,
                "banknifty_regime_filter_service.py",
                "BankNiftyRegimeFilterService",
                "banknifty",
            ),
            (
                "banknifty_compression_day_range_pct",
                "entry",
                "range compression threshold",
                True,
                "banknifty_regime_filter_service.py",
                "BankNiftyRegimeFilterService",
                "banknifty",
            ),
            (
                "banknifty_late_trade_cutoff_time",
                "entry",
                "late-day decay period start",
                True,
                "banknifty_regime_filter_service.py",
                "BankNiftyRegimeFilterService",
                "banknifty",
            ),
            (
                "banknifty_late_trade_min_premium_score",
                "entry",
                "late-day premium quality requirement",
                True,
                "banknifty_regime_filter_service.py",
                "BankNiftyRegimeFilterService",
                "banknifty",
            ),
            (
                "banknifty_expiry_min_premium_score",
                "entry",
                "expiry-day premium quality requirement",
                True,
                "banknifty_regime_filter_service.py",
                "BankNiftyRegimeFilterService",
                "banknifty",
            ),
            (
                "banknifty_top_bank_min_alignment",
                "entry",
                "top-bank alignment threshold",
                True,
                "banknifty_intelligence_service.py",
                "BankNiftyIntelligenceService",
                "banknifty",
            ),
            (
                "banknifty_top_bank_min_direction_count",
                "entry",
                "minimum aligned top-bank count",
                True,
                "banknifty_intelligence_service.py",
                "BankNiftyIntelligenceService",
                "banknifty",
            ),
            (
                "banknifty_expected_move_min_coverage",
                "entry",
                "expected move coverage threshold",
                True,
                "banknifty_intelligence_service.py",
                "BankNiftyIntelligenceService",
                "banknifty",
            ),
            (
                "min_volatility_edge_score",
                "entry",
                "volatility-edge score threshold",
                True,
                "volatility_edge_service.py",
                "VolatilityEdgeService",
                "banknifty",
            ),
            (
                "vol_edge_min_expected_move_coverage",
                "entry",
                "IV/ATR expected move coverage",
                True,
                "volatility_edge_service.py",
                "VolatilityEdgeService",
                "banknifty",
            ),
            (
                "vol_edge_max_iv_to_rv_ratio_for_buy",
                "entry",
                "overpriced IV vs realized-vol guard",
                True,
                "volatility_edge_service.py",
                "VolatilityEdgeService",
                "banknifty",
            ),
            (
                "max_entry_chase_pct",
                "entry_timing",
                "rejects chasing far above trigger",
                True,
                "entry_timing_service.py",
                "EntryTimingService",
                "generic",
            ),
            (
                "max_premium_move_from_base_pct",
                "entry_timing",
                "rejects overextended premium",
                True,
                "entry_timing_service.py",
                "EntryTimingService",
                "generic",
            ),
            (
                "min_remaining_risk_reward",
                "entry_timing",
                "blocks compressed post-breakout RR",
                True,
                "entry_timing_service.py",
                "EntryTimingService",
                "generic",
            ),
            (
                "min_target1_room_pct",
                "entry_timing",
                "requires target-1 room after entry",
                True,
                "entry_timing_service.py",
                "EntryTimingService",
                "generic",
            ),
            (
                "entry_armed_distance_to_trigger_pct",
                "entry_timing",
                "classifies setup as armed near trigger",
                True,
                "entry_timing_service.py",
                "EntryTimingService",
                "generic",
            ),
            (
                "min_entry_expected_move_coverage",
                "entry_timing",
                "entry timing expected move guard",
                True,
                "entry_timing_service.py",
                "EntryTimingService",
                "generic",
            ),
            (
                "min_entry_room_to_level_pct",
                "entry_timing",
                "entry timing room-to-level guard",
                True,
                "entry_timing_service.py",
                "EntryTimingService",
                "generic",
            ),
            (
                "option_time_stop_minutes",
                "exit",
                "time-stop duration",
                True,
                "trade_exit_service.py",
                "TradeExitService",
                "generic",
            ),
            (
                "option_time_stop_min_move_pct",
                "exit",
                "time-stop minimum progress",
                True,
                "trade_exit_service.py",
                "TradeExitService",
                "generic",
            ),
            (
                "option_trailing_stop_lock_pct",
                "exit",
                "trailing stop lock after target reach",
                True,
                "trade_exit_service.py",
                "TradeExitService",
                "generic",
            ),
            (
                "exit_open_trades_before_close_minutes",
                "exit",
                "near-close square-off window",
                True,
                "trade_exit_service.py",
                "TradeExitService",
                "generic",
            ),
        ]
        inventory = []
        for name, category, decision, tracked, file_name, function_name, scope in items:
            inventory.append(
                {
                    "file_path": f"app/services/{file_name}"
                    if file_name != "scanner_service.py"
                    else "app/services/scanner_service.py",
                    "class_or_function": function_name,
                    "threshold_name": name,
                    "current_value": getattr(settings, name, None),
                    "configurable": hasattr(settings, name),
                    "where_used": function_name,
                    "decision_affect": decision,
                    "effect_type": self._threshold_effect_type(category),
                    "scope": scope,
                    "currently_tracked_in_reports_or_db": tracked,
                }
            )
        inventory.extend(
            [
                {
                    "file_path": "app/services/trade_setup_service.py",
                    "class_or_function": "TradeSetupService.liquidity_score/_first_target/_banknifty_premium_risk_pct",
                    "threshold_name": "hardcoded_liquidity_and_premium_structure_cutoffs",
                    "current_value": "volume>=1000, spread<=2%/5%, adx>=22/24/25, risk 16%-28%, target R 1.20/1.45/1.65",
                    "configurable": False,
                    "where_used": "option scoring, SL/target derivation",
                    "decision_affect": "option_selection_and_risk",
                    "effect_type": "changes score/risk",
                    "scope": "banknifty",
                    "currently_tracked_in_reports_or_db": "partially via factor_scores/prices, not every intermediate cutoff",
                },
                {
                    "file_path": "app/services/volatility_edge_service.py",
                    "class_or_function": "VolatilityEdgeService._score/_classification",
                    "threshold_name": "hardcoded_volatility_scoring_cutoffs",
                    "current_value": "iv/vix change +/-1%, score bands 65/80, IV rank 20/80",
                    "configurable": False,
                    "where_used": "volatility score/context",
                    "decision_affect": "score/context or hard gate when enabled",
                    "effect_type": "changes score",
                    "scope": "banknifty",
                    "currently_tracked_in_reports_or_db": "partially via volatility_edge factor JSON",
                },
            ]
        )
        return inventory

    def _threshold_effect_type(self, category: str) -> str:
        if category in {"entry", "entry_timing", "data_quality"}:
            return "blocks entry/rejects trade"
        if category == "option_selection":
            return "selects or rejects option contract"
        if category == "risk":
            return "changes risk/position sizing or blocks trade"
        if category == "exit":
            return "manages exit"
        return "changes score/context"

    def _threshold_data_support(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        trades: list[TradeRecord],
    ) -> dict[str, Any]:
        return {
            "accepted_vs_rejected": bool(opportunities or rejections),
            "score_at_decision_time": any(
                getattr(row, "score", None) is not None
                for row in [*opportunities, *rejections]
            ),
            "setup_family": any(
                self._setup_family(row) != "unknown_setup"
                for row in [*opportunities, *rejections, *trades]
            ),
            "rejection_reason": any(row.reasons_json for row in rejections),
            "entry_reason": any(
                self._row_factor_scores(row) for row in [*opportunities, *trades]
            ),
            "exit_reason": any(row.outcome for row in trades),
            "time_bucket": any(
                row.created_at for row in [*opportunities, *rejections, *trades]
            ),
            "expiry_bucket": any(
                getattr(row, "expiry", None) for row in [*opportunities, *rejections]
            ),
            "trend_or_regime_bucket": any(
                self._trend_regime(row) != "unknown_trend_regime"
                for row in [*opportunities, *rejections, *trades]
            ),
            "spread_volume_oi": any(
                self._has_option_quality_payload(row)
                for row in [*opportunities, *rejections]
            ),
            "premium_confirmation_state": any(
                self._nested(
                    self._row_factor_scores(row), "option_premium_confirmation"
                )
                for row in [*opportunities, *rejections, *trades]
            ),
            "risk_reward": any(
                float(getattr(row, "risk_reward", 0.0) or 0.0) > 0
                for row in opportunities
            ),
            "realized_pnl": any(
                self._accepted_value(row) != 0 for row in [*opportunities, *trades]
            ),
            "mfe_mae": any(
                row.mfe_points is not None or row.mae_points is not None
                for row in trades
            ),
            "net_pnl_after_costs": any(row.net_pnl is not None for row in trades),
        }

    def _score_threshold_validation(
        self,
        decision_rows: list[Any],
        accepted_rows: list[Any],
        reviewed_rejections: list[RejectedOpportunityRecord],
    ) -> dict[str, Any]:
        min_score = float(settings.min_signal_score)
        buckets = {
            "below_threshold": lambda score: score < max(0.0, min_score - 5),
            "just_below_threshold": lambda score: (
                max(0.0, min_score - 5) <= score < min_score
            ),
            "just_above_threshold": lambda score: min_score <= score < min_score + 5,
            "high_score": lambda score: min_score + 5 <= score < min_score + 15,
            "very_high_score": lambda score: score >= min_score + 15,
        }
        report: dict[str, Any] = {}
        for label, predicate in buckets.items():
            decisions = [
                row for row in decision_rows if predicate(self._row_score(row))
            ]
            accepted = [row for row in accepted_rows if predicate(self._row_score(row))]
            rejected = [
                row for row in reviewed_rejections if predicate(self._row_score(row))
            ]
            values = [self._accepted_value(row) for row in accepted] + [
                self._rejected_move_value(row) for row in rejected
            ]
            outcomes = [getattr(row, "outcome", None) for row in accepted] + [
                row.later_outcome for row in rejected
            ]
            report[label] = {
                "score_range": self._score_bucket_range(label, min_score),
                "total_opportunities": len(decisions),
                "accepted_count": len(
                    [row for row in decisions if isinstance(row, OpportunityRecord)]
                ),
                "rejected_count": len(
                    [
                        row
                        for row in decisions
                        if isinstance(row, RejectedOpportunityRecord)
                    ]
                ),
                **self._summary_from_values(values, outcomes),
                "mfe_mae": self._mfe_mae_summary(
                    [row for row in accepted if isinstance(row, TradeRecord)]
                ),
                "verdict": self._verdict_from_summary(
                    len(values), self._summary_from_values(values, outcomes)
                ),
            }
        return {
            "current_min_signal_score": settings.min_signal_score,
            "buckets": report,
        }

    def _score_bucket_range(self, label: str, min_score: float) -> str:
        ranges = {
            "below_threshold": f"< {max(0.0, min_score - 5):.0f}",
            "just_below_threshold": f"{max(0.0, min_score - 5):.0f} to < {min_score:.0f}",
            "just_above_threshold": f"{min_score:.0f} to < {min_score + 5:.0f}",
            "high_score": f"{min_score + 5:.0f} to < {min_score + 15:.0f}",
            "very_high_score": f">= {min_score + 15:.0f}",
        }
        return ranges.get(label, "unknown")

    def _rejection_threshold_validation(
        self, rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        groups: dict[str, list[RejectedOpportunityRecord]] = defaultdict(list)
        for row in rejections:
            labels = self._json_list(row.reasons_json) or [
                str(row.primary_gate or "unknown_gate")
            ]
            for label in labels:
                groups[self._clean_label(label)].append(row)
        rows = []
        for label, items in groups.items():
            reviewed = [row for row in items if row.later_outcome]
            missed = [row for row in reviewed if self._is_win(row.later_outcome)]
            saved = [row for row in reviewed if self._is_loss(row.later_outcome)]
            missed_pnl = sum(max(0.0, self._rejected_move_value(row)) for row in missed)
            saved_pnl = abs(
                sum(min(0.0, self._rejected_move_value(row)) for row in saved)
            )
            summary = self._summary_from_values(
                [self._rejected_move_value(row) for row in reviewed],
                [row.later_outcome for row in reviewed],
            )
            rows.append(
                {
                    "gate_or_reason": label,
                    "rejected_count": len(items),
                    "reviewed_count": len(reviewed),
                    "later_winner_count": len(missed),
                    "later_loser_count": len(saved),
                    "estimated_pnl_missed": round(missed_pnl, 2),
                    "estimated_pnl_saved": round(saved_pnl, 2),
                    "summary_if_rejected_had_been_taken": summary,
                    "usefulness": self._filter_usefulness(
                        len(reviewed), len(missed), len(saved), missed_pnl, saved_pnl
                    ),
                }
            )
        return {
            "rows": sorted(
                rows,
                key=lambda row: (
                    -int(row["rejected_count"]),
                    str(row["gate_or_reason"]),
                ),
            ),
            "note": "P&L saved/missed is a proxy unless later_exit_price is populated for rejected rows.",
        }

    def _setup_family_threshold_validation(
        self, accepted_rows: list[Any], rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        keys = sorted(
            {self._setup_family(row) for row in [*accepted_rows, *rejections]}
        )
        report: dict[str, Any] = {}
        for key in keys:
            accepted = [row for row in accepted_rows if self._setup_family(row) == key]
            rejected = [row for row in rejections if self._setup_family(row) == key]
            reviewed = [row for row in rejected if row.later_outcome]
            values = [self._accepted_value(row) for row in accepted] + [
                self._rejected_move_value(row) for row in reviewed
            ]
            outcomes = [getattr(row, "outcome", None) for row in accepted] + [
                row.later_outcome for row in reviewed
            ]
            report[key] = {
                "total_opportunities": len(accepted) + len(rejected),
                "accepted_trades": len(accepted),
                "rejected_opportunities": len(rejected),
                **self._summary_from_values(values, outcomes),
                "mfe_mae": self._mfe_mae_summary(
                    [row for row in accepted if isinstance(row, TradeRecord)]
                ),
                "best_score_range": self._best_group(
                    accepted, lambda row: score_bucket(self._row_score(row))
                ),
                "worst_score_range": self._worst_group(
                    accepted, lambda row: score_bucket(self._row_score(row))
                ),
                "best_time_window": self._best_group(
                    accepted, lambda row: time_bucket(getattr(row, "created_at", None))
                ),
                "worst_time_window": self._worst_group(
                    accepted, lambda row: time_bucket(getattr(row, "created_at", None))
                ),
                "expiry_day": self._summary_for_rows(
                    [row for row in accepted if self._dte_bucket(row) == "expiry_day"]
                ),
                "non_expiry": self._summary_for_rows(
                    [row for row in accepted if self._dte_bucket(row) != "expiry_day"]
                ),
                "strictness_verdict": self._strictness_verdict(accepted, reviewed),
            }
        return report

    def _time_and_regime_validation(
        self,
        accepted_rows: list[Any],
        reviewed_rejections: list[RejectedOpportunityRecord],
    ) -> dict[str, Any]:
        return {
            "time_buckets": self._segment_report(
                accepted_rows,
                reviewed_rejections,
                lambda row: time_bucket(getattr(row, "created_at", None)),
            ),
            "opening_windows": self._segment_report(
                accepted_rows, reviewed_rejections, self._opening_window_bucket
            ),
            "expiry": self._segment_report(
                accepted_rows, reviewed_rejections, self._dte_bucket
            ),
            "trend_regime": self._segment_report(
                accepted_rows, reviewed_rejections, self._trend_regime
            ),
            "iv_regime": self._segment_report(
                accepted_rows, reviewed_rejections, self._iv_regime
            ),
        }

    def _score_sensitivity(
        self,
        decision_rows: list[Any],
        accepted_rows: list[Any],
        reviewed_rejections: list[RejectedOpportunityRecord],
    ) -> list[dict[str, Any]]:
        candidates = [60, 65, 70, 75, 80, 85, int(settings.min_signal_score)]
        rows = []
        for candidate in sorted(set(candidates)):
            accepted = [
                row for row in accepted_rows if self._row_score(row) >= candidate
            ]
            rejected = [
                row for row in reviewed_rejections if self._row_score(row) >= candidate
            ]
            values = [self._accepted_value(row) for row in accepted] + [
                self._rejected_move_value(row) for row in rejected
            ]
            outcomes = [getattr(row, "outcome", None) for row in accepted] + [
                row.later_outcome for row in rejected
            ]
            summary = self._summary_from_values(values, outcomes)
            rows.append(
                {
                    "candidate_min_score": candidate,
                    "current_live_value": candidate == int(settings.min_signal_score),
                    "decision_rows_at_or_above": len(
                        [
                            row
                            for row in decision_rows
                            if self._row_score(row) >= candidate
                        ]
                    ),
                    "trade_count": summary["trades"],
                    "win_rate_pct": summary["win_rate_pct"],
                    "net_expectancy": summary["expectancy"],
                    "profit_factor": summary["profit_factor"],
                    "drawdown": summary["max_drawdown"],
                    "average_trade_quality": self._average_score(
                        [*accepted, *rejected]
                    ),
                    "sample_enough": summary["trades"] >= 30,
                    "verdict": self._verdict_from_summary(summary["trades"], summary),
                }
            )
        return rows

    def _threshold_verdicts(
        self, accepted_rows: list[Any], rejections: list[RejectedOpportunityRecord]
    ) -> list[dict[str, Any]]:
        score_summary = self._summary_for_rows(accepted_rows)
        rejected_reviewed = [row for row in rejections if row.later_outcome]
        return [
            {
                "threshold_name": "min_signal_score",
                "current_value": settings.min_signal_score,
                "sample_size": score_summary["trades"],
                "verdict": self._verdict_from_summary(
                    score_summary["trades"], score_summary
                ),
                "reason": "Based on closed accepted trades and reviewed rejected setups around score buckets.",
                "recommended_action": "monitor longer"
                if score_summary["trades"] < 30
                else "keep unchanged until sensitivity shows stable improvement",
            },
            {
                "threshold_name": "rejection_gates",
                "current_value": "multiple",
                "sample_size": len(rejected_reviewed),
                "verdict": "NOT_ENOUGH_DATA"
                if len(rejected_reviewed) < 30
                else "INCONCLUSIVE",
                "reason": "Needs rejected-opportunity later outcomes before filters can be proven useful or harmful.",
                "recommended_action": "run rejected-outcome evaluation after sessions",
            },
        ]

    def _threshold_not_measurable_yet(
        self,
        opportunities: list[OpportunityRecord],
        rejections: list[RejectedOpportunityRecord],
        trades: list[TradeRecord],
    ) -> list[dict[str, str]]:
        missing = []
        if not any(
            self._has_option_quality_payload(row)
            for row in [*opportunities, *rejections]
        ):
            missing.append(
                {
                    "threshold_area": "spread_volume_oi",
                    "reason": "option quality/liquidity payload missing in older factor JSON rows",
                }
            )
        if not any(row.later_outcome for row in rejections):
            missing.append(
                {
                    "threshold_area": "rejected_trade_later_outcome",
                    "reason": "rejected opportunities need later_outcome before filter usefulness can be validated",
                }
            )
        if not any(row.mfe_points is not None for row in trades):
            missing.append(
                {
                    "threshold_area": "mfe_mae",
                    "reason": "older trades predate first-class MFE/MAE tracking",
                }
            )
        return missing

    def _mfe_mae_summary(self, rows: list[TradeRecord]) -> dict[str, Any]:
        tracked = [
            row
            for row in rows
            if row.mfe_points is not None or row.mae_points is not None
        ]
        mfe_points = [float(row.mfe_points or 0.0) for row in tracked]
        mae_points = [float(row.mae_points or 0.0) for row in tracked]
        mfe_pct = [float(row.mfe_percent or 0.0) for row in tracked]
        mae_pct = [float(row.mae_percent or 0.0) for row in tracked]
        captured = [
            value
            for row in tracked
            if (value := self._captured_mfe_percent(row)) is not None
        ]
        by_setup: dict[str, list[TradeRecord]] = defaultdict(list)
        for row in tracked:
            by_setup[self._setup_family(row)].append(row)
        return {
            "tracked_trades": len(tracked),
            "missing_trades": len(rows) - len(tracked),
            "avg_mfe_points": round(sum(mfe_points) / len(mfe_points), 3)
            if mfe_points
            else 0.0,
            "avg_mae_points": round(sum(mae_points) / len(mae_points), 3)
            if mae_points
            else 0.0,
            "avg_mfe_percent": round(sum(mfe_pct) / len(mfe_pct), 3)
            if mfe_pct
            else 0.0,
            "avg_mae_percent": round(sum(mae_pct) / len(mae_pct), 3)
            if mae_pct
            else 0.0,
            "avg_captured_mfe_percent": round(sum(captured) / len(captured), 3)
            if captured
            else 0.0,
            "by_setup_family": {
                key: self._mfe_mae_group_summary(items)
                for key, items in sorted(by_setup.items())
            },
        }

    def _mfe_mae_group_summary(self, rows: list[TradeRecord]) -> dict[str, Any]:
        captured = [
            value
            for row in rows
            if (value := self._captured_mfe_percent(row)) is not None
        ]
        return {
            "trades": len(rows),
            "avg_mfe_percent": round(
                sum(float(row.mfe_percent or 0.0) for row in rows) / len(rows), 3
            )
            if rows
            else 0.0,
            "avg_mae_percent": round(
                sum(float(row.mae_percent or 0.0) for row in rows) / len(rows), 3
            )
            if rows
            else 0.0,
            "avg_captured_mfe_percent": round(sum(captured) / len(captured), 3)
            if captured
            else 0.0,
        }

    def _captured_mfe_percent(self, row: TradeRecord) -> float | None:
        try:
            entry = float(row.average_price or row.entry_price or 0.0)
            exit_price = float(row.exit_price or 0.0)
            mfe = float(row.mfe_points or 0.0)
        except (TypeError, ValueError):
            return None
        if entry <= 0 or exit_price <= 0 or mfe <= 0:
            return None
        realized = exit_price - entry
        if str(row.side or "BUY").upper() == "SELL":
            realized *= -1
        return round((max(0.0, realized) / mfe) * 100, 3)

    def _filter_threshold_rows(
        self,
        rows: list[Any],
        *,
        start_date: date | None,
        end_date: date | None,
        setup_family: str | None,
    ) -> list[Any]:
        result = []
        family_filter = str(setup_family or "").strip().lower()
        for row in rows:
            created_at = getattr(row, "created_at", None)
            if start_date and (not created_at or created_at.date() < start_date):
                continue
            if end_date and (not created_at or created_at.date() > end_date):
                continue
            if family_filter and self._setup_family(row) != family_filter:
                continue
            result.append(row)
        return result

    def _filter_trades_by_mode(
        self, trades: list[TradeRecord], mode: str
    ) -> list[TradeRecord]:
        normalized = str(mode or "all").lower()
        if normalized in {"all", ""}:
            return trades
        if normalized == "shadow":
            return [trade for trade in trades if self._is_shadow_trade(trade)]
        return [
            trade for trade in trades if str(trade.mode or "").lower() == normalized
        ]

    def _row_score(self, row: Any) -> float:
        score = getattr(row, "score", None)
        if score is None and isinstance(row, TradeRecord):
            payload = self._json(row.order_response_json)
            score = payload.get("score") or payload.get("signal_score")
            factors = self._row_factor_scores(row)
            score = score if score is not None else factors.get("score")
        try:
            return float(score or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _summary_for_rows(self, rows: list[Any]) -> dict[str, Any]:
        return self._summary_from_values(
            [self._accepted_value(row) for row in rows],
            [getattr(row, "outcome", None) for row in rows],
        )

    def _best_group(self, rows: list[Any], key_fn: Any) -> dict[str, Any] | None:
        return self._rank_group(rows, key_fn, reverse=True)

    def _worst_group(self, rows: list[Any], key_fn: Any) -> dict[str, Any] | None:
        return self._rank_group(rows, key_fn, reverse=False)

    def _rank_group(
        self, rows: list[Any], key_fn: Any, *, reverse: bool
    ) -> dict[str, Any] | None:
        groups: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            groups[str(key_fn(row))].append(row)
        ranked = []
        for key, items in groups.items():
            summary = self._summary_for_rows(items)
            ranked.append(
                (
                    float(summary["expectancy"]),
                    float(summary["win_rate_pct"]),
                    key,
                    summary,
                )
            )
        if not ranked:
            return None
        ranked.sort(reverse=reverse)
        _, _, key, summary = ranked[0]
        return {"bucket": key, **summary}

    def _average_score(self, rows: list[Any]) -> float:
        values = [self._row_score(row) for row in rows if self._row_score(row) > 0]
        return round(sum(values) / len(values), 2) if values else 0.0

    def _filter_usefulness(
        self,
        reviewed_count: int,
        missed_winners: int,
        saved_losers: int,
        missed_pnl: float,
        saved_pnl: float,
    ) -> str:
        if reviewed_count < 10:
            return "NOT_ENOUGH_DATA"
        if saved_losers > missed_winners * 1.5 and saved_pnl >= missed_pnl:
            return "LIKELY_USEFUL"
        if missed_winners > saved_losers * 1.5 and missed_pnl > saved_pnl:
            return "LIKELY_HARMFUL"
        return "INCONCLUSIVE"

    def _strictness_verdict(
        self, accepted: list[Any], reviewed_rejections: list[RejectedOpportunityRecord]
    ) -> str:
        accepted_summary = self._summary_for_rows(accepted)
        missed = len(
            [row for row in reviewed_rejections if self._is_win(row.later_outcome)]
        )
        saved = len(
            [row for row in reviewed_rejections if self._is_loss(row.later_outcome)]
        )
        sample = int(accepted_summary["trades"]) + len(reviewed_rejections)
        if sample < 30:
            return "NOT_ENOUGH_DATA"
        if float(accepted_summary["expectancy"]) > 0 and saved >= missed:
            return "LIKELY_BALANCED_OR_USEFUL"
        if missed > saved * 1.5:
            return "LIKELY_TOO_STRICT"
        if float(accepted_summary["expectancy"]) < 0 and saved < missed:
            return "LIKELY_TOO_LOOSE"
        return "INCONCLUSIVE"

    def _verdict_from_summary(self, sample_size: int, summary: dict[str, Any]) -> str:
        if sample_size < 10:
            return "NOT_ENOUGH_DATA"
        expectancy = float(summary.get("expectancy") or 0.0)
        profit_factor = summary.get("profit_factor")
        win_rate = float(summary.get("win_rate_pct") or 0.0)
        pf = float(profit_factor) if profit_factor is not None else 0.0
        if sample_size >= 30 and expectancy > 0 and (pf >= 1.2 or win_rate >= 50):
            return "PROVEN_USEFUL"
        if expectancy > 0:
            return "LIKELY_USEFUL"
        if sample_size >= 30 and expectancy < 0 and pf < 1.0:
            return "PROVEN_HARMFUL"
        if expectancy < 0:
            return "LIKELY_HARMFUL"
        return "INCONCLUSIVE"

    def _opening_window_bucket(self, row: Any) -> str:
        created_at = getattr(row, "created_at", None)
        if not created_at:
            return "unknown"
        total = created_at.hour * 60 + created_at.minute
        open_minute = 9 * 60 + 15
        if total < open_minute + 5:
            return "opening_5_min"
        if total < open_minute + 15:
            return "opening_15_min"
        if total < open_minute + 30:
            return "opening_30_min"
        if total >= 14 * 60 + 45:
            return "late_day_decay"
        return time_bucket(created_at)

    def _has_option_quality_payload(self, row: Any) -> bool:
        payload = self._json(getattr(row, "option_quality_json", None))
        if payload:
            return True
        factors = self._row_factor_scores(row)
        contract = factors.get("contract") if isinstance(factors, dict) else None
        if isinstance(contract, dict):
            return any(
                contract.get(key) is not None
                for key in ("bid", "ask", "volume", "open_interest")
            )
        return False

    def _group_summary(
        self, rows: Iterable[Any], key_fn: Any, row_type: str
    ) -> dict[str, Any]:
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

    def _filter_rejection_quality(
        self, rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        by_filter: dict[str, list[RejectedOpportunityRecord]] = defaultdict(list)
        for row in rejections:
            reasons = self._json_list(row.reasons_json) or [
                str(row.primary_gate or "unknown_filter")
            ]
            for reason in reasons:
                by_filter[self._clean_label(reason)].append(row)
        rows: list[dict[str, Any]] = []
        for label, items in by_filter.items():
            reviewed = [row for row in items if row.later_outcome]
            missed_winners = [
                row for row in reviewed if self._is_win(row.later_outcome)
            ]
            saved_losers = [row for row in reviewed if self._is_loss(row.later_outcome)]
            values = [self._rejected_move_value(row) for row in reviewed]
            summary = self._summary_from_values(
                values, [row.later_outcome for row in reviewed]
            )
            rows.append(
                {
                    "filter_name": label,
                    "rejected_count": len(items),
                    "reviewed_count": len(reviewed),
                    "later_winner_count": len(missed_winners),
                    "later_loser_count": len(saved_losers),
                    "missed_winner_rate_pct": round(
                        (len(missed_winners) / len(reviewed)) * 100, 2
                    )
                    if reviewed
                    else 0.0,
                    "saved_loser_rate_pct": round(
                        (len(saved_losers) / len(reviewed)) * 100, 2
                    )
                    if reviewed
                    else 0.0,
                    "rejected_expectancy_proxy": summary["expectancy"],
                    "rejected_profit_factor_proxy": summary["profit_factor"],
                    "examples": [self._rejection_event(row) for row in reviewed[:3]],
                }
            )
        rows = sorted(
            rows,
            key=lambda item: (
                -int(item["rejected_count"]),
                -float(item["missed_winner_rate_pct"]),
                str(item["filter_name"]),
            ),
        )
        return {
            "filters": rows,
            "top_rejecting_filters": rows[:10],
            "possible_overfilters": [
                row
                for row in rows
                if int(row["reviewed_count"]) >= 3
                and float(row["missed_winner_rate_pct"]) >= 40.0
            ][:10],
            "needs_more_outcome_review": [
                row
                for row in rows
                if int(row["rejected_count"]) >= 5 and int(row["reviewed_count"]) < 3
            ][:10],
        }

    def _rejected_quality_summary(
        self, rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        observation_count = len(rejections)
        rejections = self._independent_rejections(rejections)
        reviewed = [row for row in rejections if row.later_outcome]
        missed = [row for row in reviewed if self._is_win(row.later_outcome)]
        saved = [row for row in reviewed if self._is_loss(row.later_outcome)]
        ambiguous = [row for row in reviewed if self._is_ambiguous(row)]
        unresolved = [row for row in rejections if not row.later_outcome]
        outcome_minutes = [
            float(row.later_outcome_minutes)
            for row in reviewed
            if row.later_outcome_minutes is not None
        ]
        return {
            "total_rejected": len(rejections),
            "observation_count": observation_count,
            "dependent_duplicate_count": observation_count - len(rejections),
            "reviewed": len(reviewed),
            "unresolved": len(unresolved),
            "missed_winners": len(missed),
            "saved_losers": len(saved),
            "ambiguous": len(ambiguous),
            "review_coverage_pct": round((len(reviewed) / len(rejections)) * 100, 2)
            if rejections
            else 0.0,
            "missed_winner_rate_pct": round((len(missed) / len(reviewed)) * 100, 2)
            if reviewed
            else 0.0,
            "saved_loser_rate_pct": round((len(saved) / len(reviewed)) * 100, 2)
            if reviewed
            else 0.0,
            "ambiguous_rate_pct": round((len(ambiguous) / len(reviewed)) * 100, 2)
            if reviewed
            else 0.0,
            "avg_minutes_to_outcome": round(
                sum(outcome_minutes) / len(outcome_minutes), 2
            )
            if outcome_minutes
            else None,
            "outcomes": dict(
                Counter(
                    str(row.later_outcome or "unresolved") for row in rejections
                ).most_common()
            ),
            "sources": dict(
                Counter(
                    str(row.later_outcome_source or "unknown") for row in reviewed
                ).most_common()
            ),
            "timeframes": dict(
                Counter(
                    str(row.later_outcome_timeframe or "unknown") for row in reviewed
                ).most_common()
            ),
        }

    def _gate_effectiveness_rows(
        self, rejections: list[RejectedOpportunityRecord]
    ) -> list[dict[str, Any]]:
        groups: dict[str, list[RejectedOpportunityRecord]] = defaultdict(list)
        for row in rejections:
            labels = [str(row.primary_gate or "unknown_gate")]
            labels.extend(self._json_list(row.reasons_json))
            for label in list(
                dict.fromkeys(self._clean_label(item) for item in labels if item)
            ):
                groups[label].append(row)
        rows: list[dict[str, Any]] = []
        for gate, items in groups.items():
            independent_by_episode: dict[str, RejectedOpportunityRecord] = {}
            for row in items:
                episode_key = str(
                    getattr(row, "episode_key", None) or f"legacy-row:{row.id}"
                )
                independent_by_episode.setdefault(episode_key, row)
            independent = list(independent_by_episode.values())
            reviewed = [row for row in independent if row.later_outcome]
            missed = [row for row in reviewed if self._is_win(row.later_outcome)]
            saved = [row for row in reviewed if self._is_loss(row.later_outcome)]
            ambiguous = [row for row in reviewed if self._is_ambiguous(row)]
            unresolved = [row for row in independent if not row.later_outcome]
            moves = [
                self._rejected_move_value(row)
                for row in reviewed
                if not self._is_ambiguous(row)
            ]
            minutes = [
                float(row.later_outcome_minutes)
                for row in reviewed
                if row.later_outcome_minutes is not None
            ]
            primary_count = len(
                [
                    row
                    for row in independent
                    if self._clean_label(str(row.primary_gate or "unknown_gate"))
                    == gate
                ]
            )
            isolated_count = len(
                [
                    row
                    for row in independent
                    if len(
                        set(
                            self._clean_label(item)
                            for item in self._json_list(row.reasons_json)
                            if item
                        )
                    )
                    <= 1
                ]
            )
            rows.append(
                {
                    "gate_or_reason": gate,
                    "count": len(independent),
                    "observation_count": len(items),
                    "independent_episode_count": len(independent),
                    "dependent_duplicate_count": len(items) - len(independent),
                    "primary_gate_count": primary_count,
                    "co_occurring_reason_count": len(independent) - primary_count,
                    "isolated_evidence_count": isolated_count,
                    "reviewed": len(reviewed),
                    "missed_winners": len(missed),
                    "saved_losers": len(saved),
                    "ambiguous": len(ambiguous),
                    "unresolved": len(unresolved),
                    "missed_winner_rate_pct": round(
                        (len(missed) / len(reviewed)) * 100, 2
                    )
                    if reviewed
                    else 0.0,
                    "saved_loser_rate_pct": round((len(saved) / len(reviewed)) * 100, 2)
                    if reviewed
                    else 0.0,
                    "unresolved_rate_pct": round(
                        (len(unresolved) / len(independent)) * 100, 2
                    )
                    if independent
                    else 0.0,
                    "ambiguous_rate_pct": round(
                        (len(ambiguous) / len(reviewed)) * 100, 2
                    )
                    if reviewed
                    else 0.0,
                    "avg_move_after_rejection": round(sum(moves) / len(moves), 2)
                    if moves
                    else 0.0,
                    "avg_minutes_to_outcome": round(sum(minutes) / len(minutes), 2)
                    if minutes
                    else None,
                    "evidence_quality": self._gate_evidence_quality(
                        len(independent), len(reviewed), len(ambiguous)
                    ),
                    "interpretation": self._gate_effectiveness_interpretation(
                        len(reviewed), len(missed), len(saved), len(ambiguous)
                    ),
                    "causal_claim": "not_established_observational_episode_evidence_only",
                }
            )
        return sorted(
            rows,
            key=lambda item: (
                -int(item["count"]),
                -float(item["saved_loser_rate_pct"]),
                float(item["missed_winner_rate_pct"]),
                str(item["gate_or_reason"]),
            ),
        )

    def _top_gates(
        self, rejections: list[RejectedOpportunityRecord], *, outcome_group: str
    ) -> dict[str, int]:
        if outcome_group == "missed_winner":
            rows = [row for row in rejections if self._is_win(row.later_outcome)]
        elif outcome_group == "saved_loser":
            rows = [row for row in rejections if self._is_loss(row.later_outcome)]
        elif outcome_group == "unresolved":
            rows = [row for row in rejections if not row.later_outcome]
        else:
            rows = []
        return dict(
            Counter(str(row.primary_gate or "unknown") for row in rows).most_common(15)
        )

    def _accepted_rejected_comparison(
        self, accepted_rows: list[Any], rejections: list[RejectedOpportunityRecord]
    ) -> dict[str, Any]:
        reviewed = [
            row
            for row in rejections
            if row.later_outcome and not self._is_ambiguous(row)
        ]
        accepted = self._summary_for_rows(accepted_rows)
        rejected_values = [self._rejected_move_value(row) for row in reviewed]
        rejected = self._summary_from_values(
            rejected_values, [row.later_outcome for row in reviewed]
        )
        return {
            "accepted_closed_count": accepted.get("trades", 0),
            "rejected_reviewed_count": len(reviewed),
            "accepted_expectancy": accepted.get("expectancy"),
            "rejected_if_taken_expectancy": rejected.get("expectancy"),
            "rejected_win_rate_pct": rejected.get("win_rate_pct"),
            "interpretation": self._accepted_rejected_interpretation(
                accepted, rejected
            ),
        }

    def _gate_evidence_quality(self, total: int, reviewed: int, ambiguous: int) -> str:
        if total < 10 or reviewed < 5:
            return "LOW_SAMPLE"
        if ambiguous / max(reviewed, 1) > 0.25:
            return "AMBIGUOUS"
        if reviewed / max(total, 1) < 0.5:
            return "PARTIAL_REVIEW"
        return "USABLE"

    def _gate_effectiveness_interpretation(
        self, reviewed: int, missed: int, saved: int, ambiguous: int
    ) -> str:
        if reviewed < 5:
            return "needs_more_review"
        if ambiguous / max(reviewed, 1) > 0.25:
            return "ambiguous_intracandle_path"
        missed_rate = missed / max(reviewed, 1)
        saved_rate = saved / max(reviewed, 1)
        if saved_rate >= 0.6 and missed_rate <= 0.2:
            return "useful_filter"
        if missed_rate >= 0.4:
            return "possible_overfilter"
        return "mixed_or_inconclusive"

    def _accepted_rejected_interpretation(
        self, accepted: dict[str, Any], rejected: dict[str, Any]
    ) -> str:
        accepted_expectancy = float(accepted.get("expectancy") or 0.0)
        rejected_expectancy = float(rejected.get("expectancy") or 0.0)
        rejected_trades = int(rejected.get("trades") or 0)
        if rejected_trades < 10:
            return "not_enough_rejected_outcomes"
        if rejected_expectancy > accepted_expectancy and rejected_expectancy > 0:
            return "rejected_setups_need_review"
        if rejected_expectancy < 0 <= accepted_expectancy:
            return "filters_currently_helpful"
        return "mixed_or_inconclusive"

    def _accepted_loss_impact(
        self,
        trades: list[TradeRecord],
        opportunities: list[OpportunityRecord],
    ) -> dict[str, Any]:
        rows: list[Any] = trades or opportunities
        losers = [
            row
            for row in rows
            if self._accepted_value(row) < 0
            or self._is_loss(getattr(row, "outcome", None))
        ]
        worst = sorted(losers, key=self._accepted_value)[:10]
        by_reason = self._segment_accepted(rows, self._loss_driver)
        return {
            "accepted_count": len(rows),
            "losing_count": len(losers),
            "losing_rate_pct": round((len(losers) / len(rows)) * 100, 2)
            if rows
            else 0.0,
            "total_loss": round(sum(self._accepted_value(row) for row in losers), 2),
            "worst_accepted_trades": [self._accepted_event(row) for row in worst],
            "loss_impact_by_driver": by_reason,
        }

    def _segment_report(
        self,
        accepted_rows: list[Any],
        rejected_rows: list[RejectedOpportunityRecord],
        key_fn: Any,
    ) -> dict[str, Any]:
        accepted_groups: dict[str, list[Any]] = defaultdict(list)
        rejected_groups: dict[str, list[RejectedOpportunityRecord]] = defaultdict(list)
        for row in accepted_rows:
            accepted_groups[str(key_fn(row))].append(row)
        for row in rejected_rows:
            rejected_groups[str(key_fn(row))].append(row)
        keys = sorted(set(accepted_groups) | set(rejected_groups))
        report: dict[str, Any] = {}
        for key in keys:
            accepted = accepted_groups.get(key, [])
            rejected = rejected_groups.get(key, [])
            accepted_values = [self._accepted_value(row) for row in accepted]
            rejected_values = [self._rejected_move_value(row) for row in rejected]
            rejected_winners = len(
                [row for row in rejected if self._is_win(row.later_outcome)]
            )
            report[key] = {
                "accepted": self._summary_from_values(
                    accepted_values, [getattr(row, "outcome", None) for row in accepted]
                ),
                "rejected": self._summary_from_values(
                    rejected_values, [row.later_outcome for row in rejected]
                ),
                "rejected_count": len(rejected),
                "rejected_later_winner_count": rejected_winners,
                "missed_winner_rate_pct": round(
                    (rejected_winners / len(rejected)) * 100, 2
                )
                if rejected
                else 0.0,
            }
        return report

    def _setup_family_ranking(
        self, accepted_rows: list[Any], rejected_rows: list[RejectedOpportunityRecord]
    ) -> list[dict[str, Any]]:
        report = self._segment_report(accepted_rows, rejected_rows, self._setup_family)
        rows = [
            {
                "setup_family": key,
                "accepted_trades": value["accepted"]["trades"],
                "accepted_expectancy": value["accepted"]["expectancy"],
                "accepted_win_rate_pct": value["accepted"]["win_rate_pct"],
                "accepted_profit_factor": value["accepted"]["profit_factor"],
                "rejected_count": value["rejected_count"],
                "rejected_later_winner_count": value["rejected_later_winner_count"],
                "missed_winner_rate_pct": value["missed_winner_rate_pct"],
            }
            for key, value in report.items()
        ]
        return sorted(
            rows,
            key=lambda row: (
                float(row["accepted_expectancy"]),
                float(row["accepted_win_rate_pct"]),
            ),
            reverse=True,
        )

    def _segment_accepted(self, rows: list[Any], key_fn: Any) -> dict[str, Any]:
        groups: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            groups[str(key_fn(row))].append(row)
        return {
            key: self._summary_from_values(
                [self._accepted_value(row) for row in items],
                [getattr(row, "outcome", None) for row in items],
            )
            for key, items in sorted(groups.items())
        }

    def _research_readiness(
        self,
        trades: list[TradeRecord],
        opportunities: list[OpportunityRecord],
        reviewed_rejections: list[RejectedOpportunityRecord],
    ) -> dict[str, Any]:
        accepted_count = len(trades or opportunities)
        reviewed_count = len(reviewed_rejections)
        warnings: list[str] = []
        if accepted_count < 30:
            warnings.append(
                "Need at least 30 closed accepted paper/live-shadow trades before changing strategy thresholds."
            )
        if reviewed_count < 30:
            warnings.append(
                "Need at least 30 reviewed rejected setups before judging whether filters are over-rejecting winners."
            )
        return {
            "accepted_sample": accepted_count,
            "reviewed_rejection_sample": reviewed_count,
            "research_ready": accepted_count >= 30 and reviewed_count >= 30,
            "safe_to_change_strategy": False,
            "safe_to_enable_live": False,
            "warnings": warnings,
        }

    def _summary_from_values(
        self, values: list[float], outcomes: list[str | None]
    ) -> dict[str, Any]:
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
            "win_rate_pct": round((len(wins) / classified) * 100, 2)
            if classified
            else 0.0,
            "average_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "average_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
            "expectancy": round((sum(wins) + sum(losses)) / classified, 2)
            if classified
            else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown": round(max_drawdown(values), 2),
            "total_pnl": round(sum(values), 2),
        }

    def _grouped_rejection_reason_counts(
        self, rows: list[RejectedOpportunityRecord]
    ) -> dict[str, int]:
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
            warnings.append(
                "No Bank Nifty paper/live-shadow evidence was stored for this date."
            )
        if reason_counts.get(
            "premium_candles_stale_or_missing", 0
        ) or reason_counts.get("insufficient_current_session_premium_candles", 0):
            warnings.append("Premium confirmation candles were stale or missing.")
        if reason_counts.get("quote_invalid", 0):
            warnings.append("Invalid or unavailable option quotes were seen.")
        if reason_counts.get("data_stale", 0):
            warnings.append("Stale market data was seen.")
        return warnings

    def _accepted_value(self, row: Any) -> float:
        if isinstance(row, TradeRecord):
            if row.net_pnl is not None:
                return float(row.net_pnl)
            if row.pnl is not None:
                return float(row.pnl)
            return self._price_move_pnl(
                row.entry_price,
                row.exit_price,
                row.placed_quantity or row.filled_quantity or 1,
                row.side,
            )
        if isinstance(row, OpportunityRecord):
            if row.pnl is not None:
                return float(row.pnl)
            return self._price_move_pnl(
                row.entry_price, row.exit_price, row.quantity or 1, row.side
            )
        return 0.0

    def _price_move_pnl(
        self, entry: Any, exit_price: Any, quantity: Any, side: Any
    ) -> float:
        try:
            entry_value = float(entry or 0.0)
            exit_value = float(exit_price or 0.0)
            qty = float(quantity or 1)
        except (TypeError, ValueError):
            return 0.0
        if entry_value <= 0 or exit_value <= 0:
            return 0.0
        multiplier = 1 if str(side or "BUY").upper() == "BUY" else -1
        return round((exit_value - entry_value) * qty * multiplier, 2)

    def _accepted_event(self, row: Any) -> dict[str, Any]:
        return {
            "type": "trade" if isinstance(row, TradeRecord) else "accepted_opportunity",
            "id": getattr(row, "id", None),
            "timestamp": self._dt(getattr(row, "created_at", None)),
            "tradingsymbol": getattr(row, "tradingsymbol", None),
            "action": getattr(row, "action", None),
            "mode": getattr(row, "mode", None),
            "outcome": getattr(row, "outcome", None),
            "entry_price": getattr(row, "entry_price", None),
            "exit_price": getattr(row, "exit_price", None),
            "mfe_percent": getattr(row, "mfe_percent", None),
            "mae_percent": getattr(row, "mae_percent", None),
            "captured_mfe_percent": self._captured_mfe_percent(row)
            if isinstance(row, TradeRecord)
            else None,
            "net_or_proxy_pnl": self._accepted_value(row),
            "setup_family": self._setup_family(row),
            "time_block": time_bucket(getattr(row, "created_at", None)),
            "dte_bucket": self._dte_bucket(row),
            "iv_regime": self._iv_regime(row),
            "trend_regime": self._trend_regime(row),
        }

    def _setup_family(self, row: Any) -> str:
        factors = self._row_factor_scores(row)
        family = factors.get("setup_family") if isinstance(factors, dict) else None
        if isinstance(family, dict) and family.get("name"):
            return str(family.get("name")).lower()
        if isinstance(factors, dict):
            for key in ("setup_family_name", "setup_type"):
                if factors.get(key):
                    return str(factors.get(key)).lower()
            metadata = (
                factors.get("strategy_metadata", {})
                if isinstance(factors.get("strategy_metadata"), dict)
                else {}
            )
            for key in ("setup_family_name", "setup_type"):
                if metadata.get(key):
                    return str(metadata.get(key)).lower()
        if isinstance(row, TradeRecord):
            response = self._json(row.order_response_json)
            for key in ("setup_family_name", "setup_type"):
                if response.get(key):
                    return str(response.get(key)).lower()
            return str(
                row.action
                or self._option_type_from_symbol(row.tradingsymbol)
                or "unknown_setup"
            ).lower()
        if isinstance(row, OpportunityRecord):
            return str(
                factors.get("setup_type")
                or self._nested(factors, "strategy_metadata", "setup_type")
                or row.action
                or self._option_type_from_symbol(row.tradingsymbol)
                or "unknown_setup"
            ).lower()
        return str(
            factors.get("setup_type")
            or getattr(row, "action", None)
            or self._option_type_from_symbol(getattr(row, "tradingsymbol", None))
            or "unknown_setup"
        ).lower()

    def _row_factor_scores(self, row: Any) -> dict[str, Any]:
        if isinstance(row, TradeRecord):
            response = self._json(row.order_response_json)
            for key in ("signal_factor_scores", "factor_scores"):
                value = response.get(key)
                if isinstance(value, dict):
                    return value
            metadata = response.get("metadata")
            if isinstance(metadata, dict):
                value = metadata.get("factor_scores")
                if isinstance(value, dict):
                    return value
            return {}
        return self._json(getattr(row, "factor_scores_json", None))

    def _weekday_bucket(self, row: Any) -> str:
        created_at = getattr(row, "created_at", None)
        if not created_at:
            return "unknown_weekday"
        return created_at.strftime("%A").lower()

    def _iv_regime(self, row: Any) -> str:
        factors = self._json(getattr(row, "factor_scores_json", None))
        volatility = (
            factors.get("volatility_edge", {})
            if isinstance(factors.get("volatility_edge"), dict)
            else {}
        )
        details = (
            volatility.get("details", {})
            if isinstance(volatility.get("details"), dict)
            else {}
        )
        classification = str(volatility.get("classification") or "").lower()
        edge = str(volatility.get("volatility_edge_for_option_buying") or "").lower()
        main_risk = str(volatility.get("main_risk") or "").lower()
        if classification and classification not in {"", "disabled"}:
            return f"iv:{classification}"
        if main_risk and main_risk not in {"none", ""}:
            return f"iv_risk:{main_risk}"
        if edge and edge not in {"", "disabled"}:
            return f"iv_edge:{edge}"
        iv_rank = details.get("iv_rank")
        try:
            if iv_rank is not None:
                value = float(iv_rank)
                if value >= 80:
                    return "iv_rank_high"
                if value <= 20:
                    return "iv_rank_low"
                return "iv_rank_mid"
        except (TypeError, ValueError):
            pass
        return "unknown_iv_regime"

    def _trend_regime(self, row: Any) -> str:
        factors = self._json(getattr(row, "factor_scores_json", None))
        day_type = (
            factors.get("day_type", {})
            if isinstance(factors.get("day_type"), dict)
            else {}
        )
        day_details = (
            day_type.get("details", {})
            if isinstance(day_type.get("details"), dict)
            else {}
        )
        bank = (
            factors.get("banknifty_intelligence", {})
            if isinstance(factors.get("banknifty_intelligence"), dict)
            else {}
        )
        bank_details = (
            bank.get("details", {}) if isinstance(bank.get("details"), dict) else {}
        )
        regime = (
            day_details.get("day_type")
            or bank_details.get("dayType")
            or self._nested(factors, "market_regime", "details", "regime")
            or self._nested(factors, "price_action", "details", "trend_regime")
        )
        if regime:
            return str(regime).lower()
        action = str(getattr(row, "action", "") or "").upper()
        if "CE" in action:
            return "bullish_unknown_regime"
        if "PE" in action:
            return "bearish_unknown_regime"
        return "unknown_trend_regime"

    def _loss_driver(self, row: Any) -> str:
        outcome = str(getattr(row, "outcome", None) or "").lower()
        if outcome:
            return outcome
        factors = self._json(getattr(row, "factor_scores_json", None))
        for key in (
            "entry_timing",
            "banknifty_regime_filter",
            "volatility_edge",
            "option_premium_confirmation",
        ):
            value = factors.get(key)
            if isinstance(value, dict):
                reasons = value.get("reasons") or value.get("hard_reasons") or []
                if isinstance(reasons, list) and reasons:
                    return self._clean_label(str(reasons[0]))
        return "unknown_loss_driver"

    def _option_type_from_symbol(self, symbol: Any) -> str | None:
        text = str(symbol or "").upper()
        if text.endswith("CE"):
            return "buy_ce"
        if text.endswith("PE"):
            return "buy_pe"
        return None

    def _clean_label(self, value: str) -> str:
        return (
            str(value or "unknown")
            .strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")[:120]
        )

    def _nested(self, payload: dict[str, Any], *keys: str) -> Any:
        current: Any = payload
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

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

    def _factor_labels(
        self, factors: dict[str, Any], *, accepted: bool, row: Any
    ) -> list[str]:
        labels = [
            f"decision:{'accepted' if accepted else 'rejected'}",
            f"score:{score_bucket(getattr(row, 'score', 0))}",
        ]
        metadata = (
            factors.get("strategy_metadata", {})
            if isinstance(factors.get("strategy_metadata"), dict)
            else {}
        )
        labels.append(
            f"strategy_version:{metadata.get('strategy_version') or 'unknown'}"
        )
        premium = (
            factors.get("option_premium_confirmation", {})
            if isinstance(factors.get("option_premium_confirmation"), dict)
            else {}
        )
        if premium:
            labels.append(
                f"premium_source:{premium.get('source') or premium.get('premium_candle_source') or 'unknown'}"
            )
            labels.append(f"premium_passed:{bool(premium.get('passed'))}")
        day_type = (
            factors.get("day_type", {})
            if isinstance(factors.get("day_type"), dict)
            else {}
        )
        if day_type:
            labels.append(
                f"day_type:{day_type.get('day_type') or day_type.get('classification') or 'unknown'}"
            )
        if not accepted:
            labels.append(
                f"rejection_gate:{getattr(row, 'primary_gate', None) or 'unknown'}"
            )
        return labels

    def _later_group_quality(
        self, groups: dict[str, list[RejectedOpportunityRecord]]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, rows in sorted(groups.items()):
            missed_winners = len(
                [row for row in rows if self._is_win(row.later_outcome)]
            )
            saved_losers = len(
                [row for row in rows if self._is_loss(row.later_outcome)]
            )
            result[key] = {
                "reviewed": len(rows),
                "missed_winners": missed_winners,
                "saved_losers": saved_losers,
                "missed_winner_rate_pct": round((missed_winners / len(rows)) * 100, 2)
                if rows
                else 0.0,
            }
        return result

    def _rejected_move_value(self, row: RejectedOpportunityRecord) -> float:
        factors = self._json(row.factor_scores_json)
        prices = (
            factors.get("prices", {}) if isinstance(factors.get("prices"), dict) else {}
        )
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

    def _filtering_interpretation(
        self,
        opportunities: list[OpportunityRecord],
        reviewed_rejections: list[RejectedOpportunityRecord],
    ) -> str:
        accepted_summary = self._opportunity_summary(
            [row for row in opportunities if row.status == "closed"]
        )
        missed_winner_rate = (
            len([row for row in reviewed_rejections if self._is_win(row.later_outcome)])
            / len(reviewed_rejections)
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
                values.append(
                    max(
                        0.0,
                        (
                            trade.exit_confirmed_at - trade.exit_requested_at
                        ).total_seconds()
                        / 60,
                    )
                )
        return round(sum(values) / len(values), 2) if values else 0.0

    def _is_shadow_trade(self, trade: TradeRecord) -> bool:
        payload = self._json(trade.order_response_json)
        return bool(
            payload.get("shadow_for_live")
            or payload.get("live_shadow")
            or "shadow" in str(trade.notes or "").lower()
        )

    def _strategy_version(self, factors: dict[str, Any]) -> str:
        metadata = (
            factors.get("strategy_metadata", {})
            if isinstance(factors.get("strategy_metadata"), dict)
            else {}
        )
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
            "later_outcome_at": self._dt(row.later_outcome_at),
            "later_outcome_minutes": row.later_outcome_minutes,
            "later_outcome_source": row.later_outcome_source,
            "later_outcome_timeframe": row.later_outcome_timeframe,
            "later_outcome_ambiguous": bool(row.later_outcome_ambiguous),
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
            "highest_price_during_trade": row.highest_price_during_trade,
            "lowest_price_during_trade": row.lowest_price_during_trade,
            "mfe_percent": row.mfe_percent,
            "mae_percent": row.mae_percent,
            "captured_mfe_percent": self._captured_mfe_percent(row),
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
        if normalized.startswith("ambiguous_"):
            return False
        return (
            normalized in WIN_OUTCOMES
            or "target" in normalized
            or normalized.startswith("would_have_hit_target")
        )

    def _is_loss(self, outcome: str | None) -> bool:
        normalized = str(outcome or "").lower()
        if normalized.startswith("ambiguous_"):
            return False
        return (
            normalized in LOSS_OUTCOMES
            or "stop" in normalized
            or normalized.startswith("would_have_hit_stop")
        )

    def _is_ambiguous(self, row_or_outcome: Any) -> bool:
        if hasattr(row_or_outcome, "later_outcome_ambiguous") and bool(
            getattr(row_or_outcome, "later_outcome_ambiguous")
        ):
            return True
        outcome = getattr(row_or_outcome, "later_outcome", row_or_outcome)
        return str(outcome or "").lower().startswith("ambiguous_")

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
