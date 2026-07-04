from __future__ import annotations

import logging
from typing import List

from app.models import Signal
from app.config import settings
from app.services.indicator_scoring_service import IndicatorScoringService
from app.services.armed_entry_tracker_service import ArmedEntryTrackerService
from app.services.banknifty_intelligence_service import BankNiftyIntelligenceService
from app.services.banknifty_option_prewarm_service import BankNiftyOptionPrewarmService
from app.services.day_type_service import DayTypeService
from app.services.data_freshness_service import DataFreshnessService
from app.services.decision_engine_service import DecisionEngineService
from app.services.entry_timing_service import EntryTimingService
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
from app.services.volatility_edge_service import VolatilityEdgeService


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
        banknifty_option_prewarm_service: BankNiftyOptionPrewarmService | None = None,
        entry_timing_service: EntryTimingService | None = None,
        volatility_edge_service: VolatilityEdgeService | None = None,
        armed_entry_tracker: ArmedEntryTrackerService | None = None,
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
        self.banknifty_option_prewarm_service = banknifty_option_prewarm_service
        self.entry_timing_service = entry_timing_service or EntryTimingService()
        self.volatility_edge_service = volatility_edge_service or VolatilityEdgeService()
        self.armed_entry_tracker = armed_entry_tracker

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
        rejection_source: str = "scanner",
    ) -> List[Signal]:
        return [
            item["signal"]
            for item in self.scan_with_diagnostics(
                symbols,
                scores,
                confidences,
                trends,
                market_contexts,
                side,
                order_mode=order_mode,
                rejection_source=rejection_source,
            )
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
        rejection_source: str = "scanner",
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
                self._save_rejection(
                    symbol=symbol,
                    side=side,
                    trend="unknown",
                    score=0,
                    reasons=reasons,
                    breakdown=self._empty_score_breakdown(),
                    snapshot=snapshot,
                    rejection_source=rejection_source,
                )
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
            prewarm_eval = self._prewarm_banknifty_options(symbol, float(snapshot["price"]), option_instruments)
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
                self._save_rejection(
                    symbol=symbol,
                    side=side,
                    trend=trend,
                    score=score,
                    reasons=reasons,
                    breakdown=self._empty_score_breakdown(),
                    snapshot=snapshot,
                    rejection_source=rejection_source,
                )
                diagnostics.append(self._diagnostic(symbol, snapshot, score, trend, market_context, side, None, reasons))
                continue

            entry_price = self._entry_price_from_contract(contract, side)
            freshness_eval = self.data_freshness_service.validate_scan_inputs(
                order_mode=order_mode,
                snapshot=snapshot,
                chain_quotes=chain_quote_map,
                contract=contract,
            )
            premium_eval = self.option_premium_confirmation_service.evaluate(contract=contract, side=side)
            quote_quality = self._selected_option_data_quality(
                contract=contract,
                side=side,
                quote_map=chain_quote_map,
                premium_eval=premium_eval,
            )
            if not quote_quality["passed"]:
                risk_failures = list(quote_quality["reasons"])
                if not freshness_eval.get("passed", False):
                    risk_failures = list(freshness_eval.get("reasons", [])) + risk_failures
                volatility_eval = self.volatility_edge_service.evaluate(
                    symbol=symbol,
                    contract=contract,
                    premium_eval=premium_eval,
                    market_snapshots=market_snapshots,
                    prices={},
                    action=self._action(side, trend),
                    side=side,
                )
                factor_scores = {
                    "technical": score,
                    "score_breakdown": self._empty_score_breakdown(),
                    "contract": self._contract_payload(contract),
                    "prices": {},
                    "data_freshness": freshness_eval,
                    "data_quality": quote_quality,
                    "option_premium_confirmation": premium_eval,
                    "volatility_edge": volatility_eval,
                    "banknifty_option_prewarm": prewarm_eval,
                    "kite_calls": self._feed_call_counts(),
                }
                factor_scores = self._with_strategy_metadata(factor_scores, order_mode)
                self._log_decision(
                    symbol=symbol,
                    accepted=False,
                    score=score,
                    reasons=risk_failures,
                    breakdown=self._empty_score_breakdown(),
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
                    score=score,
                    reasons=risk_failures,
                    breakdown=self._empty_score_breakdown(),
                    snapshot=snapshot,
                    contract=contract,
                    factor_scores=factor_scores,
                    rejection_source=rejection_source,
                )
                diagnostics.append(self._diagnostic(symbol, snapshot, score, trend, market_context, side, None, risk_failures, factor_scores))
                continue
            prices = self.trade_setup_service.build_prices(
                entry_price=entry_price,
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
            banknifty_details = banknifty_eval.get("details", {}) if isinstance(banknifty_eval.get("details"), dict) else {}
            volatility_eval = self.volatility_edge_service.evaluate(
                symbol=symbol,
                contract=contract,
                option_quality=quality_eval,
                premium_eval=premium_eval,
                market_snapshots=market_snapshots,
                prices=prices,
                action=self._action(side, trend),
                side=side,
                expected_move_check=banknifty_details.get("expectedMoveCheck") if isinstance(banknifty_details, dict) else None,
            )
            entry_timing_eval = self.entry_timing_service.evaluate(
                contract=contract,
                prices=prices,
                premium_eval=premium_eval,
                data_quality=quote_quality,
                freshness=freshness_eval,
                option_quality=quality_eval,
                banknifty_eval=banknifty_eval,
                price_action=price_eval,
                liquidity_score=liquidity_score,
                trend=trend,
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
                "volatility_edge": volatility_eval,
                "liquidity": liquidity_score,
                "contract": self._contract_payload(contract),
                "prices": prices,
                "data_freshness": freshness_eval,
                "data_quality": quote_quality,
                "entry_timing": entry_timing_eval,
                "banknifty_option_prewarm": prewarm_eval,
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
            factor_scores = self._with_strategy_metadata(factor_scores, order_mode)
            confidence = confidences.get(symbol, combined_score / 100.0)
            probability = min(0.92, (combined_score / 100.0) * 0.72 + (liquidity_score / 100.0) * 0.12 + (int(chain_eval["score"]) / 100.0) * 0.08)
            quantity = self.trade_setup_service.position_size(
                entry_price=prices["entry_price"],
                stop_loss=prices["stop_loss"],
                lot_size=contract.lot_size,
                side=side,
            )
            gate_failures = self._gate_failures(
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
                volatility_eval=volatility_eval,
                enforce_budget=enforce_budget,
            )
            if not freshness_eval.get("passed", False):
                gate_failures = list(freshness_eval.get("reasons", [])) + gate_failures
            armed_entry_eval = self._maybe_register_armed_entry(
                symbol=symbol,
                side=side,
                trend=trend,
                contract=contract,
                prices=prices,
                entry_timing_eval=entry_timing_eval,
                score=combined_score,
                probability=probability,
                confidence=confidence,
                quantity=quantity,
                factor_scores=factor_scores,
                order_mode=order_mode,
                gate_failures=gate_failures,
            )
            if armed_entry_eval:
                factor_scores = dict(factor_scores)
                factor_scores["armed_entry"] = armed_entry_eval
            risk_failures = list(gate_failures)
            risk_failures.extend(self._entry_timing_failures(entry_timing_eval))
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
                    rejection_source=rejection_source,
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
                    rejection_source=rejection_source,
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

    def _prewarm_banknifty_options(self, symbol: str, spot_price: float, option_instruments: List[dict[str, object]]) -> dict[str, object]:
        if symbol.upper() != "BANKNIFTY" or self.banknifty_option_prewarm_service is None:
            return {"prewarm_enabled": False, "prewarm_reason": "prewarm_service_unavailable"}
        try:
            return self.banknifty_option_prewarm_service.prewarm(
                spot_price=spot_price,
                option_instruments=[dict(item) for item in option_instruments],
            )
        except Exception as exc:
            return {"prewarm_enabled": settings.enable_banknifty_option_prewarm, "prewarm_reason": "prewarm_error", "message": str(exc)}

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

    def _entry_price_from_contract(self, contract: OptionContract, side: str) -> float:
        if side.upper() == "BUY":
            return float(contract.ask or contract.last_price or 0.0)
        return float(contract.bid or contract.last_price or 0.0)

    def _selected_option_data_quality(
        self,
        *,
        contract: OptionContract,
        side: str,
        quote_map: dict[str, object],
        premium_eval: dict[str, object],
    ) -> dict[str, object]:
        quote_key = f"{contract.exchange}:{contract.tradingsymbol}"
        quote_payload = quote_map.get(quote_key) or quote_map.get(contract.tradingsymbol) or {}
        quote_payload = quote_payload if isinstance(quote_payload, dict) else {}
        quote_timestamp = quote_payload.get("quote_timestamp") or quote_payload.get("timestamp")
        quote_source = quote_payload.get("source") or ("kite_quote" if quote_payload else "missing")
        quote_token = self._safe_int(quote_payload.get("instrument_token"))
        token_validation_status = "quote_token_missing"
        if quote_token is not None and contract.instrument_token is not None:
            token_validation_status = "matched" if quote_token == contract.instrument_token else "mismatch"
        elif contract.instrument_token is not None:
            token_validation_status = "instrument_token_from_master_only"

        details = premium_eval.get("details", {}) if isinstance(premium_eval.get("details"), dict) else {}
        candle_price = self._premium_reference_price(details)
        candle_timestamp = details.get("last_timestamp") or details.get("timestamp")
        candle_source = details.get("source") or ("candles" if details.get("last_close") is not None else "snapshots" if details.get("last_price") is not None else None)
        live_price = float(contract.last_price or 0.0)
        entry_price = self._entry_price_from_contract(contract, side)
        mismatch_pct = None
        reasons: list[str] = []

        if contract.instrument_token is None:
            reasons.append("selected_option_quote_invalid")
        if live_price <= 0 or entry_price <= 0:
            reasons.append("selected_option_quote_invalid")
        if contract.bid <= 0 or contract.ask <= 0 or contract.ask < contract.bid:
            reasons.append("selected_option_quote_invalid")
        if contract.volume <= 0 or contract.open_interest <= 0:
            reasons.append("selected_option_quote_invalid")
        if token_validation_status == "mismatch":
            reasons.append("selected_option_quote_invalid")
        if live_price > 0 and candle_price and candle_price > 0:
            mismatch_pct = abs(candle_price - live_price) / live_price * 100
            if mismatch_pct > settings.option_quote_premium_mismatch_tolerance_pct:
                reasons.append("option_quote_premium_mismatch")

        passed = not reasons
        return {
            "passed": passed,
            "data_quality": "valid" if passed else "invalid",
            "reasons": list(dict.fromkeys(reasons)),
            "selected_option": {
                "tradingsymbol": contract.tradingsymbol,
                "exchange": contract.exchange,
                "instrument_token": contract.instrument_token,
                "quote_key_used": quote_key,
                "quote_present": bool(quote_payload),
                "quote_source": quote_source,
                "quote_timestamp": quote_timestamp,
                "quote_instrument_token": quote_token,
                "token_validation_status": token_validation_status,
                "last_price": live_price,
                "bid": contract.bid,
                "ask": contract.ask,
                "volume": contract.volume,
                "open_interest": contract.open_interest,
            },
            "premium_reference": {
                "source": candle_source,
                "timestamp": candle_timestamp,
                "price": candle_price,
            },
            "mismatch": {
                "mismatch_pct": round(mismatch_pct, 2) if mismatch_pct is not None else None,
                "tolerance_pct": settings.option_quote_premium_mismatch_tolerance_pct,
                "reason": "option_quote_premium_mismatch" if "option_quote_premium_mismatch" in reasons else None,
            },
        }

    def _premium_reference_price(self, details: dict[str, object]) -> float | None:
        for key in ("last_close", "last_price"):
            value = details.get(key)
            try:
                if value is not None:
                    price = float(value)
                    if price > 0:
                        return price
            except (TypeError, ValueError):
                continue
        return None

    def _safe_int(self, value: object) -> int | None:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            return None
        return None

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
        volatility_eval: dict[str, object] | None = None,
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
        if settings.enable_volatility_edge and settings.enable_volatility_edge_hard_gate and volatility_eval and not volatility_eval.get("passed", False):
            failures.extend(str(reason) for reason in volatility_eval.get("reasons", ["volatility edge guard failed"]))
        if settings.enable_banknifty_intelligence and not banknifty_eval.get("passed", False):
            failures.extend(str(reason) for reason in banknifty_eval.get("hard_reasons", ["Bank Nifty intelligence no-trade filter failed"]))
        if settings.enable_outcome_learning_guard and not outcome_learning_eval.get("passed", False):
            failures.extend(str(reason) for reason in outcome_learning_eval.get("reasons", ["outcome learning guard failed"]))
        return list(dict.fromkeys(failures))

    def _entry_timing_failures(self, entry_timing_eval: dict[str, object]) -> list[str]:
        state = str(entry_timing_eval.get("state") or entry_timing_eval.get("entry_timing_state") or "")
        reasons = [str(reason) for reason in entry_timing_eval.get("reasons", []) if str(reason)]
        if state == EntryTimingService.ENTER_NOW:
            return []
        if state == EntryTimingService.TOO_LATE:
            return reasons or ["entry_too_late"]
        if state == EntryTimingService.ARMED_FOR_ENTRY:
            return ["waiting_for_entry_trigger", "premium_trigger_not_broken_yet"]
        if state == EntryTimingService.WATCHING_SETUP:
            return ["waiting_for_entry_trigger"]
        if state == EntryTimingService.NO_TRADE:
            return reasons or ["entry_timing_no_trade"]
        return []

    def _maybe_register_armed_entry(
        self,
        *,
        symbol: str,
        side: str,
        trend: str,
        contract: OptionContract,
        prices: dict[str, float],
        entry_timing_eval: dict[str, object],
        score: int,
        probability: float,
        confidence: float,
        quantity: int,
        factor_scores: dict[str, object],
        order_mode: str,
        gate_failures: list[str],
    ) -> dict[str, object] | None:
        state = str(entry_timing_eval.get("entry_timing_state") or entry_timing_eval.get("state") or "")
        if state != EntryTimingService.ARMED_FOR_ENTRY:
            return None
        if self.armed_entry_tracker is None:
            return {"registered": False, "reason": "armed_entry_tracker_unavailable"}
        if gate_failures:
            return {"registered": False, "reason": "hard_gate_failed_before_arming", "gate_failures": list(gate_failures)}
        if score < settings.min_signal_score:
            return {"registered": False, "reason": "final weighted score is below threshold"}
        if not contract.instrument_token:
            return {"registered": False, "reason": "selected_option_token_missing"}
        if not settings.enable_kite_websocket:
            return {"registered": False, "reason": "websocket_disabled_for_event_entry"}
        return self.armed_entry_tracker.register_from_scan(
            symbol=symbol,
            action=self._action(side, trend),
            side=side,
            contract=contract,
            prices=prices,
            entry_timing=entry_timing_eval,
            score=score,
            probability=probability,
            confidence=confidence,
            quantity=quantity,
            factor_scores=dict(factor_scores),
            order_mode=order_mode,
            reasons=[str(reason) for reason in entry_timing_eval.get("reasons", [])],
        )

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
        volatility = factor_scores.get("volatility_edge", {}) if factor_scores else {}
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
                "volatility_edge": volatility,
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
        rejection_source: str = "scanner",
    ) -> None:
        try:
            action = self._action(side, trend) if str(trend).lower() in {"bullish", "bearish"} else None
            factor_scores = self._with_strategy_metadata(factor_scores or {}, "unknown")
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
                rejection_source=rejection_source,
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

    def _with_strategy_metadata(self, factor_scores: dict[str, object], order_mode: str) -> dict[str, object]:
        if "strategy_metadata" in factor_scores:
            return factor_scores
        enriched = dict(factor_scores)
        enriched["strategy_metadata"] = {
            "strategy_name": settings.strategy_name,
            "strategy_version": settings.strategy_version,
            "order_mode": order_mode,
            "generated_at": self._decision_timestamp(),
            "hard_gate_thresholds": {
                "min_signal_score": settings.min_signal_score,
                "min_option_quality_score": settings.min_option_quality_score,
                "min_risk_reward": settings.min_risk_reward,
                "max_bid_ask_spread_pct": settings.max_bid_ask_spread_pct,
                "min_option_volume": settings.min_option_volume,
                "min_option_oi": settings.min_option_oi,
                "min_volatility_edge_score": settings.min_volatility_edge_score,
                "max_live_quote_age_seconds": settings.max_live_quote_age_seconds,
                "max_live_option_quote_age_seconds": settings.max_live_option_quote_age_seconds,
                "max_premium_confirmation_candle_age_seconds": settings.max_premium_confirmation_candle_age_seconds,
            },
            "weighted_score_policy": {
                "trend_momentum_cap": settings.max_trend_momentum_score,
                "min_market_regime_score": settings.min_market_regime_score,
                "min_price_action_score": settings.min_price_action_score,
                "min_option_chain_score": settings.min_option_chain_score,
                "min_option_premium_confirmation_score": settings.min_option_premium_confirmation_score,
            },
            "enabled_guards": {
                "day_type_filter": settings.enable_day_type_filter,
                "option_premium_confirmation": settings.enable_option_premium_confirmation,
                "banknifty_intelligence": settings.enable_banknifty_intelligence,
                "time_bucket_filter": settings.enable_time_bucket_filter,
                "strategy_edge_guard": settings.enable_strategy_edge_guard,
                "volatility_edge": settings.enable_volatility_edge,
                "volatility_edge_hard_gate": settings.enable_volatility_edge_hard_gate,
                "outcome_learning_guard": settings.enable_outcome_learning_guard,
                "event_driven_paper_entry": settings.enable_event_driven_paper_entry,
                "event_driven_live_entry": settings.enable_event_driven_live_entry,
            },
            "event_entry_policy": {
                "armed_entry_valid_seconds": settings.armed_entry_valid_seconds,
                "max_entry_chase_pct": settings.max_entry_chase_pct,
                "max_premium_move_from_base_pct": settings.max_premium_move_from_base_pct,
                "min_remaining_risk_reward": settings.min_remaining_risk_reward,
                "min_target1_room_pct": settings.min_target1_room_pct,
            },
            "volatility_edge_policy": {
                "iv_lookback_days": settings.vol_edge_iv_lookback_days,
                "min_iv_samples": settings.vol_edge_min_iv_samples,
                "max_iv_to_rv_ratio_for_buy": settings.vol_edge_max_iv_to_rv_ratio_for_buy,
                "min_expected_move_coverage": settings.vol_edge_min_expected_move_coverage,
                "iv_crush_warning_threshold": settings.vol_edge_iv_crush_warning_threshold,
            },
            "data_policy": {
                "use_kite_market_data": settings.use_kite_market_data,
                "enable_kite_websocket": settings.enable_kite_websocket,
                "enable_websocket_premium_candle_builder": settings.enable_websocket_premium_candle_builder,
                "enable_banknifty_option_prewarm": settings.enable_banknifty_option_prewarm,
            },
        }
        return enriched

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
        timing = factor_scores.get("entry_timing", {}) if factor_scores else {}
        timing = timing if isinstance(timing, dict) else {}
        volatility = factor_scores.get("volatility_edge", {}) if factor_scores else {}
        volatility = volatility if isinstance(volatility, dict) else {}
        volatility_details = volatility.get("details", {}) if isinstance(volatility.get("details"), dict) else {}
        armed = factor_scores.get("armed_entry", {}) if factor_scores else {}
        armed = armed if isinstance(armed, dict) else {}
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
            "setup_state": timing.get("entry_timing_state"),
            "entry_timing_state": timing.get("entry_timing_state"),
            "entry_trigger_price": timing.get("entry_trigger_price"),
            "current_premium": timing.get("current_premium"),
            "premium_distance_to_trigger_pct": timing.get("premium_distance_to_trigger_pct"),
            "premium_move_from_base_pct": timing.get("premium_move_from_base_pct"),
            "chase_risk": timing.get("chase_risk"),
            "remaining_risk_reward": timing.get("remaining_risk_reward"),
            "target1_room_pct": timing.get("target1_room_pct"),
            "entry_timing_reason": timing.get("entry_timing_reason"),
            "entry_valid_until": timing.get("entry_valid_until"),
            "entry_should_wait": timing.get("entry_should_wait"),
            "entry_should_reject_as_late": timing.get("entry_should_reject_as_late"),
            "armed_setup_id": armed.get("setup_id"),
            "selected_option": armed.get("selected_option"),
            "valid_until": armed.get("valid_until") or timing.get("entry_valid_until"),
            "websocket_tracking_enabled": armed.get("websocket_tracking_enabled", False),
            "paper_event_entry_enabled": armed.get("paper_event_entry_enabled", settings.enable_event_driven_paper_entry),
            "live_event_entry_blocked": armed.get("live_event_entry_blocked", False),
            "reason": armed.get("latest_reason") or armed.get("reason") or timing.get("entry_timing_reason"),
            "volatility_edge_score": volatility.get("score"),
            "volatility_edge_classification": volatility.get("classification"),
            "volatility_edge_for_option_buying": volatility.get("volatility_edge_for_option_buying"),
            "iv_rank": volatility_details.get("iv_rank"),
            "iv_percentile": volatility_details.get("iv_percentile"),
            "iv_to_rv_ratio": volatility_details.get("iv_to_rv_ratio"),
            "expected_move_coverage_iv": volatility_details.get("expected_move_coverage_iv"),
            "iv_expansion_supported": volatility_details.get("iv_expansion_supported"),
            "iv_crush_risk": volatility_details.get("iv_crush_risk"),
            "volatility_edge_reasons": volatility.get("reasons", []),
            "factor_scores": factor_scores or {},
            "signal": signal,
        }
