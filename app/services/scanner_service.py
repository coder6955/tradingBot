from __future__ import annotations

import logging
from typing import List

from app.models import Signal
from app.config import settings
from app.services.indicator_scoring_service import IndicatorScoringService
from app.services.banknifty_intelligence_service import BankNiftyIntelligenceService
from app.services.day_type_service import DayTypeService
from app.services.data_freshness_service import DataFreshnessService
from app.services.decision_engine_service import DecisionEngineService
from app.services.mock_market_feed import MockMarketFeed
from app.providers.kite_feed import KiteFeed
from app.providers.token_store import load_access_token
from app.services.market_regime_service import MarketRegimeService
from app.services.option_chain_service import OptionChainService
from app.services.option_premium_confirmation_service import OptionPremiumConfirmationService
from app.services.option_quality_service import OptionQualityService
from app.services.outcome_learning_service import OutcomeLearningService
from app.services.price_action_service import PriceActionService
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.signal_service import SignalService
from app.services.strategy_edge_service import StrategyEdgeService
from app.services.time_bucket_edge_service import TimeBucketEdgeService
from app.services.trade_setup_service import OptionContract, TradeSetupService


logger = logging.getLogger(__name__)


class ScannerService:
    """Produce ranked trade recommendations using mock feed data and indicator scoring."""

    FOCUS_UNDERLYINGS = {"BANKNIFTY"}
    DEFAULT_UNIVERSE = ["BANKNIFTY"]

    def __init__(
        self,
        signal_service: SignalService | None = None,
        scoring_service: IndicatorScoringService | None = None,
        feed: MockMarketFeed | None = None,
        trade_setup_service: TradeSetupService | None = None,
        market_regime_service: MarketRegimeService | None = None,
        price_action_service: PriceActionService | None = None,
        option_chain_service: OptionChainService | None = None,
        option_quality_service: OptionQualityService | None = None,
        strategy_edge_service: StrategyEdgeService | None = None,
        day_type_service: DayTypeService | None = None,
        option_premium_confirmation_service: OptionPremiumConfirmationService | None = None,
        time_bucket_edge_service: TimeBucketEdgeService | None = None,
        outcome_learning_service: OutcomeLearningService | None = None,
        banknifty_intelligence_service: BankNiftyIntelligenceService | None = None,
        data_freshness_service: DataFreshnessService | None = None,
        rejected_opportunity_repository: RejectedOpportunityRepository | None = None,
        decision_engine_service: DecisionEngineService | None = None,
    ) -> None:
        self.signal_service = signal_service or SignalService()
        self.scoring_service = scoring_service or IndicatorScoringService()
        self.trade_setup_service = trade_setup_service or TradeSetupService()
        self.market_regime_service = market_regime_service or MarketRegimeService()
        self.price_action_service = price_action_service or PriceActionService()
        self.option_chain_service = option_chain_service or OptionChainService()
        self.option_quality_service = option_quality_service or OptionQualityService()
        self.strategy_edge_service = strategy_edge_service or StrategyEdgeService()
        self.day_type_service = day_type_service or DayTypeService()
        self.option_premium_confirmation_service = option_premium_confirmation_service or OptionPremiumConfirmationService()
        self.time_bucket_edge_service = time_bucket_edge_service or TimeBucketEdgeService()
        self.outcome_learning_service = outcome_learning_service or OutcomeLearningService()
        self.banknifty_intelligence_service = banknifty_intelligence_service or BankNiftyIntelligenceService()
        self.data_freshness_service = data_freshness_service or DataFreshnessService()
        self.rejected_opportunity_repository = rejected_opportunity_repository or RejectedOpportunityRepository()
        self.decision_engine_service = decision_engine_service or DecisionEngineService()

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
        order_mode: str = "paper",
    ) -> List[Signal]:
        return [
            item["signal"]
            for item in self.scan_with_diagnostics(symbols, scores, confidences, trends, market_contexts, side, order_mode=order_mode)
            if item.get("signal")
        ]

    def scan_with_diagnostics(
        self,
        symbols: List[str] | None = None,
        scores: dict[str, int] | None = None,
        confidences: dict[str, float] | None = None,
        trends: dict[str, str] | None = None,
        market_contexts: dict[str, str] | None = None,
        side: str = "BUY",
        order_mode: str = "paper",
    ) -> List[dict[str, object]]:
        diagnostics: List[dict[str, object]] = []
        scores = scores or {}
        confidences = confidences or {}
        trends = trends or {}
        market_contexts = market_contexts or {}
        option_instruments = self._get_option_instruments()
        symbols = self._focus_symbols(symbols or self._derive_scan_universe(option_instruments))
        market_snapshots = self._market_snapshots()
        enforce_budget = str(order_mode).lower() == "live"

        for symbol in symbols:
            snapshot = self.feed.get_snapshot(symbol)
            if settings.use_kite_market_data and not snapshot.get("is_real_data"):
                reasons = ["real Kite market data was not available for this symbol"]
                self._log_decision(
                    symbol=symbol,
                    accepted=False,
                    score=0,
                    reasons=reasons,
                    breakdown=self._empty_score_breakdown(),
                    snapshot=snapshot,
                    side=side,
                    trend="unknown",
                )
                self._save_rejection(symbol=symbol, side=side, trend="unknown", score=0, reasons=reasons, breakdown=self._empty_score_breakdown(), snapshot=snapshot)
                diagnostics.append(
                    self._diagnostic(
                        symbol,
                        snapshot,
                        0,
                        "unknown",
                        "unknown",
                        side,
                        None,
                        reasons,
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
                enforce_budget=enforce_budget,
            )
            if contract is None:
                if not option_instruments:
                    reasons.append("no NFO option instruments available from Kite")
                else:
                    reasons.append("no matching option contract found")
                self._log_decision(symbol=symbol, accepted=False, score=score, reasons=reasons, breakdown=self._empty_score_breakdown(), snapshot=snapshot, side=side, trend=trend)
                self._save_rejection(symbol=symbol, side=side, trend=trend, score=score, reasons=reasons, breakdown=self._empty_score_breakdown(), snapshot=snapshot)
                diagnostics.append(self._diagnostic(symbol, snapshot, score, trend, market_context, side, None, reasons))
                continue

            entry_price = contract.ask or contract.last_price if side.upper() == "BUY" else contract.bid or contract.last_price
            freshness_eval = self.data_freshness_service.validate_scan_inputs(
                order_mode=order_mode,
                snapshot=snapshot,
                chain_quotes=chain_quote_map,
                contract=contract,
            )
            prices = self.trade_setup_service.build_prices(
                entry_price=max(entry_price, 0.05),
                side=side,
                underlying=symbol,
                snapshot=snapshot,
                contract=contract,
            )
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
            quality_eval = self.option_quality_service.evaluate(
                spot_price=float(snapshot["price"]),
                contract=contract,
                entry_price=prices["entry_price"],
                side=side,
            )
            day_type_eval = self.day_type_service.evaluate(symbol=symbol, trend=trend)
            premium_eval = self.option_premium_confirmation_service.evaluate(contract=contract, side=side)
            time_bucket_eval = self.time_bucket_edge_service.evaluate(symbol=symbol, trend=trend)
            edge_eval = self._strategy_edge_eval(symbol, trend)
            banknifty_eval = self.banknifty_intelligence_service.evaluate(
                trend=trend,
                snapshot=snapshot,
                market_snapshots=market_snapshots,
                contract=contract,
                chain_contracts=chain_contracts,
                prices=prices,
                premium_eval=premium_eval,
                day_type_eval=day_type_eval,
            )
            score_breakdown = self._score_breakdown(
                technical_score=score,
                market_score=int(market_eval["score"]),
                price_score=int(price_eval["score"]),
                chain_score=int(chain_eval["score"]),
                liquidity_score=liquidity_score,
                quality_score=int(quality_eval["score"]),
                banknifty_score=int(banknifty_eval["score"]),
            )
            combined_score = int(score_breakdown["score"])
            factor_scores = {
                "technical": score,
                "score_breakdown": score_breakdown,
                "market_regime": market_eval,
                "price_action": price_eval,
                "option_chain": chain_eval,
                "option_quality": quality_eval,
                "day_type": day_type_eval,
                "option_premium_confirmation": premium_eval,
                "time_bucket_edge": time_bucket_eval,
                "strategy_edge": edge_eval,
                "banknifty_intelligence": banknifty_eval,
                "liquidity": liquidity_score,
                "contract": self._contract_payload(contract),
                "prices": prices,
                "data_freshness": freshness_eval,
                "kite_calls": self._feed_call_counts(),
            }
            banknifty_fields = self._banknifty_response_fields(banknifty_eval)
            factor_scores.update(banknifty_fields)
            outcome_learning_eval = self.outcome_learning_service.evaluate(
                symbol=symbol,
                action=self._action(side, trend),
                factor_scores=factor_scores,
            )
            factor_scores["outcome_learning"] = outcome_learning_eval
            risk_failures = self._gate_failures(
                combined_score=combined_score,
                contract=contract,
                prices=prices,
                side=side,
                market_eval=market_eval,
                price_eval=price_eval,
                chain_eval=chain_eval,
                quality_eval=quality_eval,
                day_type_eval=day_type_eval,
                premium_eval=premium_eval,
                time_bucket_eval=time_bucket_eval,
                edge_eval=edge_eval,
                banknifty_eval=banknifty_eval,
                outcome_learning_eval=outcome_learning_eval,
                enforce_budget=enforce_budget,
            )
            if not freshness_eval.get("passed", False):
                risk_failures = list(freshness_eval.get("reasons", [])) + risk_failures
            if risk_failures:
                self._log_decision(
                    symbol=symbol,
                    accepted=False,
                    score=combined_score,
                    reasons=risk_failures,
                    breakdown=score_breakdown,
                    snapshot=snapshot,
                    side=side,
                    trend=trend,
                    contract=contract,
                    factor_scores=factor_scores,
                )
                self._save_rejection(
                    symbol=symbol,
                    side=side,
                    trend=trend,
                    score=combined_score,
                    reasons=risk_failures,
                    breakdown=score_breakdown,
                    snapshot=snapshot,
                    contract=contract,
                    factor_scores=factor_scores,
                )
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
                    banknifty_fields=banknifty_fields,
                )
                self._log_decision(
                    symbol=symbol,
                    accepted=True,
                    score=combined_score,
                    reasons=[],
                    breakdown=score_breakdown,
                    snapshot=snapshot,
                    side=side,
                    trend=trend,
                    contract=contract,
                    factor_scores=factor_scores,
                    prices=prices,
                )
                diagnostics.append(self._diagnostic(symbol, snapshot, combined_score, trend, market_context, side, signal, [], factor_scores))
            else:
                score_reasons = ["final weighted score is below threshold"]
                self._log_decision(
                    symbol=symbol,
                    accepted=False,
                    score=combined_score,
                    reasons=score_reasons,
                    breakdown=score_breakdown,
                    snapshot=snapshot,
                    side=side,
                    trend=trend,
                    contract=contract,
                    factor_scores=factor_scores,
                )
                self._save_rejection(
                    symbol=symbol,
                    side=side,
                    trend=trend,
                    score=combined_score,
                    reasons=score_reasons,
                    breakdown=score_breakdown,
                    snapshot=snapshot,
                    contract=contract,
                    factor_scores=factor_scores,
                )
                diagnostics.append(
                    self._diagnostic(
                        symbol,
                        snapshot,
                        combined_score,
                        trend,
                        market_context,
                        side,
                        None,
                        score_reasons,
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

    def _focus_symbols(self, symbols: List[str]) -> List[str]:
        focused = [symbol.upper() for symbol in symbols if symbol.upper() in self.FOCUS_UNDERLYINGS]
        return focused or self.DEFAULT_UNIVERSE

    def _market_snapshots(self) -> dict[str, dict[str, object]]:
        if not settings.use_kite_market_data:
            return {}
        return {
            "NIFTY": self.feed.get_snapshot("NIFTY"),
            "BANKNIFTY": self.feed.get_snapshot("BANKNIFTY"),
            "INDIAVIX": self.feed.get_snapshot("INDIAVIX"),
            "HDFCBANK": self.feed.get_snapshot("HDFCBANK"),
            "ICICIBANK": self.feed.get_snapshot("ICICIBANK"),
            "SBIN": self.feed.get_snapshot("SBIN"),
            "AXISBANK": self.feed.get_snapshot("AXISBANK"),
            "KOTAKBANK": self.feed.get_snapshot("KOTAKBANK"),
            "INDUSINDBK": self.feed.get_snapshot("INDUSINDBK"),
            "BANKBARODA": self.feed.get_snapshot("BANKBARODA"),
            "PNB": self.feed.get_snapshot("PNB"),
            "CANBK": self.feed.get_snapshot("CANBK"),
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

    def _score_breakdown(
        self,
        technical_score: int,
        market_score: int,
        price_score: int,
        chain_score: int,
        liquidity_score: int,
        quality_score: int,
        banknifty_score: int = 50,
    ) -> dict[str, object]:
        return self.decision_engine_service.score_breakdown(
            technical_score=technical_score,
            market_score=market_score,
            price_score=price_score,
            chain_score=chain_score,
            liquidity_score=liquidity_score,
            quality_score=quality_score,
            banknifty_score=banknifty_score,
        )

    def _final_score(
        self,
        technical_score: int,
        market_score: int,
        price_score: int,
        chain_score: int,
        liquidity_score: int,
        quality_score: int,
        banknifty_score: int = 50,
    ) -> int:
        return int(
            self._score_breakdown(
                technical_score=technical_score,
                market_score=market_score,
                price_score=price_score,
                chain_score=chain_score,
                liquidity_score=liquidity_score,
                quality_score=quality_score,
                banknifty_score=banknifty_score,
            )["score"]
        )

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
        quality_eval: dict[str, object],
        day_type_eval: dict[str, object],
        premium_eval: dict[str, object],
        time_bucket_eval: dict[str, object],
        edge_eval: dict[str, object],
        banknifty_eval: dict[str, object],
        outcome_learning_eval: dict[str, object],
        enforce_budget: bool = False,
    ) -> list[str]:
        failures = self.trade_setup_service.risk_checks(combined_score, contract, prices["entry_price"], side, enforce_budget=enforce_budget)
        if prices["risk_reward"] < settings.min_risk_reward:
            failures.append("risk/reward is below threshold")
        if int(market_eval["score"]) < settings.min_market_regime_score or not market_eval["passed"]:
            failures.extend(str(reason) for reason in market_eval.get("reasons", []))
        price_details = price_eval.get("details", {}) if isinstance(price_eval.get("details"), dict) else {}
        if price_details.get("hard_block"):
            failures.extend(str(reason) for reason in price_details.get("hard_block_reasons", price_eval.get("reasons", [])))
        if not chain_eval.get("passed", False) and not chain_eval.get("details"):
            failures.extend(str(reason) for reason in chain_eval.get("reasons", ["option chain data is unavailable"]))
        if int(quality_eval["score"]) < settings.min_option_quality_score or not quality_eval["passed"]:
            failures.extend(str(reason) for reason in quality_eval.get("reasons", []))
        if settings.enable_day_type_filter and not day_type_eval.get("passed", False):
            failures.extend(str(reason) for reason in day_type_eval.get("reasons", ["day type filter failed"]))
        if settings.enable_option_premium_confirmation and not premium_eval.get("passed", False):
            failures.extend(str(reason) for reason in premium_eval.get("reasons", ["option premium confirmation failed"]))
        if settings.enable_time_bucket_filter and not time_bucket_eval.get("passed", False):
            failures.extend(str(reason) for reason in time_bucket_eval.get("reasons", ["time bucket edge failed"]))
        if settings.enable_strategy_edge_guard and not edge_eval.get("passed", False):
            failures.extend(str(reason) for reason in edge_eval.get("reasons", ["strategy edge guard failed"]))
        if settings.enable_banknifty_intelligence and not banknifty_eval.get("passed", False):
            failures.extend(str(reason) for reason in banknifty_eval.get("hard_reasons", ["Bank Nifty intelligence no-trade filter failed"]))
        if settings.enable_outcome_learning_guard and not outcome_learning_eval.get("passed", False):
            failures.extend(str(reason) for reason in outcome_learning_eval.get("reasons", ["outcome learning guard failed"]))
        return list(dict.fromkeys(failures))

    def _log_decision(
        self,
        *,
        symbol: str,
        accepted: bool,
        score: int,
        reasons: list[str],
        breakdown: dict[str, object],
        snapshot: dict[str, object] | None = None,
        side: str = "BUY",
        trend: str = "",
        contract: OptionContract | None = None,
        factor_scores: dict[str, object] | None = None,
        prices: dict[str, float] | None = None,
    ) -> None:
        action = self._action(side, trend) if trend else ""
        quality = factor_scores.get("option_quality", {}) if factor_scores else {}
        premium = factor_scores.get("option_premium_confirmation", {}) if factor_scores else {}
        liquidity = factor_scores.get("liquidity") if factor_scores else None
        payload = {
            "symbol": symbol,
            "timestamp": self._decision_timestamp(),
            "decision": "accepted" if accepted else "rejected",
            "direction": action,
            "strike": contract.strike if contract else None,
            "expiry": contract.expiry if contract else None,
            "tradingsymbol": contract.tradingsymbol if contract else None,
            "score": score,
            "threshold": settings.min_signal_score,
            "reasons": reasons,
            "market_state": {
                "price": (snapshot or {}).get("price"),
                "source": (snapshot or {}).get("source"),
                "is_real_data": (snapshot or {}).get("is_real_data"),
                "rsi": (snapshot or {}).get("rsi"),
                "adx": (snapshot or {}).get("adx"),
                "vwap": (snapshot or {}).get("vwap"),
                "ema_9": (snapshot or {}).get("ema_9"),
                "ema_21": (snapshot or {}).get("ema_21"),
            },
            "option_state": {
                "delta": self._nested_value(quality, "details", "greeks", "delta"),
                "iv": self._nested_value(quality, "details", "greeks", "implied_volatility"),
                "theta": self._nested_value(quality, "details", "greeks", "theta"),
                "spread_pct": self._nested_value(quality, "details", "spread_pct"),
                "volume": contract.volume if contract else None,
                "open_interest": contract.open_interest if contract else None,
                "liquidity_score": liquidity,
                "premium_confirmation": premium,
            },
            "planned_exit": {
                "entry": (prices or {}).get("entry_price"),
                "stop_loss": (prices or {}).get("stop_loss"),
                "target_1": (prices or {}).get("target_1"),
                "target_2": (prices or {}).get("target_2"),
                "target_3": (prices or {}).get("target_3"),
                "time_stop_minutes": settings.option_time_stop_minutes,
                "trailing_stop_lock_pct": settings.option_trailing_stop_lock_pct,
                "near_close_exit_minutes": settings.exit_open_trades_before_close_minutes,
            },
            "score_breakdown": breakdown,
        }
        logger.info("trade_decision %s", payload)

    def _save_rejection(
        self,
        *,
        symbol: str,
        side: str,
        trend: str,
        score: int,
        reasons: list[str],
        breakdown: dict[str, object],
        snapshot: dict[str, object] | None = None,
        contract: OptionContract | None = None,
        factor_scores: dict[str, object] | None = None,
    ) -> None:
        try:
            action = self._action(side, trend) if str(trend).lower() in {"bullish", "bearish"} else None
            self.rejected_opportunity_repository.save_rejection(
                symbol=symbol,
                side=side,
                action=action,
                score=score,
                reasons=reasons,
                snapshot=snapshot,
                contract=contract,
                factor_scores=factor_scores,
                score_breakdown=breakdown,
            )
        except Exception as exc:
            logger.warning("failed_to_save_rejected_opportunity symbol=%s error=%s", symbol, exc)

    def _feed_call_counts(self) -> dict[str, object]:
        if hasattr(self.feed, "call_counts"):
            try:
                return self.feed.call_counts()  # type: ignore[no-any-return]
            except Exception:
                return {}
        return {}

    def _decision_timestamp(self) -> str:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()

    def _nested_value(self, value: object, *keys: str) -> object:
        current = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _empty_score_breakdown(self) -> dict[str, object]:
        return {
            "score": 0,
            "threshold": settings.min_signal_score,
            "weights": {},
            "components": {},
            "contributions": {},
            "caps": {"trend_momentum_capped_at": settings.max_trend_momentum_score},
        }

    def _strategy_edge_eval(self, symbol: str, trend: str) -> dict[str, object]:
        if not settings.enable_strategy_edge_guard:
            return {"enabled": False, "passed": True, "reasons": []}
        direction = "CALL" if trend.lower() == "bullish" else "PUT"
        try:
            result = self.strategy_edge_service.evaluate(symbol=symbol, direction=direction)
            return {"enabled": True, **result}
        except Exception as exc:
            return {
                "enabled": True,
                "passed": False,
                "reasons": [f"strategy edge validation unavailable: {exc}"],
            }

    def _setup_type(self, side: str, trend: str) -> str:
        if side.upper() == "SELL":
            return "credit_put_sell" if trend.lower() == "bullish" else "credit_call_sell"
        return "directional_call_buy" if trend.lower() == "bullish" else "directional_put_buy"

    def _action(self, side: str, trend: str) -> str:
        if side.upper() == "SELL":
            return "SELL_PE" if trend.lower() == "bullish" else "SELL_CE"
        return "BUY_CE" if trend.lower() == "bullish" else "BUY_PE"

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

    def _banknifty_response_fields(self, banknifty_eval: dict[str, object]) -> dict[str, object]:
        details = banknifty_eval.get("details", {}) if isinstance(banknifty_eval.get("details"), dict) else {}
        return {
            "bankNiftySpecificScore": details.get("bankNiftySpecificScore", banknifty_eval.get("score")),
            "topBankAlignment": details.get("topBankAlignment"),
            "privateBankStrength": details.get("privateBankStrength"),
            "psuBankStrength": details.get("psuBankStrength"),
            "relativeStrengthVsNifty": details.get("relativeStrengthVsNifty"),
            "openingRangeStatus": details.get("openingRangeStatus"),
            "optionPremiumConfirmation": details.get("optionPremiumConfirmation"),
            "expectedMoveCheck": details.get("expectedMoveCheck"),
            "dteMode": details.get("dteMode"),
            "eventDayMode": details.get("eventDayMode"),
            "nearestMajorZone": details.get("nearestMajorZone"),
            "optionChainNearAtmSignal": details.get("optionChainNearAtmSignal"),
            "dayType": details.get("dayType"),
            "noTradeReasons": details.get("noTradeReasons", []),
            "tradeQuality": details.get("tradeQuality", "NO_TRADE" if banknifty_eval.get("passed") is False else "B"),
            "confidenceReason": details.get("confidenceReason", ""),
            "invalidationReason": details.get("invalidationReason", ""),
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
