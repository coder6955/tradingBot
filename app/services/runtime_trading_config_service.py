from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings


class RuntimeTradingConfigService:
    """Session-level trading-mode choices controlled from the dashboard.

    This intentionally does not edit .env. It makes live/paper intent explicit,
    visible, and reversible for the running app process.
    """

    OPTIONAL_DEFAULTS = {
        "broker_emergency_sl": False,
        "partial_booking": False,
        "event_driven_live_entry": False,
        "telegram_alerts": True,
        "websocket_live_gap_polling_fallback": False,
        "confirm_one_live_trade_at_a_time": True,
        "event_driven_paper_entry": True,
        "paper_shadow_for_blocked_live": True,
    }

    def __init__(self) -> None:
        self.mode = str(settings.default_order_mode or "paper").lower()
        if self.mode not in {"paper", "live"}:
            self.mode = "paper"
        self.options: dict[str, bool] = dict(self.OPTIONAL_DEFAULTS)
        self.confirmed_at: str | None = None
        self.confirmed_by = "dashboard"
        self.warning_acknowledged = False

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "confirmed_at": self.confirmed_at,
            "confirmed_by": self.confirmed_by,
            "warning_acknowledged": self.warning_acknowledged,
            "required": self.required_config(self.mode),
            "optional": deepcopy(self.options),
            "effective": self.effective_config(),
            "note": "Runtime session config only; .env defaults are not modified.",
        }

    def preview(self, mode: str) -> dict[str, Any]:
        normalized = self._normalize_mode(mode)
        return {
            "mode": normalized,
            "required": self.required_config(normalized),
            "optional_choices": self.optional_choices(normalized),
            "warning": self._warning(normalized),
        }

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = self._normalize_mode(str(payload.get("mode") or self.mode))
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        merged = dict(self.OPTIONAL_DEFAULTS)
        for key in merged:
            if key in options:
                merged[key] = bool(options[key])
        if mode == "paper":
            merged["event_driven_live_entry"] = False
            merged["broker_emergency_sl"] = False
            merged["websocket_live_gap_polling_fallback"] = False
        if mode == "live":
            merged["confirm_one_live_trade_at_a_time"] = True
        self.mode = mode
        self.options = merged
        self.warning_acknowledged = bool(payload.get("warning_acknowledged", mode == "paper"))
        self.confirmed_by = str(payload.get("confirmed_by") or "dashboard")
        self.confirmed_at = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M:%S %p IST")
        self._apply_runtime_settings()
        return self.status()

    def automation_payload(self, base: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(base or {})
        payload.update(self.required_config(self.mode)["automation_payload"])
        payload["runtime_options"] = deepcopy(self.options)
        return payload

    def effective_config(self) -> dict[str, Any]:
        required = self.required_config(self.mode)
        return {
            "order_mode": self.mode,
            "automation": required["automation_payload"],
            "settings_overrides": required["settings_overrides"],
            "runtime_options": deepcopy(self.options),
        }

    def required_config(self, mode: str) -> dict[str, Any]:
        normalized = self._normalize_mode(mode)
        if normalized == "live":
            return {
                "summary": "Live automation can place real Zerodha orders after final confirmation.",
                "automation_payload": {
                    "order_mode": "live",
                    "place_orders": True,
                    "confirm_live": True,
                },
                "settings_overrides": {
                    "LIVE_TRADING_MODE": True,
                    "PAPER_TRADING_MODE": False,
                    "ENABLE_AUTO_SQUAREOFF": True,
                    "LIVE_AUTO_SQUAREOFF": True,
                    "ENABLE_KITE_WEBSOCKET": True,
                    "MAX_OPEN_TRADES": 1,
                },
            }
        return {
            "summary": "Paper automation uses real market data but records simulated orders only.",
            "automation_payload": {
                "order_mode": "paper",
                "place_orders": True,
                "confirm_live": False,
            },
            "settings_overrides": {
                "LIVE_TRADING_MODE": False,
                "PAPER_TRADING_MODE": True,
                "ENABLE_AUTO_SQUAREOFF": True,
                "LIVE_AUTO_SQUAREOFF": False,
                "ENABLE_KITE_WEBSOCKET": True,
            },
        }

    def optional_choices(self, mode: str) -> list[dict[str, Any]]:
        normalized = self._normalize_mode(mode)
        live_only = normalized == "live"
        choices = [
            {
                "key": "broker_emergency_sl",
                "label": "Broker emergency SL",
                "default": bool(self.options.get("broker_emergency_sl", False)) if live_only else False,
                "enabled": live_only,
                "description": "Places a Zerodha SL-M SELL order after live entry fill, so broker has a backup stop-loss.",
            },
            {
                "key": "partial_booking",
                "label": "Partial booking",
                "default": bool(self.options.get("partial_booking", False)),
                "enabled": True,
                "description": "Allows booking part quantity at target 1 when quantity can be split, then manages the remainder.",
            },
            {
                "key": "event_driven_live_entry",
                "label": "Event-driven live entry",
                "default": bool(self.options.get("event_driven_live_entry", False)) if live_only else False,
                "enabled": live_only,
                "description": "Allows live entry from armed WebSocket trigger flow. Keep off until paper evidence is strong.",
            },
            {
                "key": "event_driven_paper_entry",
                "label": "Event-driven paper entry",
                "default": bool(self.options.get("event_driven_paper_entry", True)),
                "enabled": True,
                "description": "Allows paper entries from armed WebSocket trigger flow for learning faster entry timing.",
            },
            {
                "key": "telegram_alerts",
                "label": "Telegram alerts",
                "default": bool(self.options.get("telegram_alerts", True)),
                "enabled": True,
                "description": "Sends important start, stop, error, and exit-failure alerts if Telegram is configured.",
            },
            {
                "key": "websocket_live_gap_polling_fallback",
                "label": "Live polling fallback",
                "default": bool(self.options.get("websocket_live_gap_polling_fallback", False)) if live_only else False,
                "enabled": live_only,
                "description": "If WebSocket gaps during a live trade, temporarily uses Kite polling for active price checks.",
            },
            {
                "key": "confirm_one_live_trade_at_a_time",
                "label": "One live trade at a time",
                "default": True,
                "enabled": live_only,
                "description": "Keeps live automation conservative by allowing only one open live trade.",
            },
            {
                "key": "paper_shadow_for_blocked_live",
                "label": "Paper shadow for blocked live",
                "default": bool(self.options.get("paper_shadow_for_blocked_live", True)),
                "enabled": True,
                "description": "Keeps a paper/shadow record for blocked live setups so learning continues without placing a real order.",
            },
        ]
        return choices

    def _warning(self, mode: str) -> str:
        if self._normalize_mode(mode) == "live":
            return "Live mode can place real Zerodha orders. Use only after Kite, broker reconciliation, data freshness, and risk checks are green."
        return "Paper mode will switch automation back to simulated order placement while still using real market data."

    def _normalize_mode(self, mode: str) -> str:
        normalized = str(mode or "paper").lower()
        return normalized if normalized in {"paper", "live"} else "paper"

    def _apply_runtime_settings(self) -> None:
        """Apply dashboard choices to the current process only.

        The Settings dataclass is frozen so startup defaults remain stable, but
        dashboard mode changes need to affect services that read settings while
        the process is running. These changes are intentionally not written to
        .env.
        """

        overrides = self.required_config(self.mode)["settings_overrides"]
        env_to_attr = {
            "LIVE_TRADING_MODE": "live_trading_mode",
            "PAPER_TRADING_MODE": "paper_trading_mode",
            "ENABLE_AUTO_SQUAREOFF": "enable_auto_squareoff",
            "LIVE_AUTO_SQUAREOFF": "live_auto_squareoff",
            "ENABLE_KITE_WEBSOCKET": "enable_kite_websocket",
            "MAX_OPEN_TRADES": "max_open_trades",
        }
        for env_key, attr in env_to_attr.items():
            if env_key in overrides:
                object.__setattr__(settings, attr, overrides[env_key])

        object.__setattr__(settings, "default_order_mode", self.mode)
        object.__setattr__(settings, "enable_broker_emergency_sl", bool(self.options.get("broker_emergency_sl", False)))
        object.__setattr__(settings, "enable_partial_booking", bool(self.options.get("partial_booking", False)))
        object.__setattr__(settings, "enable_event_driven_paper_entry", bool(self.options.get("event_driven_paper_entry", True)))
        object.__setattr__(
            settings,
            "enable_event_driven_live_entry",
            self.mode == "live" and bool(self.options.get("event_driven_live_entry", False)),
        )
        object.__setattr__(
            settings,
            "websocket_live_gap_polling_fallback",
            self.mode == "live" and bool(self.options.get("websocket_live_gap_polling_fallback", False)),
        )
        if self.mode == "live" and bool(self.options.get("confirm_one_live_trade_at_a_time", True)):
            object.__setattr__(settings, "max_open_trades", 1)
