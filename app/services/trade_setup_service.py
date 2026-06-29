from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import floor
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings


@dataclass(frozen=True)
class OptionContract:
    tradingsymbol: str
    exchange: str
    instrument_token: int | None
    name: str
    expiry: str
    strike: float
    option_type: str
    lot_size: int
    last_price: float = 0.0
    open_interest: float = 0.0
    volume: float = 0.0
    bid: float = 0.0
    ask: float = 0.0


class TradeSetupService:
    """Select liquid option contracts and convert ranked setups into executable plans."""

    def nearest_expiry(self, instruments: Iterable[Dict[str, Any]], underlying: str) -> Optional[str]:
        expiries: List[date] = []
        for item in instruments:
            if self._underlying_matches(item, underlying) and item.get("instrument_type") in {"CE", "PE"}:
                expiry = self._parse_expiry(item.get("expiry"))
                if expiry:
                    expiries.append(expiry)
        if not expiries:
            return None
        today = date.today()
        valid_expiries = [expiry for expiry in expiries if expiry >= today]
        return min(valid_expiries or expiries).isoformat()

    def select_contract(
        self,
        instruments: Iterable[Dict[str, Any]],
        underlying: str,
        spot_price: float,
        trend: str,
        side: str = "BUY",
        quotes: Dict[str, Any] | None = None,
    ) -> OptionContract | None:
        option_type = self.option_type_for(trend, side)
        expiry = self.nearest_expiry(instruments, underlying)
        if expiry is None:
            return None

        candidates: List[OptionContract] = []
        for item in instruments:
            if not self._underlying_matches(item, underlying):
                continue
            if str(item.get("instrument_type")) != option_type:
                continue
            item_expiry = self._parse_expiry(item.get("expiry"))
            if item_expiry is None or item_expiry.isoformat() != expiry:
                continue
            contract = self._contract_from_instrument(item, quotes or {})
            if contract:
                candidates.append(contract)

        if not candidates:
            return None

        return min(candidates, key=lambda contract: abs(contract.strike - spot_price))

    def liquidity_score(self, contract: OptionContract) -> int:
        score = 0
        if contract.last_price > 0:
            score += 20
        if contract.volume >= 1000:
            score += 25
        elif contract.volume > 0:
            score += 10
        if contract.open_interest >= 10000:
            score += 25
        elif contract.open_interest > 0:
            score += 10
        spread = contract.ask - contract.bid if contract.ask and contract.bid else 0.0
        if spread > 0 and contract.last_price > 0:
            spread_pct = spread / contract.last_price
            if spread_pct <= 0.02:
                score += 30
            elif spread_pct <= 0.05:
                score += 15
        elif contract.last_price > 0:
            score += 15
        return min(score, 100)

    def build_prices(self, entry_price: float, side: str) -> Dict[str, float]:
        if side.upper() == "SELL":
            stop_loss = entry_price * 1.35
            target_1 = entry_price * 0.75
            target_2 = entry_price * 0.55
            reward = entry_price - target_1
            risk = stop_loss - entry_price
        else:
            stop_loss = entry_price * 0.78
            target_1 = entry_price * 1.35
            target_2 = entry_price * 1.65
            reward = target_1 - entry_price
            risk = entry_price - stop_loss
        return {
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "target_1": round(target_1, 2),
            "target_2": round(target_2, 2),
            "risk_reward": round(reward / risk, 2) if risk > 0 else 0.0,
        }

    def position_size(self, entry_price: float, stop_loss: float, lot_size: int, side: str) -> int:
        if lot_size <= 0:
            return 0
        per_unit_risk = abs(entry_price - stop_loss)
        if per_unit_risk <= 0:
            return 0
        max_risk = settings.account_equity * (settings.max_risk_per_trade_pct / 100)
        lots = floor(max_risk / (per_unit_risk * lot_size))
        return max(lot_size, lots * lot_size) if lots > 0 else lot_size

    def risk_checks(self, score: int, contract: OptionContract, entry_price: float, side: str) -> List[str]:
        failures: List[str] = []
        if score < settings.min_signal_score:
            failures.append("technical score is below threshold")
        if self.liquidity_score(contract) < settings.min_option_liquidity_score:
            failures.append("option liquidity is below threshold")
        if side.upper() == "BUY":
            max_premium = settings.account_equity * (settings.max_option_premium_pct / 100)
            if entry_price * contract.lot_size > max_premium:
                failures.append("option premium is too large for configured account risk")
        return failures

    def option_type_for(self, trend: str, side: str) -> str:
        bullish = trend.lower() == "bullish"
        if side.upper() == "SELL":
            return "PE" if bullish else "CE"
        return "CE" if bullish else "PE"

    def _contract_from_instrument(self, item: Dict[str, Any], quotes: Dict[str, Any]) -> OptionContract | None:
        tradingsymbol = str(item.get("tradingsymbol") or "")
        if not tradingsymbol:
            return None
        quote = quotes.get(f"{settings.option_exchange}:{tradingsymbol}") or quotes.get(tradingsymbol) or {}
        depth = quote.get("depth", {}) if isinstance(quote, dict) else {}
        buy_depth = depth.get("buy", []) if isinstance(depth, dict) else []
        sell_depth = depth.get("sell", []) if isinstance(depth, dict) else []
        bid = float(buy_depth[0].get("price", 0.0)) if buy_depth else 0.0
        ask = float(sell_depth[0].get("price", 0.0)) if sell_depth else 0.0
        return OptionContract(
            tradingsymbol=tradingsymbol,
            exchange=str(item.get("exchange") or settings.option_exchange),
            instrument_token=self._safe_int(item.get("instrument_token")),
            name=str(item.get("name") or ""),
            expiry=self._parse_expiry(item.get("expiry")).isoformat() if self._parse_expiry(item.get("expiry")) else "",
            strike=float(item.get("strike") or 0.0),
            option_type=str(item.get("instrument_type") or ""),
            lot_size=self._safe_int(item.get("lot_size"), 0) or 1,
            last_price=float(quote.get("last_price") or item.get("last_price") or 0.0),
            open_interest=float(quote.get("oi") or item.get("oi") or 0.0),
            volume=float(quote.get("volume") or item.get("volume") or 0.0),
            bid=bid,
            ask=ask,
        )

    def _underlying_matches(self, item: Dict[str, Any], underlying: str) -> bool:
        target = underlying.upper().replace(" ", "")
        name = str(item.get("name") or "").upper().replace(" ", "")
        symbol = str(item.get("tradingsymbol") or "").upper().replace(" ", "")
        return name == target or symbol.startswith(target)

    def _parse_expiry(self, value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            return None

    def _safe_int(self, value: Any, default: int | None = None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
