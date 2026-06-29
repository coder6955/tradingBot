from __future__ import annotations

from typing import Any, Dict


class OptionAnalysisService:
    """Compute a lightweight set of option-chain metrics for recommendations."""

    def analyze(self, chain: Dict[str, Any]) -> Dict[str, float]:
        oi_call = float(chain.get("call_oi", 0.0))
        oi_put = float(chain.get("put_oi", 0.0))
        pcr = oi_put / oi_call if oi_call else 0.0
        max_pain = float(chain.get("max_pain", 0.0))
        return {
            "pcr": pcr,
            "max_pain": max_pain,
            "oi_change": float(chain.get("oi_change", 0.0)),
            "iv": float(chain.get("implied_volatility", 0.0)),
        }
