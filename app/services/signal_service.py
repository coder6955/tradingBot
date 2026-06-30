from __future__ import annotations

from app.models import Signal
from app.config import settings


class SignalService:
    """Generate an option trade recommendation from scoring and market context."""

    def generate_signal(
        self,
        symbol: str,
        score: int,
        confidence: float,
        trend: str,
        market_context: str,
        strike: float | None = None,
        expiry: str | None = None,
        entry_price: float | None = None,
        stop_loss: float | None = None,
        target_1: float | None = None,
        target_2: float | None = None,
        target_3: float | None = None,
        side: str = "BUY",
        tradingsymbol: str | None = None,
        exchange: str = "NFO",
        instrument_token: int | None = None,
        quantity: int = 0,
        lot_size: int = 0,
        probability: float | None = None,
        risk_reward: float = 0.0,
        setup_type: str = "",
        technical_score: int = 0,
        market_regime_score: int = 0,
        price_action_score: int = 0,
        option_chain_score: int = 0,
        liquidity_score: int = 0,
        factor_scores: dict[str, object] | None = None,
        risk_notes: list[str] | None = None,
    ) -> Signal:
        if score < settings.min_signal_score:
            raise ValueError(f"score must be at least {settings.min_signal_score} to qualify")

        bullish = trend.lower() == "bullish"
        if side.upper() == "SELL":
            option_type = "PE" if bullish else "CE"
        else:
            option_type = "CE" if bullish else "PE"
        action = f"{side.upper()}_{option_type}"
        strike = strike if strike is not None else (20000.0 if symbol.upper() == "NIFTY" else 50000.0)
        expiry = expiry or "weekly"
        entry_price = entry_price if entry_price is not None else (150.0 if symbol.upper() == "NIFTY" else 200.0)
        stop_loss = stop_loss if stop_loss is not None else entry_price * 0.9
        target_1 = target_1 if target_1 is not None else entry_price * 1.12
        target_2 = target_2 if target_2 is not None else entry_price * 1.2
        target_3 = target_3 if target_3 is not None else entry_price * 1.3
        probability = probability if probability is not None else confidence

        explanation = (
            f"{symbol} shows a {trend} setup with {market_context} market context. "
            f"The score of {score}/100 and probability estimate of {probability:.0%} exceed the configured threshold. "
            "Risk controls must still be followed; this is not a guaranteed-profit trade."
        )

        return Signal(
            symbol=symbol,
            action=action,
            side=side.upper(),
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            instrument_token=instrument_token,
            strike=strike,
            expiry=expiry,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            target_3=target_3,
            quantity=quantity,
            lot_size=lot_size,
            probability=probability,
            risk_reward=risk_reward,
            setup_type=setup_type,
            technical_score=technical_score,
            market_regime_score=market_regime_score,
            price_action_score=price_action_score,
            option_chain_score=option_chain_score,
            liquidity_score=liquidity_score,
            factor_scores=factor_scores or {},
            risk_notes=risk_notes or [],
            confidence=confidence,
            score=score,
            explanation=explanation,
        )
