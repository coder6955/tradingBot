from __future__ import annotations

from typing import List

from app.models import Signal
from app.config import settings
from app.services.indicator_scoring_service import IndicatorScoringService
from app.services.mock_market_feed import MockMarketFeed
from app.providers.kite_feed import KiteFeed
from app.services.signal_service import SignalService
from app.services.trade_setup_service import TradeSetupService


class ScannerService:
    """Produce ranked trade recommendations using mock feed data and indicator scoring."""

    DEFAULT_UNIVERSE = [
        "NIFTY",
        "BANKNIFTY",
        "FINNIFTY",
        "RELIANCE",
        "TCS",
        "INFY",
        "HDFCBANK",
        "ICICIBANK",
        "SBIN",
        "AXISBANK",
        "LT",
        "TATAMOTORS",
        "MARUTI",
    ]

    def __init__(
        self,
        signal_service: SignalService | None = None,
        scoring_service: IndicatorScoringService | None = None,
        feed: MockMarketFeed | None = None,
        trade_setup_service: TradeSetupService | None = None,
    ) -> None:
        self.signal_service = signal_service or SignalService()
        self.scoring_service = scoring_service or IndicatorScoringService()
        self.trade_setup_service = trade_setup_service or TradeSetupService()

        # Select feed implementation: KiteFeed when live trading is enabled
        if feed is not None:
            self.feed = feed
        else:
            if settings.use_kite_market_data and settings.kite_access_token:
                try:
                    self.feed = KiteFeed()
                except Exception:
                    self.feed = MockMarketFeed()
            else:
                self.feed = MockMarketFeed()

    def scan_symbols(
        self,
        symbols: List[str] | None = None,
        scores: dict[str, int] | None = None,
        confidences: dict[str, float] | None = None,
        trends: dict[str, str] | None = None,
        market_contexts: dict[str, str] | None = None,
        side: str = "BUY",
    ) -> List[Signal]:
        return [item["signal"] for item in self.scan_with_diagnostics(symbols, scores, confidences, trends, market_contexts, side) if item.get("signal")]

    def scan_with_diagnostics(
        self,
        symbols: List[str] | None = None,
        scores: dict[str, int] | None = None,
        confidences: dict[str, float] | None = None,
        trends: dict[str, str] | None = None,
        market_contexts: dict[str, str] | None = None,
        side: str = "BUY",
    ) -> List[dict[str, object]]:
        recommendations: List[Signal] = []
        diagnostics: List[dict[str, object]] = []
        symbols = symbols or self.DEFAULT_UNIVERSE
        scores = scores or {}
        confidences = confidences or {}
        trends = trends or {}
        market_contexts = market_contexts or {}
        option_instruments = self._get_option_instruments()

        for symbol in symbols:
            snapshot = self.feed.get_snapshot(symbol)
            technical_score = self.scoring_service.score_symbol(
                rsi=snapshot["rsi"],
                adx=snapshot["adx"],
                macd_positive=snapshot["macd_positive"],
                ema_alignment=snapshot["ema_alignment"],
                vwap_above_price=snapshot["vwap_above_price"],
                volume_confirmed=snapshot["volume_confirmed"],
                trend_bullish=snapshot["trend_bullish"],
                market_context=snapshot["market_context"],
            )
            score = scores.get(symbol, technical_score)
            trend = trends.get(symbol, "bullish" if snapshot["trend_bullish"] else "bearish")
            market_context = market_contexts.get(symbol, snapshot["market_context"])
            reasons: list[str] = []
            option_quote_map = self._quote_nearby_options(option_instruments, symbol, float(snapshot["price"]), trend, side)
            contract = self.trade_setup_service.select_contract(
                instruments=option_instruments,
                underlying=symbol,
                spot_price=float(snapshot["price"]),
                trend=trend,
                side=side,
                quotes=option_quote_map,
            )
            if contract is None:
                if score >= settings.min_signal_score:
                    signal = self.signal_service.generate_signal(
                        symbol=symbol,
                        score=score,
                        confidence=confidences.get(symbol, score / 100.0),
                        trend=trend,
                        market_context=market_context,
                        side=side,
                    )
                    recommendations.append(signal)
                    diagnostics.append(self._diagnostic(symbol, snapshot, score, trend, market_context, side, signal, ["no matching option contract found; returning underlying-level signal"]))
                    continue
                reasons.append("technical score is below threshold")
                if not option_instruments:
                    reasons.append("no NFO option instruments available; configure Kite or use mock index symbols")
                else:
                    reasons.append("no matching option contract found")
                diagnostics.append(self._diagnostic(symbol, snapshot, score, trend, market_context, side, None, reasons))
                continue

            entry_price = contract.ask or contract.last_price if side.upper() == "BUY" else contract.bid or contract.last_price
            prices = self.trade_setup_service.build_prices(entry_price=max(entry_price, 0.05), side=side)
            liquidity_score = self.trade_setup_service.liquidity_score(contract)
            combined_score = min(100, round((score * 0.7) + (liquidity_score * 0.3)))
            risk_failures = self.trade_setup_service.risk_checks(combined_score, contract, prices["entry_price"], side)
            if risk_failures:
                diagnostics.append(self._diagnostic(symbol, snapshot, combined_score, trend, market_context, side, None, risk_failures))
                continue

            confidence = confidences.get(symbol, combined_score / 100.0)
            probability = min(0.92, (combined_score / 100.0) * 0.75 + (liquidity_score / 100.0) * 0.17)
            quantity = self.trade_setup_service.position_size(
                entry_price=prices["entry_price"],
                stop_loss=prices["stop_loss"],
                lot_size=contract.lot_size,
                side=side,
            )
            if combined_score >= settings.min_signal_score:
                signal = self.signal_service.generate_signal(
                    symbol=symbol,
                    score=combined_score,
                    confidence=confidence,
                    trend=trend,
                    market_context=market_context,
                    strike=contract.strike,
                    expiry=contract.expiry,
                    entry_price=prices["entry_price"],
                    stop_loss=prices["stop_loss"],
                    target_1=prices["target_1"],
                    target_2=prices["target_2"],
                    side=side,
                    tradingsymbol=contract.tradingsymbol,
                    exchange=contract.exchange,
                    instrument_token=contract.instrument_token,
                    quantity=quantity,
                    lot_size=contract.lot_size,
                    probability=probability,
                    risk_reward=prices["risk_reward"],
                )
                recommendations.append(signal)
                diagnostics.append(self._diagnostic(symbol, snapshot, combined_score, trend, market_context, side, signal, []))
            else:
                diagnostics.append(self._diagnostic(symbol, snapshot, combined_score, trend, market_context, side, None, ["combined technical/liquidity score is below threshold"]))
        return sorted(diagnostics, key=lambda item: (bool(item.get("signal")), int(item["score"])), reverse=True)

    def _get_option_instruments(self) -> List[dict[str, object]]:
        if hasattr(self.feed, "get_instruments"):
            return self.feed.get_instruments(settings.option_exchange)  # type: ignore
        return []

    def _quote_nearby_options(
        self,
        option_instruments: List[dict[str, object]],
        symbol: str,
        spot_price: float,
        trend: str,
        side: str,
    ) -> dict[str, object]:
        if not hasattr(self.feed, "get_quotes") or not option_instruments:
            return {}
        option_type = self.trade_setup_service.option_type_for(trend, side)
        nearby = [
            item
            for item in option_instruments
            if str(item.get("instrument_type")) == option_type
            and str(item.get("tradingsymbol", "")).upper().startswith(symbol.upper())
            and abs(float(item.get("strike") or 0.0) - spot_price) <= max(spot_price * 0.03, 250)
        ][:40]
        symbols = [f"{settings.option_exchange}:{item['tradingsymbol']}" for item in nearby if item.get("tradingsymbol")]
        return self.feed.get_quotes(symbols)  # type: ignore

    def _diagnostic(
        self,
        symbol: str,
        snapshot: dict[str, object],
        score: int,
        trend: str,
        market_context: str,
        side: str,
        signal: Signal | None,
        reasons: list[str],
    ) -> dict[str, object]:
        return {
            "symbol": symbol,
            "score": score,
            "trend": trend,
            "side": side.upper(),
            "market_context": market_context,
            "price": snapshot.get("price"),
            "rsi": snapshot.get("rsi"),
            "adx": snapshot.get("adx"),
            "passed": signal is not None,
            "reasons": reasons,
            "signal": signal,
        }
