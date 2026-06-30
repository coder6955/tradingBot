from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Signal:
    symbol: str
    action: str
    side: str = "BUY"
    tradingsymbol: Optional[str] = None
    exchange: str = "NFO"
    instrument_token: Optional[int] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    target_3: Optional[float] = None
    quantity: int = 0
    lot_size: int = 0
    probability: float = 0.0
    risk_reward: float = 0.0
    setup_type: str = ""
    technical_score: int = 0
    market_regime_score: int = 0
    price_action_score: int = 0
    option_chain_score: int = 0
    liquidity_score: int = 0
    factor_scores: dict[str, object] = field(default_factory=dict)
    risk_notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    score: int = 0
    explanation: str = ""
