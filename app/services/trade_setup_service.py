from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import floor
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings
from app.services.account_funds_service import AccountFundsService


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
    oi_day_high: float = 0.0
    oi_day_low: float = 0.0


class TradeSetupService:
    """Select liquid option contracts and convert ranked setups into executable plans."""

    def __init__(self, account_funds_service: AccountFundsService | None = None) -> None:
        self.account_funds_service = account_funds_service or AccountFundsService()

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
        enforce_budget: bool = False,
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

        interval = self._strike_interval(candidates)
        target = self._target_strike(spot_price, option_type, side, interval)
        if enforce_budget and side.upper() == "BUY":
            affordable = self._affordable_buy_candidates(candidates)
            if affordable:
                return max(affordable, key=lambda contract: self._contract_score(contract, target))
        return max(candidates, key=lambda contract: self._contract_score(contract, target))

    def build_contracts(
        self,
        instruments: Iterable[Dict[str, Any]],
        underlying: str,
        quotes: Dict[str, Any] | None = None,
    ) -> List[OptionContract]:
        expiry = self.nearest_expiry(instruments, underlying)
        if expiry is None:
            return []
        contracts: List[OptionContract] = []
        for item in instruments:
            if not self._underlying_matches(item, underlying):
                continue
            if str(item.get("instrument_type")) not in {"CE", "PE"}:
                continue
            item_expiry = self._parse_expiry(item.get("expiry"))
            if item_expiry is None or item_expiry.isoformat() != expiry:
                continue
            contract = self._contract_from_instrument(item, quotes or {})
            if contract:
                contracts.append(contract)
        return contracts

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

    def build_prices(self, entry_price: float, side: str, underlying: str | None = None, snapshot: Dict[str, Any] | None = None) -> Dict[str, float]:
        if (underlying or "").upper() == "BANKNIFTY" and side.upper() == "BUY":
            return self._banknifty_buy_prices(entry_price, snapshot or {})
        if side.upper() == "SELL":
            stop_loss = entry_price * 1.35
            target_1 = entry_price * 0.75
            target_2 = entry_price * 0.55
            target_3 = entry_price * 0.35
            reward = entry_price - target_1
            risk = stop_loss - entry_price
        else:
            stop_loss = entry_price * 0.78
            target_1 = entry_price * 1.35
            target_2 = entry_price * 1.65
            target_3 = entry_price * 2.0
            reward = target_1 - entry_price
            risk = entry_price - stop_loss
        return {
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "target_1": round(target_1, 2),
            "target_2": round(target_2, 2),
            "target_3": round(target_3, 2),
            "risk_reward": round(reward / risk, 2) if risk > 0 else 0.0,
        }

    def _banknifty_buy_prices(self, entry_price: float, snapshot: Dict[str, Any]) -> Dict[str, float]:
        risk_pct = self._banknifty_premium_risk_pct(snapshot)
        target_1_pct = risk_pct * 1.45
        target_2_pct = risk_pct * 2.05
        target_3_pct = risk_pct * 2.75
        stop_loss = entry_price * (1 - risk_pct)
        target_1 = entry_price * (1 + target_1_pct)
        target_2 = entry_price * (1 + target_2_pct)
        target_3 = entry_price * (1 + target_3_pct)
        return {
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "target_1": round(target_1, 2),
            "target_2": round(target_2, 2),
            "target_3": round(target_3, 2),
            "risk_reward": round(target_1_pct / risk_pct, 2) if risk_pct > 0 else 0.0,
            "risk_model": "banknifty_adaptive_premium",
            "premium_risk_pct": round(risk_pct * 100, 2),
        }

    def _banknifty_premium_risk_pct(self, snapshot: Dict[str, Any]) -> float:
        price = float(snapshot.get("price") or 0.0)
        day_high = float(snapshot.get("day_high") or 0.0)
        day_low = float(snapshot.get("day_low") or 0.0)
        day_range_pct = ((day_high - day_low) / price) if price > 0 and day_high > day_low else 0.006
        adx = float(snapshot.get("adx") or 15.0)
        risk_pct = 0.16 + min(day_range_pct * 10, 0.08)
        if adx >= 22:
            risk_pct += 0.02
        if not bool(snapshot.get("volume_confirmed")):
            risk_pct -= 0.02
        return max(0.16, min(0.28, risk_pct))

    def position_size(self, entry_price: float, stop_loss: float, lot_size: int, side: str, account_equity: float | None = None) -> int:
        if lot_size <= 0:
            return 0
        per_unit_risk = abs(entry_price - stop_loss)
        if per_unit_risk <= 0:
            return 0
        equity = account_equity if account_equity is not None else self._available_cash()
        max_risk = equity * (settings.max_risk_per_trade_pct / 100)
        lots = floor(max_risk / (per_unit_risk * lot_size))
        return max(lot_size, lots * lot_size) if lots > 0 else lot_size

    def affordable_quantity(self, entry_price: float, lot_size: int, available_funds: float, side: str) -> int:
        if lot_size <= 0 or entry_price <= 0 or available_funds <= 0:
            return 0
        if side.upper() == "SELL":
            return lot_size
        affordable_lots = floor(available_funds / (entry_price * lot_size))
        return max(0, affordable_lots * lot_size)

    def risk_checks(self, score: int, contract: OptionContract, entry_price: float, side: str, enforce_budget: bool = False) -> List[str]:
        failures: List[str] = []
        if self.liquidity_score(contract) < settings.min_option_liquidity_score:
            failures.append("option liquidity is below threshold")
        if side.upper() == "BUY":
            if entry_price < settings.min_option_buy_premium:
                failures.append("option premium is below minimum configured for buying")
            expiry = self._parse_expiry(contract.expiry)
            if settings.block_expiry_day_option_buying and expiry is not None and expiry <= date.today():
                failures.append("expiry-day option buying is blocked")
            if enforce_budget:
                available_cash = self._available_cash()
                max_premium = available_cash * (settings.max_option_premium_pct / 100)
                one_lot_cost = entry_price * contract.lot_size
                if one_lot_cost > max_premium:
                    failures.append(
                        "option premium is too large for configured account risk "
                        f"(one lot costs {one_lot_cost:.2f}, allowed {max_premium:.2f} from Kite cash {available_cash:.2f})"
                    )
        else:
            if not settings.allow_option_selling:
                failures.append("option selling is disabled by configuration")
        if self._spread_pct(contract) > settings.max_bid_ask_spread_pct:
            failures.append("bid/ask spread is too wide")
        if contract.volume < settings.min_option_volume:
            failures.append("option volume is below threshold")
        if contract.open_interest < settings.min_option_oi:
            failures.append("option open interest is below threshold")
        return failures

    def _available_cash(self) -> float:
        try:
            return self.account_funds_service.available_cash()
        except Exception:
            return 0.0

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
            oi_day_high=float(quote.get("oi_day_high") or 0.0),
            oi_day_low=float(quote.get("oi_day_low") or 0.0),
        )

    def _strike_interval(self, contracts: List[OptionContract]) -> float:
        strikes = sorted({contract.strike for contract in contracts})
        diffs = [strikes[idx] - strikes[idx - 1] for idx in range(1, len(strikes)) if strikes[idx] > strikes[idx - 1]]
        return min(diffs) if diffs else 50.0

    def _target_strike(self, spot_price: float, option_type: str, side: str, interval: float) -> float:
        if side.upper() == "SELL":
            return spot_price - interval if option_type == "PE" else spot_price + interval
        return spot_price - (interval * 0.5) if option_type == "CE" else spot_price + (interval * 0.5)

    def _contract_score(self, contract: OptionContract, target: float) -> float:
        distance_penalty = abs(contract.strike - target)
        return self.liquidity_score(contract) - (distance_penalty / max(contract.strike, 1.0) * 1000)

    def _affordable_buy_candidates(self, contracts: List[OptionContract]) -> List[OptionContract]:
        available_cash = self._available_cash()
        if available_cash <= 0:
            return []
        max_premium = available_cash * (settings.max_option_premium_pct / 100)
        affordable: List[OptionContract] = []
        for contract in contracts:
            entry_price = contract.ask or contract.last_price
            if entry_price <= 0:
                continue
            if entry_price * contract.lot_size <= max_premium:
                affordable.append(contract)
        return affordable

    def _spread_pct(self, contract: OptionContract) -> float:
        if not contract.bid or not contract.ask or not contract.last_price:
            return 100.0
        return ((contract.ask - contract.bid) / contract.last_price) * 100

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
