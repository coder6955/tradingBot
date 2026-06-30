from __future__ import annotations

from typing import List

from app.models import Signal
from app.config import settings
from app.services.indicator_scoring_service import IndicatorScoringService
from app.services.mock_market_feed import MockMarketFeed
from app.providers.kite_feed import KiteFeed
from app.providers.token_store import load_access_token
from app.services.market_regime_service import MarketRegimeService
from app.services.option_chain_service import OptionChainService
from app.services.price_action_service import PriceActionService
from app.services.signal_service import SignalService
from app.services.trade_setup_service import OptionContract, TradeSetupService


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
        market_regime_service: MarketRegimeService | None = None,
        price_action_service: PriceActionService | None = None,
        option_chain_service: OptionChainService | None = None,
    ) -> None:
        self.signal_service = signal_service or SignalService()
        self.scoring_service = scoring_service or IndicatorScoringService()
        self.trade_setup_service = trade_setup_service or TradeSetupService()
        self.market_regime_service = market_regime_service or MarketRegimeService()
        self.price_action_service = price_action_service or PriceActionService()
        self.option_chain_service = option_chain_service or OptionChainService()

        # Market data is independent from live order placement. When Kite data is
        # requested, fail closed instead of silently returning mock opportunities.
        if feed is not None:
            self.feed = feed
        else:
            access_token = load_access_token() or settings.kite_access_token
            if settings.use_kite_market_data and access_token:
                self.feed = KiteFeed()
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
        diagnostics: List[dict[str, object]] = []
        scores = scores or {}
        confidences = confidences or {}
        trends = trends or {}
        market_contexts = market_contexts or {}
        option_instruments = self._get_option_instruments()
        symbols = symbols or self._derive_scan_universe(option_instruments)
        market_snapshots = self._market_snapshots()

        for symbol in symbols:
            snapshot = self.feed.get_snapshot(symbol)
            if settings.use_kite_market_data and not snapshot.get("is_real_data"):
                diagnostics.append(
                    self._diagnostic(
                        symbol,
                        snapshot,
                        0,
                        "unknown",
                        "unknown",
                        side,
                        None,
                        ["real Kite market data was not available for this symbol"],
                    )
                )
                continue

            inferred_trend = trends.get(symbol, "bullish" if snapshot["trend_bullish"] else "bearish")
            technical_score = self._technical_score(snapshot, inferred_trend)
            score = scores.get(symbol, technical_score)
            trend = inferred_trend
            market_context = market_contexts.get(symbol, snapshot["market_context"])
            reasons: list[str] = []
            chain_quote_map = self._quote_chain_options(option_instruments, symbol, float(snapshot["price"]))
            contract = self.trade_setup_service.select_contract(
                instruments=option_instruments,
                underlying=symbol,
                spot_price=float(snapshot["price"]),
                trend=trend,
                side=side,
                quotes=chain_quote_map,
            )
            if contract is None:
                if score < settings.min_signal_score:
                    reasons.append("technical score is below threshold")
                if not option_instruments:
                    reasons.append("no NFO option instruments available from Kite")
                else:
                    reasons.append("no matching option contract found")
                diagnostics.append(self._diagnostic(symbol, snapshot, score, trend, market_context, side, None, reasons))
                continue

            entry_price = contract.ask or contract.last_price if side.upper() == "BUY" else contract.bid or contract.last_price
            prices = self.trade_setup_service.build_prices(entry_price=max(entry_price, 0.05), side=side)
            liquidity_score = self.trade_setup_service.liquidity_score(contract)
            chain_contracts = self.trade_setup_service.build_contracts(option_instruments, symbol, chain_quote_map)
            market_eval = self.market_regime_service.evaluate(
                symbol=symbol,
                trend=trend,
                side=side,
                nifty=market_snapshots.get("NIFTY"),
                banknifty=market_snapshots.get("BANKNIFTY"),
                vix=market_snapshots.get("INDIAVIX"),
            )
            price_eval = self.price_action_service.evaluate(snapshot, trend, side)
            chain_eval = self.option_chain_service.analyze(
                spot_price=float(snapshot["price"]),
                trend=trend,
                side=side,
                selected=contract,
                contracts=chain_contracts,
            )
            combined_score = self._final_score(
                technical_score=score,
                market_score=int(market_eval["score"]),
                price_score=int(price_eval["score"]),
                chain_score=int(chain_eval["score"]),
                liquidity_score=liquidity_score,
            )
            factor_scores = {
                "technical": score,
                "market_regime": market_eval,
                "price_action": price_eval,
                "option_chain": chain_eval,
                "liquidity": liquidity_score,
                "contract": self._contract_payload(contract),
                "prices": prices,
            }
            risk_failures = self._gate_failures(
                combined_score=combined_score,
                contract=contract,
                prices=prices,
                side=side,
                market_eval=market_eval,
                price_eval=price_eval,
                chain_eval=chain_eval,
            )
            if risk_failures:
                diagnostics.append(
                    self._diagnostic(
                        symbol,
                        snapshot,
                        combined_score,
                        trend,
                        market_context,
                        side,
                        None,
                        risk_failures,
                        factor_scores,
                    )
                )
                continue

            confidence = confidences.get(symbol, combined_score / 100.0)
            probability = min(0.92, (combined_score / 100.0) * 0.72 + (liquidity_score / 100.0) * 0.12 + (int(chain_eval["score"]) / 100.0) * 0.08)
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
                    target_3=prices["target_3"],
                    side=side,
                    tradingsymbol=contract.tradingsymbol,
                    exchange=contract.exchange,
                    instrument_token=contract.instrument_token,
                    quantity=quantity,
                    lot_size=contract.lot_size,
                    probability=probability,
                    risk_reward=prices["risk_reward"],
                    setup_type=self._setup_type(side, trend),
                    technical_score=score,
                    market_regime_score=int(market_eval["score"]),
                    price_action_score=int(price_eval["score"]),
                    option_chain_score=int(chain_eval["score"]),
                    liquidity_score=liquidity_score,
                    factor_scores=factor_scores,
                    risk_notes=[],
                )
                diagnostics.append(self._diagnostic(symbol, snapshot, combined_score, trend, market_context, side, signal, [], factor_scores))
            else:
                diagnostics.append(
                    self._diagnostic(
                        symbol,
                        snapshot,
                        combined_score,
                        trend,
                        market_context,
                        side,
                        None,
                        ["final multi-factor score is below threshold"],
                        factor_scores,
                    )
                )
        return sorted(diagnostics, key=lambda item: (bool(item.get("signal")), int(item["score"])), reverse=True)

    def _get_option_instruments(self) -> List[dict[str, object]]:
        if hasattr(self.feed, "get_instruments"):
            return self.feed.get_instruments(settings.option_exchange)  # type: ignore
        return []

    def _derive_scan_universe(self, option_instruments: List[dict[str, object]]) -> List[str]:
        if not option_instruments:
            return self.DEFAULT_UNIVERSE

        names = {
            str(item.get("name", "")).upper().replace(" ", "")
            for item in option_instruments
            if item.get("name") and item.get("instrument_type") in {"CE", "PE"}
        }
        priority = [symbol for symbol in self.DEFAULT_UNIVERSE if symbol in names]
        remaining = sorted(name for name in names if name and name not in priority)
        return (priority + remaining)[: settings.max_scan_symbols]

    def _market_snapshots(self) -> dict[str, dict[str, object]]:
        if not settings.use_kite_market_data:
            return {}
        return {
            "NIFTY": self.feed.get_snapshot("NIFTY"),
            "BANKNIFTY": self.feed.get_snapshot("BANKNIFTY"),
            "INDIAVIX": self.feed.get_snapshot("INDIAVIX"),
        }

    def _quote_chain_options(
        self,
        option_instruments: List[dict[str, object]],
        symbol: str,
        spot_price: float,
    ) -> dict[str, object]:
        if not hasattr(self.feed, "get_quotes") or not option_instruments:
            return {}
        nearby = sorted(
            [
                item
                for item in option_instruments
                if self._instrument_matches(item, symbol)
                and str(item.get("instrument_type")) in {"CE", "PE"}
                and abs(float(item.get("strike") or 0.0) - spot_price) <= max(spot_price * 0.04, 300)
            ],
            key=lambda item: abs(float(item.get("strike") or 0.0) - spot_price),
        )[:120]
        instruments = [f"{settings.option_exchange}:{item['tradingsymbol']}" for item in nearby if item.get("tradingsymbol")]
        return self.feed.get_quotes(instruments)  # type: ignore

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
        nearby = sorted([
            item
            for item in option_instruments
            if str(item.get("instrument_type")) == option_type
            and self._instrument_matches(item, symbol)
            and abs(float(item.get("strike") or 0.0) - spot_price) <= max(spot_price * 0.03, 250)
        ], key=lambda item: abs(float(item.get("strike") or 0.0) - spot_price))[:80]
        symbols = [f"{settings.option_exchange}:{item['tradingsymbol']}" for item in nearby if item.get("tradingsymbol")]
        return self.feed.get_quotes(symbols)  # type: ignore

    def _instrument_matches(self, item: dict[str, object], symbol: str) -> bool:
        target = symbol.upper().replace(" ", "")
        name = str(item.get("name") or "").upper().replace(" ", "")
        tradingsymbol = str(item.get("tradingsymbol") or "").upper().replace(" ", "")
        return name == target or tradingsymbol.startswith(target)

    def _final_score(
        self,
        technical_score: int,
        market_score: int,
        price_score: int,
        chain_score: int,
        liquidity_score: int,
    ) -> int:
        score = (
            technical_score * 0.22
            + market_score * 0.16
            + price_score * 0.22
            + chain_score * 0.25
            + liquidity_score * 0.15
        )
        return min(100, max(0, round(score)))

    def _technical_score(self, snapshot: dict[str, object], trend: str) -> int:
        bullish = trend.lower() == "bullish"
        if bullish:
            return self.scoring_service.score_symbol(
                rsi=float(snapshot["rsi"]),
                adx=float(snapshot["adx"]),
                macd_positive=bool(snapshot["macd_positive"]),
                ema_alignment=bool(snapshot["ema_alignment"]),
                vwap_above_price=bool(snapshot["vwap_above_price"]),
                volume_confirmed=bool(snapshot["volume_confirmed"]),
                trend_bullish=bool(snapshot["trend_bullish"]),
                market_context=str(snapshot["market_context"]),
            )

        score = 0
        rsi = float(snapshot.get("rsi") or 50)
        adx = float(snapshot.get("adx") or 0)
        if not bool(snapshot.get("trend_bullish")):
            score += 25
        if not bool(snapshot.get("ema_alignment")):
            score += 15
        if not bool(snapshot.get("vwap_above_price")):
            score += 10
        if not bool(snapshot.get("macd_positive")):
            score += 10
        if bool(snapshot.get("volume_confirmed")):
            score += 10
        if 30 <= rsi <= 50:
            score += 10
        if adx >= 20:
            score += 10
        if str(snapshot.get("market_context", "")).lower() == "strong":
            score += 10
        return min(score, 100)

    def _gate_failures(
        self,
        combined_score: int,
        contract: OptionContract,
        prices: dict[str, float],
        side: str,
        market_eval: dict[str, object],
        price_eval: dict[str, object],
        chain_eval: dict[str, object],
    ) -> list[str]:
        failures = self.trade_setup_service.risk_checks(combined_score, contract, prices["entry_price"], side)
        if prices["risk_reward"] < settings.min_risk_reward:
            failures.append("risk/reward is below threshold")
        if int(market_eval["score"]) < settings.min_market_regime_score or not market_eval["passed"]:
            failures.extend(str(reason) for reason in market_eval.get("reasons", []))
        if int(price_eval["score"]) < settings.min_price_action_score or not price_eval["passed"]:
            failures.extend(str(reason) for reason in price_eval.get("reasons", []))
        if int(chain_eval["score"]) < settings.min_option_chain_score or not chain_eval["passed"]:
            failures.extend(str(reason) for reason in chain_eval.get("reasons", []))
        if combined_score < settings.min_signal_score:
            failures.append("final multi-factor score is below threshold")
        return list(dict.fromkeys(failures))

    def _setup_type(self, side: str, trend: str) -> str:
        if side.upper() == "SELL":
            return "credit_put_sell" if trend.lower() == "bullish" else "credit_call_sell"
        return "directional_call_buy" if trend.lower() == "bullish" else "directional_put_buy"

    def _contract_payload(self, contract: OptionContract) -> dict[str, object]:
        return {
            "tradingsymbol": contract.tradingsymbol,
            "exchange": contract.exchange,
            "strike": contract.strike,
            "expiry": contract.expiry,
            "option_type": contract.option_type,
            "lot_size": contract.lot_size,
            "last_price": contract.last_price,
            "bid": contract.bid,
            "ask": contract.ask,
            "open_interest": contract.open_interest,
            "volume": contract.volume,
        }

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
        factor_scores: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "symbol": symbol,
            "score": score,
            "trend": trend,
            "side": side.upper(),
            "market_context": market_context,
            "price": snapshot.get("price"),
            "source": snapshot.get("source"),
            "is_real_data": snapshot.get("is_real_data"),
            "rsi": snapshot.get("rsi"),
            "adx": snapshot.get("adx"),
            "vwap": snapshot.get("vwap"),
            "ema_9": snapshot.get("ema_9"),
            "ema_21": snapshot.get("ema_21"),
            "passed": signal is not None,
            "reasons": reasons,
            "factor_scores": factor_scores or {},
            "signal": signal,
        }
