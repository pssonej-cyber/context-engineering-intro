"""Unit tests for state persistence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from btc_bot.config import BotConfig
from btc_bot.state import _today, append_log, load_state, save_state


def _config(tmp_path) -> BotConfig:
    return BotConfig(bitkub_api_key="", bitkub_api_secret="", state_dir=str(tmp_path))


def test_load_state_returns_default_when_missing(tmp_path):
    config = _config(tmp_path)
    state = load_state(config)
    assert state["open_trades"] == []
    assert state["closed_trades"] == []
    assert state["total_trades"] == 0
    assert state["daily_date"] == _today()


def test_save_then_load_roundtrip(tmp_path):
    config = _config(tmp_path)
    state = load_state(config)
    state["total_trades"] = 5
    state["winning_trades"] = 3
    save_state(config, state)

    reloaded = load_state(config)
    assert reloaded["total_trades"] == 5
    assert reloaded["winning_trades"] == 3


def test_daily_pnl_resets_on_date_change(tmp_path):
    config = _config(tmp_path)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    state = load_state(config)
    state["daily_pnl"] = 1234.5
    state["daily_date"] = yesterday
    save_state(config, state)

    reloaded = load_state(config)
    # Date rolled over → daily_pnl reset to 0
    assert reloaded["daily_pnl"] == 0
    assert reloaded["daily_date"] == _today()


def test_append_log_caps_at_1000_entries(tmp_path):
    config = _config(tmp_path)
    for i in range(1100):
        append_log(config, "TEST", {"i": i})
    with config.log_path().open() as f:
        logs = json.load(f)
    assert len(logs) == 1000
    # Most recent entries kept
    assert logs[-1]["i"] == 1099
    assert logs[0]["i"] == 100  # earliest 100 dropped
