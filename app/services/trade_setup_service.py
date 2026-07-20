from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import floor
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings
from app.services.account_funds_service import AccountFundsService
from app.services.database import Candle, get_session
from app.services.time_utils import ist_now_naive, ist_today


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
    bid_quantity: int = 0
    ask_quantity: int = 0
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None


class TradeSetupService:
    """Select liquid option contracts and convert ranked setups into executable plans."""

    def __init__(self, account_funds_service: AccountFundsService | None = None) -> None:
        self.account_funds_service = account_funds_service or AccountFundsService()
        self._sticky_contracts: dict[str, tuple[int, datetime]] = {}

    def nearest_expiry(self, instruments: Iterable[Dict[str, Any]], underlying: str) -> Optional[str]:
        expiries: List[date] = []
        for item in instruments:
            if self._underlying_matches(item, underlying) and item.get("instrument_type") in {"CE", "PE"}:
                expiry = self._parse_expiry(item.get("expiry"))
                if expiry:
                    expiries.append(expiry)
        if not expiries:
            return None
        today = ist_today()
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
                candidates = affordable
        ranked = self.rank_contracts(candidates=candidates, spot_price=spot_price, target_strike=target, side=side)
        selected = ranked[0]["contract"] if ranked else max(candidates, key=lambda contract: self._contract_score(contract, target))
        if underlying.upper() != "BANKNIFTY":
            return selected
        return self._sticky_contract_selection(
            key=f"{underlying.upper()}|{option_type}|{side.upper()}|{expiry}",
            candidates=candidates,
            selected=selected,
            target=target,
        )

    def rank_contracts(
        self,
        *,
        candidates: List[OptionContract],
        spot_price: float,
        target_strike: float | None = None,
        side: str = "BUY",
    ) -> list[dict[str, Any]]:
        """Rank contracts by executable price, liquidity, Greeks, DTE, and stability."""
        if not candidates:
            return []
        target = float(target_strike if target_strike is not None else spot_price)
        rows: list[dict[str, Any]] = []
        for contract in candidates:
            executable = contract.ask if side.upper() == "BUY" else contract.bid
            spread_pct = self._spread_pct(contract)
            distance_intervals = abs(contract.strike - target) / max(self._strike_interval(candidates), 1.0)
            score = float(self.liquidity_score(contract))
            score -= min(30.0, distance_intervals * 8.0)
            score -= min(35.0, spread_pct * 4.0)
            if executable <= 0:
                score -= 60.0
            if contract.bid_quantity >= settings.contract_min_depth_quantity:
                score += 6.0
            if contract.ask_quantity >= settings.contract_min_depth_quantity:
                score += 4.0
            if contract.delta is not None:
                abs_delta = abs(float(contract.delta))
                if settings.min_option_buy_delta <= abs_delta <= settings.max_option_buy_delta:
                    score += 10.0
                else:
                    score -= 8.0
            if contract.theta is not None and executable > 0:
                theta_pct = abs(float(contract.theta)) / executable * 100
                if theta_pct > settings.max_option_buy_theta_pct:
                    score -= 12.0
            expiry = self._parse_expiry(contract.expiry)
            dte = (expiry - ist_today()).days if expiry else None
            if dte is not None and dte <= 0 and settings.block_expiry_day_option_buying:
                score -= 50.0
            rows.append(
                {
                    "contract": contract,
                    "score": round(max(0.0, min(100.0, score)), 2),
                    "executable_price": executable,
                    "spread_pct": round(spread_pct, 3),
                    "distance_intervals": round(distance_intervals, 3),
                    "dte": dte,
                    "depth": {"bid_quantity": contract.bid_quantity, "ask_quantity": contract.ask_quantity},
                    "greeks": {"delta": contract.delta, "gamma": contract.gamma, "theta": contract.theta, "iv": contract.implied_volatility},
                }
            )
        rows.sort(key=lambda row: (float(row["score"]), -float(row["spread_pct"]), float(row["executable_price"])), reverse=True)
        return rows[: max(1, settings.contract_max_ranked_candidates)]

    def _sticky_contract_selection(
        self,
        *,
        key: str,
        candidates: List[OptionContract],
        selected: OptionContract,
        target: float,
    ) -> OptionContract:
        now = ist_now_naive()
        existing = self._sticky_contracts.get(key)
        if existing is not None:
            token, selected_at = existing
            age = max(0.0, (now - selected_at).total_seconds())
            pinned = next((contract for contract in candidates if contract.instrument_token == token), None)
            if pinned is not None and age <= max(0, settings.banknifty_contract_stickiness_seconds):
                selected_score = self._contract_score(selected, target)
                pinned_score = self._contract_score(pinned, target)
                spread_safe = self._spread_pct(pinned) <= settings.max_bid_ask_spread_pct
                if spread_safe and selected_score - pinned_score < settings.banknifty_contract_switch_score_advantage:
                    return pinned
        if selected.instrument_token:
            self._sticky_contracts[key] = (int(selected.instrument_token), now)
        return selected

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

    def build_prices(
        self,
        entry_price: float,
        side: str,
        underlying: str | None = None,
        snapshot: Dict[str, Any] | None = None,
        contract: OptionContract | None = None,
    ) -> Dict[str, float]:
        if entry_price <= 0:
            raise ValueError("entry price must be positive before calculating stop loss and targets")
        if (underlying or "").upper() == "BANKNIFTY" and side.upper() == "BUY":
            return self._banknifty_buy_prices(entry_price, snapshot or {}, contract=contract)
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

    def _banknifty_buy_prices(self, entry_price: float, snapshot: Dict[str, Any], contract: OptionContract | None = None) -> Dict[str, float]:
        structure = self._option_premium_structure(contract.tradingsymbol if contract else "")
        spread = max((contract.ask - contract.bid) if contract and contract.ask and contract.bid else 0.0, 0.0)
        atr = float(structure.get("atr") or 0.0)
        swing_low = float(structure.get("swing_low") or 0.0)
        swing_high = float(structure.get("swing_high") or 0.0)
        atr_pct = atr / max(entry_price, 0.05)
        spread_pct = spread / max(entry_price, 0.05)
        fallback_risk_pct = self._banknifty_premium_risk_pct(snapshot)
        min_noise_risk = max(entry_price * 0.10, atr * 1.05, spread * 2.0, 0.05)
        max_risk = entry_price * self._max_banknifty_risk_pct(entry_price, atr_pct, spread_pct, snapshot)

        if swing_low > 0 and swing_low < entry_price:
            structure_risk = entry_price - max(0.05, swing_low - max(spread, atr * 0.20))
        else:
            structure_risk = entry_price * fallback_risk_pct
        risk = max(min_noise_risk, structure_risk)
        risk = min(max(risk, entry_price * 0.12), max_risk)

        stop_loss = max(0.05, entry_price - risk)
        target_1 = self._first_target(entry_price=entry_price, risk=risk, atr=atr, swing_high=swing_high, spread=spread, snapshot=snapshot)
        target_2 = max(target_1 + risk * 0.55, entry_price + risk * 2.05, target_1 * 1.10)
        target_3 = max(target_2 + risk * 0.65, entry_price + risk * 3.00, target_2 * 1.12)
        reward = target_1 - entry_price
        return {
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "target_1": round(target_1, 2),
            "target_2": round(target_2, 2),
            "target_3": round(target_3, 2),
            "risk_reward": round(reward / risk, 2) if risk > 0 else 0.0,
            "risk_model": "banknifty_structure_atr_premium",
            "premium_risk_pct": round((risk / max(entry_price, 0.05)) * 100, 2),
            "premium_atr": round(atr, 2),
            "premium_atr_pct": round(atr_pct * 100, 2),
            "premium_swing_low": round(swing_low, 2) if swing_low else 0.0,
            "premium_swing_high": round(swing_high, 2) if swing_high else 0.0,
            "spread_cushion": round(spread, 2),
            "exit_logic": "SL below recent option premium support with ATR/spread noise buffer; targets from premium resistance and R-multiples.",
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

    def _option_premium_structure(self, tradingsymbol: str, timeframe: str = "5minute", limit: int = 24) -> Dict[str, float]:
        if not tradingsymbol:
            return {"atr": 0.0, "swing_low": 0.0, "swing_high": 0.0}
        session = get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == tradingsymbol, Candle.timeframe == timeframe)
                .order_by(Candle.timestamp.desc())
                .limit(limit)
                .all()
            )
            candles = list(reversed(rows))
        finally:
            session.close()
        if len(candles) < 4:
            return {"atr": 0.0, "swing_low": 0.0, "swing_high": 0.0}
        true_ranges: list[float] = []
        previous_close = float(candles[0].close_price)
        for candle in candles[1:]:
            high = float(candle.high_price)
            low = float(candle.low_price)
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
            previous_close = float(candle.close_price)
        recent = candles[-8:] if len(candles) >= 8 else candles
        return {
            "atr": sum(true_ranges[-14:]) / max(len(true_ranges[-14:]), 1),
            "swing_low": min(float(candle.low_price) for candle in recent),
            "swing_high": max(float(candle.high_price) for candle in recent),
        }

    def _max_banknifty_risk_pct(self, entry_price: float, atr_pct: float, spread_pct: float, snapshot: Dict[str, Any]) -> float:
        adx = float(snapshot.get("adx") or 15.0)
        room_pct = float(snapshot.get("room_to_level_pct") or 0.0)
        cap = 0.30
        if entry_price < settings.min_option_buy_premium * 1.5:
            cap = 0.24
        if atr_pct > 0.20 or spread_pct > 0.03:
            cap -= 0.03
        if adx >= 25:
            cap += 0.03
        if room_pct and room_pct < settings.min_directional_room_pct * 1.5:
            cap -= 0.03
        return max(0.18, min(0.36, cap))

    def _first_target(self, *, entry_price: float, risk: float, atr: float, swing_high: float, spread: float, snapshot: Dict[str, Any]) -> float:
        minimum_target = entry_price + max(risk * 1.20, atr * 1.35, spread * 3.0, entry_price * 0.14)
        if swing_high > entry_price and (swing_high - entry_price) >= risk * 1.05:
            return max(minimum_target, min(swing_high, entry_price + risk * 1.80))
        adx = float(snapshot.get("adx") or 15.0)
        multiplier = 1.45 if adx < 24 else 1.65
        return max(minimum_target, entry_price + risk * multiplier)

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
            if settings.block_expiry_day_option_buying and expiry is not None and expiry <= ist_today():
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
        bid_quantity = int(float(buy_depth[0].get("quantity", 0) or 0)) if buy_depth else 0
        ask_quantity = int(float(sell_depth[0].get("quantity", 0) or 0)) if sell_depth else 0
        greeks = quote.get("greeks", {}) if isinstance(quote.get("greeks"), dict) else {}
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
            bid_quantity=bid_quantity,
            ask_quantity=ask_quantity,
            implied_volatility=self._optional_float(quote.get("implied_volatility") or quote.get("iv") or greeks.get("iv")),
            delta=self._optional_float(quote.get("delta") or greeks.get("delta")),
            gamma=self._optional_float(quote.get("gamma") or greeks.get("gamma")),
            theta=self._optional_float(quote.get("theta") or greeks.get("theta")),
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
        executable_penalty = 60.0 if contract.ask <= 0 else 0.0
        depth_bonus = 5.0 if contract.bid_quantity >= settings.contract_min_depth_quantity else 0.0
        return self.liquidity_score(contract) + depth_bonus - executable_penalty - (distance_penalty / max(contract.strike, 1.0) * 1000)

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

    def _optional_float(self, value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
