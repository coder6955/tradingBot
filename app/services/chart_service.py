from __future__ import annotations

from typing import Any, Dict, List


class ChartService:
    """Build simple chart-ready series for use in the dashboard."""

    def build_series(self, values: List[float], label: str) -> List[Dict[str, Any]]:
        return [{"x": idx, "y": value, "label": label} for idx, value in enumerate(values)]
