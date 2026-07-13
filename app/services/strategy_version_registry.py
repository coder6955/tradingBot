from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import settings
from app.services.database import StrategyVersionRecord, get_session
from app.services.time_utils import ist_now_naive


SETTING_PURPOSES: dict[str, str] = {
    "min_signal_score": "Minimum weighted score required before a trade can become an accepted opportunity.",
    "min_market_regime_score": "Minimum broader market context score used to avoid low-quality directional conditions.",
    "min_price_action_score": "Minimum price-action score used to avoid weak Bank Nifty structure.",
    "min_option_chain_score": "Minimum option-chain context score; PCR/max pain remain context, not standalone hard triggers.",
    "min_risk_reward": "Minimum reward-to-risk required after dynamic entry, stop, and target calculation.",
    "max_trend_momentum_score": "Cap for correlated trend/momentum indicators so they cannot overboost confidence.",
    "max_bid_ask_spread_pct": "Maximum acceptable option bid/ask spread for tradable liquidity.",
    "min_option_volume": "Minimum selected option volume needed to avoid thin contracts.",
    "min_option_oi": "Minimum selected option open interest needed to avoid poor depth.",
    "min_option_buy_delta": "Lower delta bound for option-buying contracts; avoids too-far OTM options.",
    "max_option_buy_delta": "Upper delta bound for option-buying contracts; avoids overly expensive/deep ITM options.",
    "max_option_buy_theta_pct": "Theta quality guard for option buying; avoids contracts where decay is too heavy.",
    "min_option_buy_iv": "Lower IV sanity bound used by option-quality scoring.",
    "max_option_buy_iv": "Upper IV sanity bound used by option-quality scoring.",
    "min_option_quality_score": "Minimum combined option quality score for selected contracts.",
    "enable_volatility_edge": "Enables diagnostic volatility edge analysis for Bank Nifty option buying.",
    "enable_volatility_edge_hard_gate": "Allows volatility edge to block trades only when explicitly enabled.",
    "min_volatility_edge_score": "Minimum volatility edge score when the volatility hard gate is enabled.",
    "vol_edge_iv_lookback_days": "Historical lookback used for IV rank and percentile context.",
    "vol_edge_min_iv_samples": "Minimum IV samples needed before IV history is treated as reliable.",
    "vol_edge_max_iv_to_rv_ratio_for_buy": "Maximum IV-to-realized-volatility ratio considered acceptable for option buying.",
    "vol_edge_min_expected_move_coverage": "Minimum expected-move coverage needed before volatility context supports target 1.",
    "vol_edge_iv_crush_warning_threshold": "IV rank threshold where IV crush risk is highlighted.",
    "enable_option_premium_confirmation": "Requires selected option premium to show fresh current-session participation.",
    "min_option_premium_confirmation_score": "Minimum premium confirmation score before entry is allowed.",
    "option_premium_lookback_candles": "Number of recent option premium candles used for premium confirmation.",
    "max_premium_confirmation_candle_age_seconds": "Maximum age of premium confirmation candles before they are considered stale.",
    "max_stored_premium_candle_age_seconds": "Maximum age for stored option premium candles before rejecting them.",
    "min_websocket_premium_candles": "Minimum WebSocket-built option candles needed before premium confirmation can trust them.",
    "max_websocket_premium_candle_age_seconds": "Maximum age for WebSocket-built option premium candles.",
    "max_entry_chase_pct": "Maximum allowed distance above breakout trigger before rejecting a late/chasing entry.",
    "max_premium_move_from_base_pct": "Maximum premium move from base before the setup is considered stretched.",
    "min_remaining_risk_reward": "Minimum reward-to-risk that must remain after the current entry price.",
    "min_target1_room_pct": "Minimum remaining room to target 1 after entry; avoids buying too near target.",
    "entry_armed_distance_to_trigger_pct": "Distance below trigger where a setup becomes armed instead of immediately rejected.",
    "entry_trigger_lookback_candles": "Recent premium candle lookback used to define the entry trigger.",
    "min_entry_expected_move_coverage": "Minimum expected-move coverage needed for the trade to have enough practical room.",
    "min_entry_room_to_level_pct": "Minimum room to nearest important Bank Nifty level after entry.",
    "enable_event_driven_paper_entry": "Allows paper-only WebSocket trigger entry for already armed setups.",
    "enable_event_driven_live_entry": "Reserved live event-entry switch; intentionally disabled unless separately implemented.",
    "armed_entry_valid_seconds": "How long an armed setup may wait for a WebSocket trigger before expiring.",
    "enable_early_armed_entry": "Allows strong forming Bank Nifty setups to arm before full premium confirmation, while final entry remains WebSocket-triggered.",
    "early_armed_entry_paper_only": "Keeps early armed entries restricted to paper mode until live-shadow evidence proves the behavior.",
    "early_arm_min_score": "Minimum weighted setup score required before early arming is allowed.",
    "early_arm_trigger_buffer_pct": "Minimum premium move above current price required for a synthetic early-arm trigger when premium candles are not ready.",
    "early_arm_allow_premium_pending": "Allows early arming only for premium-confirmation-pending cases while preserving other hard gates.",
    "enable_tick_quality_confirmation": "Requires WebSocket trigger crossings to hold through multiple quality ticks before paper entry.",
    "tick_quality_min_ticks_above_trigger": "Minimum number of WebSocket ticks at or above trigger before event entry can fire.",
    "tick_quality_hold_seconds": "Minimum time the premium must remain at or above trigger before event entry can fire.",
    "tick_quality_require_bid_progress": "Requires bid-side progress so a single ask spike cannot trigger entry by itself.",
    "tick_quality_max_spread_multiplier": "Maximum spread widening relative to arming spread accepted during tick-quality confirmation.",
    "enable_rejected_outcome_candle_replay": "Uses post-rejection candle path to label missed target/stop outcomes instead of relying only on the latest quote.",
    "rejected_outcome_replay_timeframes": "Ordered candle timeframes used to classify rejected setup later outcomes.",
    "rejected_outcome_replay_max_candles": "Maximum post-rejection candles inspected per rejected setup.",
    "rejected_outcome_use_ws_token_candles": "Allows WebSocket token-stored premium candles to support rejected setup replay.",
    "rejected_outcome_ambiguous_candle_policy": "Policy for candles whose high and low touch both target and stop; label_ambiguous keeps these separate from wins/losses.",
    "rejected_outcome_batch_limit": "Number of rejected setups evaluated per after-market replay batch.",
    "rejected_outcome_max_batches": "Maximum after-market rejected-outcome replay batches per run.",
    "rejected_outcome_batch_delay_seconds": "Delay between rejected-outcome replay batches to avoid loading the server at once.",
    "automation_exhaust_rejected_outcomes_after_close": "Lets after-close automation walk through all pending rejected rows instead of only the first page.",
    "enable_targeted_option_candle_backfill": "After market close, backfills candles for option contracts actually traded, accepted, or rejected by the app.",
    "targeted_option_candle_backfill_timeframes": "Ordered timeframes used for targeted relevant option candle backfill.",
    "targeted_option_candle_backfill_batch_limit": "Contracts processed before sleeping during targeted option candle backfill.",
    "targeted_option_candle_backfill_max_contracts": "Maximum relevant option contracts backfilled per after-market run.",
    "targeted_option_candle_backfill_delay_seconds": "Delay between targeted option candle backfill batches to avoid a sudden broker/server load spike.",
    "min_targeted_option_candle_coverage_pct": "Minimum candle coverage percentage considered high-confidence for rejected-opportunity research.",
    "enable_live_option_candle_gap_backfill": "During market hours, backfills recent missing option candles after restart/reconnect so premium confirmation can recover.",
    "live_option_candle_backfill_timeframes": "Timeframes used for live restart/reconnect option candle catch-up.",
    "live_option_candle_backfill_lookback_minutes": "Maximum recent window repaired during live option candle catch-up.",
    "live_option_candle_backfill_interval_seconds": "Minimum interval between automated live option candle catch-up attempts.",
    "live_option_candle_backfill_min_gap_seconds": "Minimum missing closed-candle gap before live catch-up calls historical data.",
    "live_option_candle_backfill_max_contracts": "Maximum app-relevant contracts repaired per live catch-up run.",
    "live_option_candle_backfill_batch_limit": "Contracts processed before sleeping during live catch-up.",
    "live_option_candle_backfill_delay_seconds": "Delay between live catch-up batches.",
    "enable_on_demand_premium_candle_backfill": "Lets premium confirmation backfill the selected option once before rejecting stale/missing candles.",
    "on_demand_premium_candle_backfill_cooldown_seconds": "Cooldown between on-demand selected-option candle backfill attempts.",
    "enable_banknifty_intelligence": "Enables Bank Nifty specialized filters such as top-bank alignment and zone room.",
    "banknifty_top_bank_min_alignment": "Minimum top-bank constituent alignment required for Bank Nifty confidence.",
    "banknifty_top_bank_min_direction_count": "Minimum number of top banks that should support the chosen direction.",
    "banknifty_expected_move_min_coverage": "Expected move coverage guard for whether target distance is realistic.",
    "banknifty_zone_risk_points": "Distance near major zones where Bank Nifty trades become riskier.",
    "enable_banknifty_regime_filter": "Enables Bank Nifty option-buying no-trade regime filters.",
    "min_banknifty_regime_score": "Minimum Bank Nifty regime score required after no-buy regime checks.",
    "banknifty_significant_gap_pct": "Gap percentage treated as meaningful for gap-trap context.",
    "banknifty_compression_day_range_pct": "Small day-range threshold used to detect compression before expansion.",
    "banknifty_late_trade_cutoff_time": "Time after which late-day option buying requires stronger premium expansion.",
    "banknifty_late_trade_min_premium_score": "Minimum premium confirmation score needed for late-day option buying.",
    "banknifty_expiry_min_premium_score": "Minimum premium confirmation score needed for expiry/near-expiry option buying.",
    "option_time_stop_minutes": "Maximum time to stay in a trade without sufficient movement.",
    "option_time_stop_min_move_pct": "Minimum favorable move required to avoid time-stop exit.",
    "option_trailing_stop_lock_pct": "Profit lock level used after target 1 has been reached.",
    "exit_open_trades_before_close_minutes": "Minutes before market close when open trades should be exited.",
    "enable_underlying_invalidation_exit": "Exits when Bank Nifty structure invalidates the original trade idea.",
    "enable_premium_invalidation_exit": "Exits when selected option premium structure invalidates after entry.",
    "enable_partial_booking": "Whether target 1 should book partial quantity instead of full exit.",
    "partial_target1_pct": "Percentage of quantity booked at target 1 when partial booking is enabled.",
    "partial_move_sl_to_cost": "Whether remaining quantity stop should move to cost after partial booking.",
    "max_daily_loss_pct": "Maximum daily loss guard for live trading risk control.",
    "max_trades_per_day": "Maximum trades per day to prevent overtrading.",
    "max_stop_losses_per_day": "Maximum stop-loss hits allowed before cooling down for the day.",
    "max_open_trades": "Maximum total open trades allowed; live mode should remain one at a time.",
    "cooldown_after_stop_minutes": "Cooldown after a stop loss to avoid revenge or chop trading.",
    "enforce_execution_quality": "Final pre-order quote/spread/deviation check before order placement.",
    "max_execution_spread_pct": "Maximum spread accepted immediately before execution.",
    "max_entry_price_deviation_pct": "Maximum allowed difference between signal entry and current executable quote.",
    "enable_execution_realism": "Uses conservative adverse fills for paper trading and backtests.",
    "realism_entry_buy_slippage_pct": "Extra paper/backtest entry fill penalty when buying near ask.",
    "realism_exit_target_slippage_pct": "Extra paper/backtest target exit penalty when selling near bid.",
    "realism_exit_stop_overshoot_pct": "Extra paper/backtest stop-loss overshoot penalty.",
    "realism_no_fill_touch_buffer_pct": "Requires target to clear a small buffer before backtest assumes a fill.",
    "realism_first_15_min_extra_slippage_pct": "Additional execution penalty during opening volatility.",
    "realism_expiry_day_extra_slippage_pct": "Additional execution penalty on expiry-day conditions.",
    "realism_high_iv_extra_slippage_pct": "Additional execution penalty during high-IV regimes.",
    "realism_wide_spread_extra_slippage_pct": "Additional execution penalty when spreads are wide.",
    "enable_kite_websocket": "Enables WebSocket-first active trade monitoring.",
    "websocket_price_stale_seconds": "Maximum tick age accepted for WebSocket price monitoring.",
    "websocket_live_stale_blocks": "Blocks live active-price use if WebSocket ticks are stale or missing.",
    "enable_websocket_premium_candle_builder": "Builds current-session one-minute option premium candles from ticks.",
    "enable_websocket_candle_persistence": "Persists WebSocket-built option candles so restart does not wipe premium candle state.",
    "enable_websocket_candle_daily_cleanup": "Keeps WebSocket-built one-minute option candles current-day only to avoid stale intraday data buildup.",
    "enable_market_data_gap_detection": "Detects tick/candle feed gaps and marks recovery uncertainty.",
    "max_websocket_gap_seconds": "Maximum tolerated WebSocket tick silence before marking a data gap.",
    "enable_websocket_gap_backfill": "Allows best-effort Kite historical backfill for missed WebSocket candle minutes.",
    "websocket_live_gap_polling_fallback": "Allows marked Kite polling fallback for open live trades during WebSocket gaps.",
    "cancel_armed_entries_on_data_gap": "Cancels armed event-driven entries when WebSocket data gaps are detected.",
    "enable_banknifty_option_prewarm": "Pre-subscribes near-ATM Bank Nifty option contracts for premium candle warm-up.",
    "use_kite_market_data": "Requires real Kite market data rather than mock data.",
}


class StrategyVersionRegistry:
    """Source of truth for what each strategy version represented."""

    def ensure_current_version(self, *, human_note: str | None = None, reason_for_change: str | None = None) -> dict[str, Any]:
        payload = self.current_payload(human_note=human_note, reason_for_change=reason_for_change)
        return self.register(payload)

    def current_payload(self, *, human_note: str | None = None, reason_for_change: str | None = None) -> dict[str, Any]:
        config_snapshot = self.current_config_snapshot()
        settings_purpose = self.settings_purpose_snapshot(config_snapshot)
        return {
            "strategy_name": settings.strategy_name,
            "version": settings.strategy_version,
            "status": "active",
            "human_note": human_note or settings.strategy_version_note,
            "reason_for_change": reason_for_change or settings.strategy_change_reason,
            "entry_logic_summary": self.entry_logic_summary(config_snapshot),
            "exit_logic_summary": self.exit_logic_summary(config_snapshot),
            "stoploss_logic_summary": self.stoploss_logic_summary(config_snapshot),
            "target_logic_summary": self.target_logic_summary(config_snapshot),
            "config_snapshot": config_snapshot,
            "settings_purpose": settings_purpose,
        }

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        version = str(payload.get("version") or settings.strategy_version)
        strategy_name = str(payload.get("strategy_name") or settings.strategy_name)
        now = ist_now_naive()
        config_snapshot = payload.get("config_snapshot") if isinstance(payload.get("config_snapshot"), dict) else self.current_config_snapshot()
        settings_purpose = payload.get("settings_purpose") if isinstance(payload.get("settings_purpose"), dict) else self.settings_purpose_snapshot(config_snapshot)
        config_hash = self.config_hash(config_snapshot)
        session = get_session()
        try:
            record = (
                session.query(StrategyVersionRecord)
                .filter(StrategyVersionRecord.version == version)
                .first()
            )
            created = False
            drift_detected = False
            if record is None:
                created = True
                record = StrategyVersionRecord(
                    strategy_name=strategy_name,
                    version=version,
                    status=str(payload.get("status") or "active"),
                    started_at=now,
                    last_seen_at=now,
                    human_note=str(payload.get("human_note") or ""),
                    reason_for_change=str(payload.get("reason_for_change") or ""),
                    entry_logic_summary=str(payload.get("entry_logic_summary") or ""),
                    exit_logic_summary=str(payload.get("exit_logic_summary") or ""),
                    stoploss_logic_summary=str(payload.get("stoploss_logic_summary") or ""),
                    target_logic_summary=str(payload.get("target_logic_summary") or ""),
                    config_snapshot_json=json.dumps(config_snapshot, default=str, sort_keys=True),
                    latest_config_snapshot_json=json.dumps(config_snapshot, default=str, sort_keys=True),
                    settings_purpose_json=json.dumps(settings_purpose, default=str, sort_keys=True),
                    config_hash=config_hash,
                    latest_config_hash=config_hash,
                    config_drift_detected=0,
                )
                session.add(record)
            else:
                latest_hash = config_hash
                drift_detected = bool(record.config_hash and record.config_hash != latest_hash)
                force_text_update = bool(payload.get("_force_text_update"))
                record.updated_at = now
                record.last_seen_at = now
                record.latest_config_snapshot_json = json.dumps(config_snapshot, default=str, sort_keys=True)
                record.settings_purpose_json = json.dumps(settings_purpose, default=str, sort_keys=True)
                record.latest_config_hash = latest_hash
                record.config_drift_detected = 1 if drift_detected or int(record.config_drift_detected or 0) else 0
                if payload.get("human_note") and (force_text_update or not record.human_note):
                    record.human_note = str(payload["human_note"])
                if payload.get("reason_for_change") and (force_text_update or not record.reason_for_change):
                    record.reason_for_change = str(payload["reason_for_change"])
                for field in ("entry_logic_summary", "exit_logic_summary", "stoploss_logic_summary", "target_logic_summary"):
                    if payload.get(field) and (force_text_update or not getattr(record, field)):
                        setattr(record, field, str(payload[field]))
            if str(payload.get("status") or "active") == "active":
                self._retire_other_active_versions(session, strategy_name=strategy_name, version=version, now=now)
                record.status = "active"
            session.commit()
            session.refresh(record)
            return {"status": "ok", "created": created, "config_drift_detected": drift_detected, "version": self.to_dict(record)}
        finally:
            session.close()

    def list_versions(self, *, limit: int = 50) -> dict[str, Any]:
        self.ensure_current_version()
        session = get_session()
        try:
            rows = (
                session.query(StrategyVersionRecord)
                .order_by(StrategyVersionRecord.id.desc())
                .limit(max(1, int(limit)))
                .all()
            )
            return {"status": "ok", "count": len(rows), "versions": [self.to_dict(row) for row in rows]}
        finally:
            session.close()

    def current_version(self) -> dict[str, Any]:
        return self.ensure_current_version()

    def get_version(self, version: str) -> dict[str, Any]:
        session = get_session()
        try:
            record = (
                session.query(StrategyVersionRecord)
                .filter(StrategyVersionRecord.version == version)
                .first()
            )
            if record is None:
                return {"status": "not_found", "version": version}
            return {"status": "ok", "version": self.to_dict(record)}
        finally:
            session.close()

    def current_config_snapshot(self) -> dict[str, Any]:
        return {
            "strategy": {
                "strategy_name": settings.strategy_name,
                "strategy_version": settings.strategy_version,
                "app_environment": settings.app_environment,
            },
            "entry_filters": self._values(
                "min_signal_score",
                "min_market_regime_score",
                "min_price_action_score",
                "min_option_chain_score",
                "min_risk_reward",
                "max_trend_momentum_score",
            ),
            "option_quality": self._values(
                "max_bid_ask_spread_pct",
                "min_option_volume",
                "min_option_oi",
                "min_option_buy_delta",
                "max_option_buy_delta",
                "max_option_buy_theta_pct",
                "min_option_buy_iv",
                "max_option_buy_iv",
                "min_option_quality_score",
            ),
            "premium_confirmation": self._values(
                "enable_option_premium_confirmation",
                "min_option_premium_confirmation_score",
                "option_premium_lookback_candles",
                "max_premium_confirmation_candle_age_seconds",
                "max_stored_premium_candle_age_seconds",
                "min_websocket_premium_candles",
                "max_websocket_premium_candle_age_seconds",
            ),
            "volatility_edge": self._values(
                "enable_volatility_edge",
                "enable_volatility_edge_hard_gate",
                "min_volatility_edge_score",
                "vol_edge_iv_lookback_days",
                "vol_edge_min_iv_samples",
                "vol_edge_max_iv_to_rv_ratio_for_buy",
                "vol_edge_min_expected_move_coverage",
                "vol_edge_iv_crush_warning_threshold",
            ),
            "entry_timing": self._values(
                "max_entry_chase_pct",
                "max_premium_move_from_base_pct",
                "min_remaining_risk_reward",
                "min_target1_room_pct",
                "entry_armed_distance_to_trigger_pct",
                "entry_trigger_lookback_candles",
                "min_entry_expected_move_coverage",
                "min_entry_room_to_level_pct",
                "enable_event_driven_paper_entry",
                "enable_event_driven_live_entry",
                "armed_entry_valid_seconds",
                "enable_early_armed_entry",
                "early_armed_entry_paper_only",
                "early_arm_min_score",
                "early_arm_trigger_buffer_pct",
                "early_arm_allow_premium_pending",
                "enable_tick_quality_confirmation",
                "tick_quality_min_ticks_above_trigger",
                "tick_quality_hold_seconds",
                "tick_quality_require_bid_progress",
                "tick_quality_max_spread_multiplier",
            ),
            "banknifty_specialization": self._values(
                "enable_banknifty_intelligence",
                "banknifty_top_bank_min_alignment",
                "banknifty_top_bank_min_direction_count",
                "banknifty_expected_move_min_coverage",
                "banknifty_zone_risk_points",
                "banknifty_major_zone_points",
                "banknifty_very_major_zone_points",
                "enable_banknifty_regime_filter",
                "min_banknifty_regime_score",
                "banknifty_significant_gap_pct",
                "banknifty_compression_day_range_pct",
                "banknifty_late_trade_cutoff_time",
                "banknifty_late_trade_min_premium_score",
                "banknifty_expiry_min_premium_score",
            ),
            "exit_rules": self._values(
                "enable_auto_squareoff",
                "live_auto_squareoff",
                "option_time_stop_minutes",
                "option_time_stop_min_move_pct",
                "option_trailing_stop_lock_pct",
                "exit_open_trades_before_close_minutes",
                "enable_underlying_invalidation_exit",
                "enable_premium_invalidation_exit",
                "enable_partial_booking",
                "partial_target1_pct",
                "partial_move_sl_to_cost",
            ),
            "risk_rules": self._values(
                "max_daily_loss_pct",
                "max_trades_per_day",
                "max_stop_losses_per_day",
                "max_open_trades",
                "max_symbol_open_trades",
                "max_open_premium_exposure_pct",
                "cooldown_after_stop_minutes",
                "max_option_premium_pct",
            ),
            "execution_quality": self._values(
                "enforce_execution_quality",
                "max_execution_spread_pct",
                "max_entry_price_deviation_pct",
                "min_execution_quote_price",
                "enable_execution_realism",
                "realism_entry_buy_slippage_pct",
                "realism_exit_target_slippage_pct",
                "realism_exit_stop_overshoot_pct",
                "realism_no_fill_touch_buffer_pct",
                "realism_first_15_min_extra_slippage_pct",
                "realism_expiry_day_extra_slippage_pct",
                "realism_high_iv_extra_slippage_pct",
                "realism_wide_spread_extra_slippage_pct",
            ),
            "data_policy": self._values(
                "use_kite_market_data",
                "max_live_quote_age_seconds",
                "max_live_option_quote_age_seconds",
                "max_live_chain_age_seconds",
                "max_live_candle_age_seconds",
                "max_paper_candle_age_seconds",
                "enable_kite_websocket",
                "websocket_price_stale_seconds",
                "websocket_live_stale_blocks",
                "websocket_live_require_exchange_timestamp",
                "enable_websocket_premium_candle_builder",
                "enable_websocket_candle_persistence",
                "enable_websocket_candle_daily_cleanup",
                "enable_market_data_gap_detection",
                "max_websocket_gap_seconds",
                "enable_websocket_gap_backfill",
                "websocket_live_gap_polling_fallback",
                "cancel_armed_entries_on_data_gap",
                "enable_banknifty_option_prewarm",
            ),
            "learning_guards": self._values(
                "enable_outcome_learning_guard",
                "min_outcome_learning_trades",
                "min_outcome_learning_expectancy_pct",
                "min_outcome_learning_win_rate_pct",
                "outcome_learning_lookback",
                "enable_rejected_outcome_candle_replay",
                "rejected_outcome_replay_timeframes",
                "rejected_outcome_replay_max_candles",
                "rejected_outcome_use_ws_token_candles",
                "rejected_outcome_ambiguous_candle_policy",
                "rejected_outcome_batch_limit",
                "rejected_outcome_max_batches",
                "rejected_outcome_batch_delay_seconds",
                "automation_exhaust_rejected_outcomes_after_close",
                "enable_targeted_option_candle_backfill",
                "targeted_option_candle_backfill_timeframes",
                "targeted_option_candle_backfill_batch_limit",
                "targeted_option_candle_backfill_max_contracts",
                "targeted_option_candle_backfill_delay_seconds",
                "min_targeted_option_candle_coverage_pct",
                "enable_live_option_candle_gap_backfill",
                "live_option_candle_backfill_timeframes",
                "live_option_candle_backfill_lookback_minutes",
                "live_option_candle_backfill_interval_seconds",
                "live_option_candle_backfill_min_gap_seconds",
                "live_option_candle_backfill_max_contracts",
                "live_option_candle_backfill_batch_limit",
                "live_option_candle_backfill_delay_seconds",
                "enable_on_demand_premium_candle_backfill",
                "on_demand_premium_candle_backfill_cooldown_seconds",
                "enable_strategy_edge_guard",
                "min_strategy_trades",
                "min_strategy_expectancy_pct",
                "min_strategy_profit_factor",
            ),
        }

    def settings_purpose_snapshot(self, config_snapshot: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for category, values in config_snapshot.items():
            if not isinstance(values, dict):
                continue
            result[category] = {
                key: {"value": value, "purpose": SETTING_PURPOSES.get(key, "Recorded for reproducibility and version comparison.")}
                for key, value in values.items()
            }
        return result

    def entry_logic_summary(self, config_snapshot: dict[str, Any]) -> str:
        entry = config_snapshot.get("entry_timing", {})
        premium = config_snapshot.get("premium_confirmation", {})
        return (
            "Bank Nifty option-buying entries require real market data, weighted score above threshold, "
            "tradable option quality, fresh premium confirmation, and entry timing that is not too early or too late. "
            f"Current chase limit is {entry.get('max_entry_chase_pct')}% and premium confirmation is "
            f"{'enabled' if premium.get('enable_option_premium_confirmation') else 'disabled'}."
        )

    def exit_logic_summary(self, config_snapshot: dict[str, Any]) -> str:
        exit_rules = config_snapshot.get("exit_rules", {})
        partial = "partial target-1 booking" if exit_rules.get("enable_partial_booking") else "full target-1 square-off"
        return (
            "Open trades are monitored for stop loss, target hits, trailing stop, time stop, near-close exit, "
            f"underlying invalidation, and premium invalidation. Current target-1 behavior is {partial}."
        )

    def stoploss_logic_summary(self, config_snapshot: dict[str, Any]) -> str:
        exit_rules = config_snapshot.get("exit_rules", {})
        return (
            "Stop-loss and invalidation exits protect the option premium after entry. "
            f"Underlying invalidation is {'enabled' if exit_rules.get('enable_underlying_invalidation_exit') else 'disabled'}; "
            f"premium invalidation is {'enabled' if exit_rules.get('enable_premium_invalidation_exit') else 'disabled'}."
        )

    def target_logic_summary(self, config_snapshot: dict[str, Any]) -> str:
        exit_rules = config_snapshot.get("exit_rules", {})
        return (
            "Targets are evaluated against live/paper option premium. "
            f"Target 1 currently {'books ' + str(exit_rules.get('partial_target1_pct')) + '% when partial booking is enabled' if exit_rules.get('enable_partial_booking') else 'closes the full position'}; "
            f"trailing lock is {exit_rules.get('option_trailing_stop_lock_pct')}% after target progress."
        )

    def config_hash(self, config_snapshot: dict[str, Any]) -> str:
        payload = json.dumps(config_snapshot, default=str, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self, record: StrategyVersionRecord) -> dict[str, Any]:
        original_config = self._json(record.config_snapshot_json)
        latest_config = self._json(record.latest_config_snapshot_json) if record.latest_config_snapshot_json else original_config
        return {
            "id": record.id,
            "created_at": self._format_dt(record.created_at),
            "updated_at": self._format_dt(record.updated_at),
            "last_seen_at": self._format_dt(record.last_seen_at),
            "strategy_name": record.strategy_name,
            "version": record.version,
            "status": record.status,
            "started_at": self._format_dt(record.started_at),
            "retired_at": self._format_dt(record.retired_at),
            "human_note": record.human_note,
            "reason_for_change": record.reason_for_change,
            "entry_logic_summary": record.entry_logic_summary,
            "exit_logic_summary": record.exit_logic_summary,
            "stoploss_logic_summary": record.stoploss_logic_summary,
            "target_logic_summary": record.target_logic_summary,
            "config_hash": record.config_hash,
            "latest_config_hash": record.latest_config_hash,
            "config_drift_detected": bool(record.config_drift_detected),
            "config_drift_warning": (
                "Current settings differ from the first-seen config for this version. Bump STRATEGY_VERSION before judging new trades."
                if record.config_drift_detected
                else None
            ),
            "config_snapshot": original_config,
            "latest_config_snapshot": latest_config,
            "settings_purpose": self._json(record.settings_purpose_json),
        }

    def _retire_other_active_versions(self, session: Any, *, strategy_name: str, version: str, now: Any) -> None:
        rows = (
            session.query(StrategyVersionRecord)
            .filter(
                StrategyVersionRecord.strategy_name == strategy_name,
                StrategyVersionRecord.version != version,
                StrategyVersionRecord.status == "active",
            )
            .all()
        )
        for row in rows:
            row.status = "retired"
            row.retired_at = row.retired_at or now
            row.updated_at = now

    def _values(self, *keys: str) -> dict[str, Any]:
        return {key: getattr(settings, key) for key in keys}

    def _json(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _format_dt(self, value: Any) -> str | None:
        return value.isoformat(sep=" ") if value else None
