from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Any

from app.config import settings
from app.services.database import Candle, get_session
from app.services.indicator_service import compute_ema, compute_macd, compute_rsi


@dataclass(frozen=True)
class BacktestTrade:
    timestamp: str
    direction: str
    entry_price: float
    exit_price: float
    outcome: str
    move_pct: float
    bars_held: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptionBacktestTrade:
    timestamp: str
    direction: str
    underlying_entry: float
    tradingsymbol: str
    option_entry: float
    option_exit: float
    outcome: str
    pnl_pct: float
    gross_pnl_pct: float
    charges_pct: float
    slippage_pct: float
    spread_impact_pct: float
    bars_held: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BacktestService:
    """Replay stored underlying candles to validate directional option-buying rules."""

    ABLATION_VARIANTS: dict[str, dict[str, Any]] = {
        "baseline": {},
        "without_rsi": {"disabled": {"rsi"}},
        "without_macd": {"disabled": {"macd"}},
        "without_adx": {"unsupported": "ADX is not stored in historical candle backtest inputs yet."},
        "without_pcr": {"unsupported": "PCR is option-chain context and not part of candle replay yet."},
        "without_max_pain": {"unsupported": "Max pain is option-chain context and not part of candle replay yet."},
        "without_nifty_context": {"unsupported": "Nifty context is live scanner context and not part of single-symbol replay yet."},
        "without_vix_context": {"unsupported": "VIX context is live scanner context and not part of single-symbol replay yet."},
        "without_premium_confirmation": {"unsupported": "Premium confirmation requires live/stored option premium snapshots, not underlying-only signals."},
        "without_candle_confirmation": {"disabled": {"breakout"}},
        "without_day_type_hard_gate": {"unsupported": "Day-type hard gate uses intraday context and is not represented in this option premium replay."},
        "only_vwap_premium_quality": {"unsupported": "VWAP/premium/quality ablation needs stored VWAP and option-quality snapshots."},
        "only_trend_quality_premium": {"unsupported": "Quality and premium confirmation are not fully represented in this candle signal replay."},
    }

    def run(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        side: str = "BUY",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        side = side.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        candles = self._load_candles(symbol=symbol.upper(), timeframe=timeframe, limit=limit)
        if len(candles) < 60:
            return {
                "status": "insufficient_data",
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "candles": len(candles),
                "minimum_required": 60,
                "message": "Store more historical candles before this backtest can produce useful evidence.",
            }

        closes = [float(item.close_price) for item in candles]
        highs = [float(item.high_price) for item in candles]
        lows = [float(item.low_price) for item in candles]
        volumes = [float(item.volume) for item in candles]
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)
        rsi = compute_rsi(closes, 14)
        macd, macd_signal = compute_macd(closes)

        trades: list[BacktestTrade] = []
        warmup = 55
        last_signal_index = -horizon
        for idx in range(warmup, len(candles) - horizon):
            if idx - last_signal_index < max(3, horizon // 3):
                continue
            signal_direction, reason = self._signal_direction(
                idx=idx,
                closes=closes,
                highs=highs,
                lows=lows,
                volumes=volumes,
                ema9=ema9,
                ema21=ema21,
                ema50=ema50,
                rsi=rsi,
                macd=macd,
                macd_signal=macd_signal,
            )
            if signal_direction is None:
                continue
            if direction in {"CALL", "CE", "BULLISH"} and signal_direction != "CALL":
                continue
            if direction in {"PUT", "PE", "BEARISH"} and signal_direction != "PUT":
                continue
            trade = self._simulate_trade(
                candles=candles,
                idx=idx,
                direction=signal_direction,
                horizon=horizon,
                reason=reason,
            )
            trades.append(trade)
            last_signal_index = idx

        summary = self._summarize(trades)
        return {
            "status": "ok",
            "mode": "underlying_rule_replay",
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "side": side,
            "direction": direction,
            "candles": len(candles),
            "horizon_candles": horizon,
            "summary": summary,
            "examples": [trade.to_dict() for trade in trades[-20:]],
            "limitations": [
                "This validates directional entry logic on underlying candles.",
                "Exact option P&L requires historical option-chain quotes, bid/ask, IV, and Greeks snapshots.",
                "Use this result as a filter-quality study, not as a guaranteed live fill simulation.",
            ],
        }

    def run_option_premium(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        underlying = self._load_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        if len(underlying) < 60:
            return {"status": "insufficient_data", "symbol": symbol, "candles": len(underlying), "minimum_required": 60}

        option_candles = self._load_option_candles(underlying=symbol, timeframe=timeframe)
        if not option_candles:
            return {
                "status": "insufficient_option_data",
                "symbol": symbol,
                "message": "No stored option premium candles found for this underlying. Run /data/ingest/option-candles first.",
            }

        signals = self._signals_from_underlying(underlying, direction=direction, horizon=horizon)
        trades: list[OptionBacktestTrade] = []
        for idx, signal_direction, reason in signals:
            trade = self._simulate_option_trade(
                underlying=underlying,
                option_candles=option_candles,
                idx=idx,
                direction=signal_direction,
                horizon=horizon,
                reason=reason,
            )
            if trade is not None:
                trades.append(trade)

        summary = self._summarize_option_trades(trades, timeframe=timeframe)
        return {
            "status": "ok",
            "mode": "option_premium_replay",
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "underlying_candles": len(underlying),
            "option_contracts": len(option_candles),
            "horizon_candles": horizon,
            "stop_loss_pct": settings.backtest_option_stop_loss_pct,
            "target_pct": settings.backtest_option_target_pct,
            "slippage_pct": settings.backtest_slippage_pct,
            "charges_pct": settings.backtest_charges_pct,
            "summary": summary,
            "segments": self._segment_option_trades(trades, timeframe=timeframe),
            "examples": [trade.to_dict() for trade in trades[-20:]],
        }

    def run_walk_forward(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 3000,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        candles = self._load_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        if len(candles) < 120:
            return {"status": "insufficient_data", "symbol": symbol, "candles": len(candles), "minimum_required": 120}

        split = int(len(candles) * (settings.backtest_walk_forward_train_pct / 100))
        split = min(max(split, 60), len(candles) - 60)
        train_result = self._run_option_premium_on_candles(symbol=symbol, timeframe=timeframe, direction=direction, horizon=horizon, underlying=candles[:split])
        test_result = self._run_option_premium_on_candles(symbol=symbol, timeframe=timeframe, direction=direction, horizon=horizon, underlying=candles[split:])
        passed, reasons = self._validation_passed(test_result.get("summary", {}))
        return {
            "status": "ok",
            "mode": "walk_forward_option",
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "train_candles": split,
            "test_candles": len(candles) - split,
            "train_summary": train_result.get("summary", {}),
            "test_summary": test_result.get("summary", {}),
            "summary": test_result.get("summary", {}),
            "passed": passed,
            "reasons": reasons,
            "thresholds": {
                "min_trades": settings.min_strategy_trades,
                "min_expectancy_pct": settings.min_strategy_expectancy_pct,
                "min_profit_factor": settings.min_strategy_profit_factor,
                "min_win_rate_pct": settings.min_strategy_win_rate_pct,
            },
        }

    def run_ablation(
        self,
        *,
        symbol: str,
        timeframe: str = "5minute",
        direction: str | None = None,
        horizon_candles: int | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        direction = (direction or "BOTH").upper()
        horizon = horizon_candles or settings.backtest_horizon_candles
        underlying = self._load_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        if len(underlying) < 60:
            return {"status": "insufficient_data", "symbol": symbol, "candles": len(underlying), "minimum_required": 60}
        option_candles = self._load_option_candles(underlying=symbol, timeframe=timeframe)
        if not option_candles:
            return {"status": "insufficient_option_data", "symbol": symbol, "message": "No stored option premium candles found for ablation replay."}

        variants: dict[str, Any] = {}
        baseline_summary: dict[str, Any] | None = None
        for name, config in self.ABLATION_VARIANTS.items():
            if config.get("unsupported"):
                variants[name] = {"status": "unsupported", "reason": config["unsupported"]}
                continue
            signals = self._signals_from_underlying(
                underlying,
                direction=direction,
                horizon=horizon,
                disabled_factors=set(config.get("disabled", set())),
            )
            trades = [
                trade
                for idx, signal_direction, reason in signals
                if (trade := self._simulate_option_trade(underlying=underlying, option_candles=option_candles, idx=idx, direction=signal_direction, horizon=horizon, reason=f"{name}: {reason}"))
                is not None
            ]
            variants[name] = {
                "status": "ok",
                "disabled_factors": sorted(config.get("disabled", [])),
                "summary": self._summarize_option_trades(trades, timeframe=timeframe),
                "segments": self._segment_option_trades(trades, timeframe=timeframe),
            }
            if name == "baseline":
                baseline_summary = variants[name]["summary"]
        if baseline_summary:
            for name, result in variants.items():
                if result.get("status") != "ok":
                    continue
                result["impact_vs_baseline"] = self._ablation_impact(baseline_summary, result.get("summary", {}))
        return {
            "status": "ok",
            "mode": "ablation_option_replay",
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "horizon_candles": horizon,
            "variants": variants,
            "note": "Unsupported variants need stored option-chain/VIX/VWAP/quality snapshots before they can affect replay.",
        }

    def _run_option_premium_on_candles(
        self,
        *,
        symbol: str,
        timeframe: str,
        direction: str,
        horizon: int,
        underlying: list[Candle],
    ) -> dict[str, Any]:
        option_candles = self._load_option_candles(underlying=symbol, timeframe=timeframe)
        if not option_candles:
            return {"summary": self._summarize_option_trades([], timeframe=timeframe), "examples": []}
        signals = self._signals_from_underlying(underlying, direction=direction, horizon=horizon)
        trades = [
            trade
            for idx, signal_direction, reason in signals
            if (trade := self._simulate_option_trade(underlying=underlying, option_candles=option_candles, idx=idx, direction=signal_direction, horizon=horizon, reason=reason))
            is not None
        ]
        return {
            "summary": self._summarize_option_trades(trades, timeframe=timeframe),
            "segments": self._segment_option_trades(trades, timeframe=timeframe),
            "examples": [trade.to_dict() for trade in trades[-20:]],
        }

    def _load_candles(self, *, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.timestamp.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))
        finally:
            session.close()

    def _load_option_candles(self, *, underlying: str, timeframe: str) -> dict[str, list[Candle]]:
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol.like(f"{underlying.upper()}%"), Candle.timeframe == timeframe)
                .order_by(Candle.symbol.asc(), Candle.timestamp.asc())
                .all()
            )
            grouped: dict[str, list[Candle]] = {}
            for row in rows:
                if self._parse_option_symbol(row.symbol) is not None:
                    grouped.setdefault(row.symbol, []).append(row)
            return grouped
        finally:
            session.close()

    def _signals_from_underlying(self, candles: list[Candle], *, direction: str, horizon: int, disabled_factors: set[str] | None = None) -> list[tuple[int, str, str]]:
        closes = [float(item.close_price) for item in candles]
        highs = [float(item.high_price) for item in candles]
        lows = [float(item.low_price) for item in candles]
        volumes = [float(item.volume) for item in candles]
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)
        rsi = compute_rsi(closes, 14)
        macd, macd_signal = compute_macd(closes)
        signals: list[tuple[int, str, str]] = []
        warmup = 55
        last_signal_index = -horizon
        for idx in range(warmup, len(candles) - horizon):
            if idx - last_signal_index < max(3, horizon // 3):
                continue
            signal_direction, reason = self._signal_direction(
                idx=idx,
                closes=closes,
                highs=highs,
                lows=lows,
                volumes=volumes,
                ema9=ema9,
                ema21=ema21,
                ema50=ema50,
                rsi=rsi,
                macd=macd,
                macd_signal=macd_signal,
                disabled_factors=disabled_factors or set(),
            )
            if signal_direction is None:
                continue
            if direction in {"CALL", "CE", "BULLISH"} and signal_direction != "CALL":
                continue
            if direction in {"PUT", "PE", "BEARISH"} and signal_direction != "PUT":
                continue
            signals.append((idx, signal_direction, reason))
            last_signal_index = idx
        return signals

    def _signal_direction(
        self,
        *,
        idx: int,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        volumes: list[float],
        ema9: list[float],
        ema21: list[float],
        ema50: list[float],
        rsi: list[float],
        macd: list[float],
        macd_signal: list[float],
        disabled_factors: set[str] | None = None,
    ) -> tuple[str | None, str]:
        disabled_factors = disabled_factors or set()
        recent_high = max(highs[idx - 10 : idx])
        recent_low = min(lows[idx - 10 : idx])
        avg_volume = sum(volumes[idx - 20 : idx]) / 20
        volume_ok = volumes[idx] >= avg_volume * 1.15
        breakout_call = "breakout" in disabled_factors or closes[idx] > recent_high
        breakout_put = "breakout" in disabled_factors or closes[idx] < recent_low
        ema_call = "ema" in disabled_factors or ema9[idx] > ema21[idx] > ema50[idx]
        ema_put = "ema" in disabled_factors or ema9[idx] < ema21[idx] < ema50[idx]
        macd_call = "macd" in disabled_factors or macd[idx] > macd_signal[idx]
        macd_put = "macd" in disabled_factors or macd[idx] < macd_signal[idx]
        rsi_call = "rsi" in disabled_factors or 48 <= rsi[idx] <= 72
        rsi_put = "rsi" in disabled_factors or 28 <= rsi[idx] <= 52
        volume_call = "volume" in disabled_factors or volume_ok
        volume_put = "volume" in disabled_factors or volume_ok

        bullish = (
            breakout_call
            and ema_call
            and macd_call
            and rsi_call
            and volume_call
        )
        bearish = (
            breakout_put
            and ema_put
            and macd_put
            and rsi_put
            and volume_put
        )
        if bullish:
            return "CALL", "breakout with EMA alignment, MACD confirmation, RSI room, and volume expansion"
        if bearish:
            return "PUT", "breakdown with EMA alignment, MACD confirmation, RSI room, and volume expansion"
        return None, ""

    def _simulate_trade(self, *, candles: list[Candle], idx: int, direction: str, horizon: int, reason: str) -> BacktestTrade:
        entry = float(candles[idx].close_price)
        target_pct = 0.0065
        stop_pct = 0.0045
        if direction == "CALL":
            target = entry * (1 + target_pct)
            stop = entry * (1 - stop_pct)
        else:
            target = entry * (1 - target_pct)
            stop = entry * (1 + stop_pct)

        exit_price = float(candles[idx + horizon].close_price)
        outcome = "expired"
        bars_held = horizon
        for offset in range(1, horizon + 1):
            candle = candles[idx + offset]
            high = float(candle.high_price)
            low = float(candle.low_price)
            if direction == "CALL":
                if low <= stop:
                    exit_price = stop
                    outcome = "stop_loss"
                    bars_held = offset
                    break
                if high >= target:
                    exit_price = target
                    outcome = "target"
                    bars_held = offset
                    break
            else:
                if high >= stop:
                    exit_price = stop
                    outcome = "stop_loss"
                    bars_held = offset
                    break
                if low <= target:
                    exit_price = target
                    outcome = "target"
                    bars_held = offset
                    break

        move_pct = ((exit_price - entry) / entry) * 100
        if direction == "PUT":
            move_pct *= -1
        timestamp = candles[idx].timestamp
        if isinstance(timestamp, datetime):
            timestamp_text = timestamp.isoformat(sep=" ")
        else:
            timestamp_text = str(timestamp)
        return BacktestTrade(
            timestamp=timestamp_text,
            direction=direction,
            entry_price=round(entry, 2),
            exit_price=round(exit_price, 2),
            outcome=outcome,
            move_pct=round(move_pct, 3),
            bars_held=bars_held,
            reason=reason,
        )

    def _simulate_option_trade(
        self,
        *,
        underlying: list[Candle],
        option_candles: dict[str, list[Candle]],
        idx: int,
        direction: str,
        horizon: int,
        reason: str,
    ) -> OptionBacktestTrade | None:
        option_type = "CE" if direction == "CALL" else "PE"
        timestamp = underlying[idx].timestamp
        spot = float(underlying[idx].close_price)
        selected_symbol = self._select_option_symbol(option_candles, option_type=option_type, spot=spot, timestamp=timestamp)
        if selected_symbol is None:
            return None
        series = option_candles[selected_symbol]
        option_idx = self._find_candle_index_at_or_after(series, timestamp)
        if option_idx is None or option_idx + 1 >= len(series):
            return None

        entry_raw = float(series[option_idx].close_price)
        if entry_raw <= 0:
            return None
        entry_friction_pct = settings.backtest_slippage_pct + settings.paper_spread_impact_pct_per_side
        exit_friction_pct = settings.backtest_slippage_pct + settings.paper_spread_impact_pct_per_side
        entry = entry_raw * (1 + entry_friction_pct / 100)
        stop = entry * (1 - settings.backtest_option_stop_loss_pct / 100)
        target = entry * (1 + settings.backtest_option_target_pct / 100)
        exit_price = float(series[min(option_idx + horizon, len(series) - 1)].close_price) * (1 - exit_friction_pct / 100)
        outcome = "expired"
        bars_held = min(horizon, len(series) - option_idx - 1)

        for offset in range(1, min(horizon, len(series) - option_idx - 1) + 1):
            candle = series[option_idx + offset]
            low = float(candle.low_price)
            high = float(candle.high_price)
            if low <= stop:
                exit_price = stop * (1 - exit_friction_pct / 100)
                outcome = "stop_loss"
                bars_held = offset
                break
            if high >= target:
                exit_price = target * (1 - exit_friction_pct / 100)
                outcome = "target"
                bars_held = offset
                break

        gross_pnl_pct = ((exit_price - entry) / entry) * 100
        charges_pct = settings.backtest_charges_pct
        pnl_pct = gross_pnl_pct - charges_pct
        return OptionBacktestTrade(
            timestamp=timestamp.isoformat(sep=" ") if isinstance(timestamp, datetime) else str(timestamp),
            direction=direction,
            underlying_entry=round(spot, 2),
            tradingsymbol=selected_symbol,
            option_entry=round(entry, 2),
            option_exit=round(exit_price, 2),
            outcome=outcome,
            pnl_pct=round(pnl_pct, 3),
            gross_pnl_pct=round(gross_pnl_pct, 3),
            charges_pct=round(charges_pct, 3),
            slippage_pct=round(settings.backtest_slippage_pct * 2, 3),
            spread_impact_pct=round(settings.paper_spread_impact_pct_per_side * 2, 3),
            bars_held=bars_held,
            reason=reason,
        )

    def _summarize(self, trades: list[BacktestTrade]) -> dict[str, Any]:
        if not trades:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "average_win_pct": 0.0,
                "average_loss_pct": 0.0,
                "avg_move_pct": 0.0,
                "expectancy_pct": 0.0,
                "profit_factor": None,
                "max_drawdown_pct": 0.0,
                "avg_bars_held": 0.0,
            }
        wins = [trade for trade in trades if trade.outcome == "target"]
        losses = [trade for trade in trades if trade.outcome == "stop_loss"]
        moves = [trade.move_pct for trade in trades]
        gross_win = sum(trade.move_pct for trade in wins)
        gross_loss = abs(sum(trade.move_pct for trade in losses))
        max_drawdown = self._max_drawdown([trade.move_pct for trade in trades])
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "expired": len([trade for trade in trades if trade.outcome == "expired"]),
            "win_rate": round((len(wins) / len(trades)) * 100, 2),
            "average_win_pct": round(gross_win / len(wins), 3) if wins else 0.0,
            "average_loss_pct": round(gross_loss / len(losses), 3) if losses else 0.0,
            "avg_move_pct": round(sum(moves) / len(moves), 3),
            "expectancy_pct": round((gross_win - gross_loss) / len(trades), 3),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown_pct": round(max_drawdown, 3),
            "avg_bars_held": round(sum(trade.bars_held for trade in trades) / len(trades), 2),
        }

    def _summarize_option_trades(self, trades: list[OptionBacktestTrade], timeframe: str = "5minute") -> dict[str, Any]:
        if not trades:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "expired": 0,
                "win_rate": 0.0,
                "average_win_pct": 0.0,
                "average_loss_pct": 0.0,
                "avg_pnl_pct": 0.0,
                "expectancy_pct": 0.0,
                "profit_factor": None,
                "max_drawdown_pct": 0.0,
                "charges_pct_per_trade": settings.backtest_charges_pct,
                "slippage_pct_per_side": settings.backtest_slippage_pct,
                "avg_bars_held": 0.0,
                "avg_time_in_trade_minutes": 0.0,
                "gross_pnl_pct": 0.0,
                "net_pnl_pct": 0.0,
            }
        wins = [trade for trade in trades if trade.pnl_pct > 0]
        losses = [trade for trade in trades if trade.pnl_pct < 0]
        gross_win = sum(trade.pnl_pct for trade in wins)
        gross_loss = abs(sum(trade.pnl_pct for trade in losses))
        raw_gross_win = sum(trade.gross_pnl_pct for trade in trades if trade.gross_pnl_pct > 0)
        raw_gross_loss = abs(sum(trade.gross_pnl_pct for trade in trades if trade.gross_pnl_pct < 0))
        avg_bars = sum(trade.bars_held for trade in trades) / len(trades)
        avg_minutes = avg_bars * self._timeframe_minutes(timeframe)
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "expired": len([trade for trade in trades if trade.outcome == "expired"]),
            "win_rate": round((len(wins) / len(trades)) * 100, 2),
            "average_win_pct": round(gross_win / len(wins), 3) if wins else 0.0,
            "average_loss_pct": round(gross_loss / len(losses), 3) if losses else 0.0,
            "avg_pnl_pct": round(sum(trade.pnl_pct for trade in trades) / len(trades), 3),
            "expectancy_pct": round((gross_win - gross_loss) / len(trades), 3),
            "gross_expectancy_pct": round((raw_gross_win - raw_gross_loss) / len(trades), 3),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "gross_profit_factor": round(raw_gross_win / raw_gross_loss, 2) if raw_gross_loss else None,
            "max_drawdown_pct": round(self._max_drawdown([trade.pnl_pct for trade in trades]), 3),
            "charges_pct_per_trade": settings.backtest_charges_pct,
            "slippage_pct_per_side": settings.backtest_slippage_pct,
            "spread_impact_pct_per_side": settings.paper_spread_impact_pct_per_side,
            "avg_bars_held": round(avg_bars, 2),
            "avg_time_in_trade_minutes": round(avg_minutes, 2),
            "gross_pnl_pct": round(sum(trade.gross_pnl_pct for trade in trades), 3),
            "net_pnl_pct": round(sum(trade.pnl_pct for trade in trades), 3),
        }

    def _ablation_impact(self, baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        baseline_trades = int(baseline.get("trades") or 0)
        current_trades = int(current.get("trades") or 0)
        baseline_wins = int(baseline.get("wins") or 0)
        current_wins = int(current.get("wins") or 0)
        return {
            "net_expectancy_delta": round(float(current.get("expectancy_pct") or 0.0) - float(baseline.get("expectancy_pct") or 0.0), 3),
            "win_rate_delta": round(float(current.get("win_rate") or 0.0) - float(baseline.get("win_rate") or 0.0), 2),
            "profit_factor_delta": self._optional_delta(current.get("profit_factor"), baseline.get("profit_factor")),
            "max_drawdown_delta": round(float(current.get("max_drawdown_pct") or 0.0) - float(baseline.get("max_drawdown_pct") or 0.0), 3),
            "trade_count_delta": current_trades - baseline_trades,
            "missed_winning_trades": max(0, baseline_wins - current_wins),
        }

    def _optional_delta(self, current: Any, baseline: Any) -> float | None:
        if current is None or baseline is None:
            return None
        return round(float(current) - float(baseline), 3)

    def _segment_option_trades(self, trades: list[OptionBacktestTrade], timeframe: str = "5minute") -> dict[str, Any]:
        return {
            "ce_vs_pe": self._summarize_groups(trades, lambda trade: "CE" if trade.direction == "CALL" else "PE", timeframe),
            "expiry_day": self._summarize_groups(trades, lambda trade: "expiry_day" if self._is_expiry_day_trade(trade) else "non_expiry_or_unknown", timeframe),
            "time_bucket": self._summarize_groups(trades, lambda trade: self._time_bucket(trade.timestamp), timeframe),
            "moneyness": self._summarize_groups(trades, self._moneyness_bucket, timeframe),
            "setup": self._summarize_groups(trades, lambda trade: trade.reason.split(":", 1)[0] if ":" in trade.reason else trade.reason[:60], timeframe),
        }

    def _summarize_groups(self, trades: list[OptionBacktestTrade], key_fn: Any, timeframe: str) -> dict[str, Any]:
        groups: dict[str, list[OptionBacktestTrade]] = {}
        for trade in trades:
            groups.setdefault(str(key_fn(trade)), []).append(trade)
        return {key: self._summarize_option_trades(items, timeframe=timeframe) for key, items in groups.items()}

    def _time_bucket(self, timestamp: str) -> str:
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return "unknown"
        total = parsed.hour * 60 + parsed.minute
        if total < 10 * 60:
            return "open_early"
        if total < 11 * 60 + 30:
            return "morning"
        if total < 13 * 60 + 30:
            return "midday"
        if total < 14 * 60 + 45:
            return "afternoon"
        return "closing"

    def _moneyness_bucket(self, trade: OptionBacktestTrade) -> str:
        parsed = self._parse_option_symbol(trade.tradingsymbol)
        if parsed is None:
            return "unknown"
        distance_pct = ((float(parsed["strike"]) - trade.underlying_entry) / max(trade.underlying_entry, 0.01)) * 100
        if abs(distance_pct) <= 0.15:
            return "ATM"
        if trade.direction == "CALL":
            return "ITM" if distance_pct < 0 else "OTM"
        return "ITM" if distance_pct > 0 else "OTM"

    def _is_expiry_day_trade(self, trade: OptionBacktestTrade) -> bool:
        expiry = self._parse_expiry_from_symbol(trade.tradingsymbol, trade.timestamp)
        if expiry is None:
            return False
        try:
            trade_date = datetime.fromisoformat(trade.timestamp).date()
        except ValueError:
            return False
        return trade_date == expiry.date()

    def _parse_expiry_from_symbol(self, symbol: str, timestamp: str) -> datetime | None:
        match = re.search(r"(?P<day>\d{2})(?P<month>[A-Z]{3})\d+(?:\.\d+)?(?:CE|PE)$", symbol.upper())
        if not match:
            return None
        try:
            year = datetime.fromisoformat(timestamp).year
            return datetime.strptime(f"{year}-{match.group('month')}-{match.group('day')}", "%Y-%b-%d")
        except ValueError:
            return None

    def _max_drawdown(self, returns_pct: list[float]) -> float:
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in returns_pct:
            equity += value
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        return abs(max_drawdown)

    def _timeframe_minutes(self, timeframe: str) -> float:
        match = re.match(r"(\d+)", timeframe)
        return float(match.group(1)) if match else 5.0

    def _validation_passed(self, summary: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if int(summary.get("trades") or 0) < settings.min_strategy_trades:
            reasons.append("not enough out-of-sample trades")
        if float(summary.get("expectancy_pct") or 0.0) < settings.min_strategy_expectancy_pct:
            reasons.append("out-of-sample expectancy is below threshold")
        profit_factor = summary.get("profit_factor")
        if profit_factor is None or float(profit_factor) < settings.min_strategy_profit_factor:
            reasons.append("out-of-sample profit factor is below threshold")
        if float(summary.get("win_rate") or 0.0) < settings.min_strategy_win_rate_pct:
            reasons.append("out-of-sample win rate is below threshold")
        return not reasons, reasons

    def _select_option_symbol(self, option_candles: dict[str, list[Candle]], *, option_type: str, spot: float, timestamp: datetime) -> str | None:
        candidates: list[tuple[float, str]] = []
        for symbol, candles in option_candles.items():
            parsed = self._parse_option_symbol(symbol)
            if parsed is None or parsed["option_type"] != option_type:
                continue
            if self._find_candle_index_at_or_after(candles, timestamp) is None:
                continue
            candidates.append((abs(float(parsed["strike"]) - spot), symbol))
        if not candidates:
            return None
        return sorted(candidates)[0][1]

    def _find_candle_index_at_or_after(self, candles: list[Candle], timestamp: datetime) -> int | None:
        target = timestamp.replace(tzinfo=None) if isinstance(timestamp, datetime) else timestamp
        for idx, candle in enumerate(candles):
            candle_ts = candle.timestamp.replace(tzinfo=None) if isinstance(candle.timestamp, datetime) else candle.timestamp
            if candle_ts >= target:
                return idx
        return None

    def _parse_option_symbol(self, symbol: str) -> dict[str, Any] | None:
        match = re.search(r"(?P<strike>\d+(?:\.\d+)?)(?P<option_type>CE|PE)$", symbol.upper())
        if not match:
            return None
        return {"strike": float(match.group("strike")), "option_type": match.group("option_type")}
