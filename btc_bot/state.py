"""Persistent JSON state for open trades, closed trades, and daily PnL.

The bot is invoked by a systemd timer every 15 min, so state must survive
between runs. We deliberately keep the schema as plain dicts (not dataclasses
in the JSON) so the file is human-readable.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from btc_bot.config import BotConfig


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _default_state() -> dict[str, Any]:
    return {
        "open_trades": [],
        "closed_trades": [],
        "daily_pnl": 0.0,
        "daily_date": _today(),
        "total_trades": 0,
        "winning_trades": 0,
        "total_pnl": 0.0,
        "recent_results": [],
        "last_loss_time": "",
    }


def load_state(config: BotConfig) -> dict[str, Any]:
    """Load state from disk; reset daily PnL if the date has rolled over."""
    path = config.state_path()
    if not path.exists():
        return _default_state()
    try:
        with path.open() as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()

    # Reset daily PnL on date change
    if state.get("daily_date") != _today():
        state["daily_pnl"] = 0.0
        state["daily_date"] = _today()

    # Backfill any missing keys (schema migrations)
    for k, v in _default_state().items():
        state.setdefault(k, v)
    return state


def save_state(config: BotConfig, state: dict[str, Any]) -> None:
    path = config.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def append_log(config: BotConfig, action: str, details: dict[str, Any]) -> None:
    """Append a log entry; cap log file at last 1000 entries to bound disk."""
    path = config.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        logs = json.load(path.open()) if path.exists() else []
    except (json.JSONDecodeError, OSError):
        logs = []
    logs.append({"time": datetime.now().isoformat(), "action": action, **details})
    logs = logs[-1000:]
    with path.open("w") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)
