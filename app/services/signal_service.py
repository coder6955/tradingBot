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
        banknifty_fields: dict[str, object] | None = None,
        enforce_score_threshold: bool = True,
    ) -> Signal:
        if enforce_score_threshold and score < settings.min_signal_score:
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
        probability_text = f"calibrated probability estimate of {probability:.0%}" if probability is not None else "an uncalibrated heuristic confidence score"
        if enforce_score_threshold:
            qualification_text = f"The score of {score}/100 and {probability_text} exceed the configured threshold."
        else:
            qualification_text = (
                f"Primary safety, structure and execution gates passed; the {score}/100 score is used for ranking only, "
                f"with {probability_text}."
            )
        explanation = (
            f"{symbol} shows a {trend} setup with {market_context} market context. "
            f"{qualification_text} Risk controls must still be followed; this is not a guaranteed-profit trade."
        )

        banknifty_fields = banknifty_fields or {}
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
            bankNiftySpecificScore=int(banknifty_fields.get("bankNiftySpecificScore") or 0),
            topBankAlignment=banknifty_fields.get("topBankAlignment") if isinstance(banknifty_fields.get("topBankAlignment"), dict) else None,
            privateBankStrength=float(banknifty_fields.get("privateBankStrength") or 0.0),
            psuBankStrength=float(banknifty_fields.get("psuBankStrength") or 0.0),
            relativeStrengthVsNifty=banknifty_fields.get("relativeStrengthVsNifty") if isinstance(banknifty_fields.get("relativeStrengthVsNifty"), dict) else None,
            openingRangeStatus=banknifty_fields.get("openingRangeStatus") if isinstance(banknifty_fields.get("openingRangeStatus"), dict) else None,
            optionPremiumConfirmation=banknifty_fields.get("optionPremiumConfirmation") if isinstance(banknifty_fields.get("optionPremiumConfirmation"), dict) else None,
            expectedMoveCheck=banknifty_fields.get("expectedMoveCheck") if isinstance(banknifty_fields.get("expectedMoveCheck"), dict) else None,
            dteMode=banknifty_fields.get("dteMode") if isinstance(banknifty_fields.get("dteMode"), dict) else None,
            eventDayMode=banknifty_fields.get("eventDayMode") if isinstance(banknifty_fields.get("eventDayMode"), dict) else None,
            nearestMajorZone=banknifty_fields.get("nearestMajorZone") if isinstance(banknifty_fields.get("nearestMajorZone"), dict) else None,
            optionChainNearAtmSignal=banknifty_fields.get("optionChainNearAtmSignal") if isinstance(banknifty_fields.get("optionChainNearAtmSignal"), dict) else None,
            dayType=str(banknifty_fields.get("dayType") or ""),
            noTradeReasons=list(banknifty_fields.get("noTradeReasons") or []),
            tradeQuality=str(banknifty_fields.get("tradeQuality") or ""),
            confidenceReason=str(banknifty_fields.get("confidenceReason") or ""),
            invalidationReason=str(banknifty_fields.get("invalidationReason") or ""),
            confidence=confidence,
            score=score,
            explanation=explanation,
        )
