from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from math import erf, exp, log, pi, sqrt
from typing import Any


@dataclass(frozen=True)
class Greeks:
    implied_volatility: float
    delta: float
    gamma: float
    theta: float
    theta_pct: float
    vega: float
    intrinsic_value: float
    extrinsic_value: float
    days_to_expiry: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


class GreeksService:
    """Estimate Black-Scholes Greeks for option-buying quality checks."""

    def estimate(
        self,
        *,
        spot_price: float,
        strike: float,
        option_price: float,
        option_type: str,
        expiry: str | date | datetime | None,
        risk_free_rate: float = 0.065,
    ) -> Greeks:
        days = max(1, self._days_to_expiry(expiry))
        time_years = max(days / 365.0, 1 / 365.0)
        intrinsic = self._intrinsic_value(spot_price, strike, option_type)
        extrinsic = max(0.0, option_price - intrinsic)
        iv = self._implied_volatility(
            spot_price=spot_price,
            strike=strike,
            option_price=max(option_price, 0.01),
            option_type=option_type,
            time_years=time_years,
            risk_free_rate=risk_free_rate,
        )
        delta, gamma, theta, vega = self._greeks(spot_price, strike, option_type, time_years, risk_free_rate, iv)
        theta_pct = abs(theta) / max(option_price, 0.01) * 100
        return Greeks(
            implied_volatility=round(iv, 4),
            delta=round(delta, 4),
            gamma=round(gamma, 6),
            theta=round(theta, 4),
            theta_pct=round(theta_pct, 2),
            vega=round(vega, 4),
            intrinsic_value=round(intrinsic, 2),
            extrinsic_value=round(extrinsic, 2),
            days_to_expiry=days,
        )

    def _implied_volatility(
        self,
        *,
        spot_price: float,
        strike: float,
        option_price: float,
        option_type: str,
        time_years: float,
        risk_free_rate: float,
    ) -> float:
        low = 0.01
        high = 3.0
        for _ in range(60):
            mid = (low + high) / 2
            theoretical = self._price(spot_price, strike, option_type, time_years, risk_free_rate, mid)
            if theoretical > option_price:
                high = mid
            else:
                low = mid
        return (low + high) / 2

    def _price(self, spot: float, strike: float, option_type: str, t: float, r: float, sigma: float) -> float:
        d1, d2 = self._d1_d2(spot, strike, t, r, sigma)
        if option_type.upper() == "CE":
            return spot * self._norm_cdf(d1) - strike * exp(-r * t) * self._norm_cdf(d2)
        return strike * exp(-r * t) * self._norm_cdf(-d2) - spot * self._norm_cdf(-d1)

    def _greeks(self, spot: float, strike: float, option_type: str, t: float, r: float, sigma: float) -> tuple[float, float, float, float]:
        d1, d2 = self._d1_d2(spot, strike, t, r, sigma)
        pdf = self._norm_pdf(d1)
        gamma = pdf / (spot * sigma * sqrt(t))
        vega = spot * pdf * sqrt(t) / 100
        if option_type.upper() == "CE":
            delta = self._norm_cdf(d1)
            theta = (-(spot * pdf * sigma) / (2 * sqrt(t)) - r * strike * exp(-r * t) * self._norm_cdf(d2)) / 365
        else:
            delta = self._norm_cdf(d1) - 1
            theta = (-(spot * pdf * sigma) / (2 * sqrt(t)) + r * strike * exp(-r * t) * self._norm_cdf(-d2)) / 365
        return delta, gamma, theta, vega

    def _d1_d2(self, spot: float, strike: float, t: float, r: float, sigma: float) -> tuple[float, float]:
        spot = max(spot, 0.01)
        strike = max(strike, 0.01)
        sigma = max(sigma, 0.01)
        d1 = (log(spot / strike) + (r + (sigma**2 / 2)) * t) / (sigma * sqrt(t))
        return d1, d1 - sigma * sqrt(t)

    def _intrinsic_value(self, spot: float, strike: float, option_type: str) -> float:
        if option_type.upper() == "CE":
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)

    def _days_to_expiry(self, expiry: Any) -> int:
        parsed = self._parse_date(expiry)
        if parsed is None:
            return 7
        return max(0, (parsed - date.today()).days)

    def _parse_date(self, value: Any) -> date | None:
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

    def _norm_cdf(self, value: float) -> float:
        return 0.5 * (1 + erf(value / sqrt(2)))

    def _norm_pdf(self, value: float) -> float:
        return exp(-(value**2) / 2) / sqrt(2 * pi)
