"""Unit tests for risk.manager — sizing, gates, exit logic."""

from __future__ import annotations

from datetime import datetime, timedelta

from btc_bot.config import BotConfig
from btc_bot.risk.manager import RiskManager


def _config(**overrides) -> BotConfig:
    base = {
        "bitkub_api_key": "",
        "bitkub_api_secret": "",
        "risk_per_trade_pct": 2.0,
        "max_open_trades": 2,
        "daily_loss_limit_pct": 3.0,
        "max_portfolio_heat_pct": 6.0,
        "min_position_thb": 500.0,
        "trailing_stop_pct": 1.0,
        "tp1_rr_ratio": 2.0,
        "tp2_rr_ratio": 4.0,
        "partial_exit_pct": 50.0,
        "max_hold_hours": 96,
    }
    base.update(overrides)
    return BotConfig(**base)


def test_position_size_respects_risk_pct():
    rm = RiskManager(_config(risk_per_trade_pct=2.0))
    # Capital 100000 THB, risk 2% = 2000 THB
    # Entry 1,000,000, SL 990,000 → SL distance 1% of price
    # Position = 2000 / 0.01 = 200,000 (capped at 30% of 100000 = 30000)
    size = rm.position_size(capital_thb=100000, entry=1_000_000, stop_loss=990_000)
    assert size == 30000  # 30% cap kicks in


def test_position_size_zero_when_below_minimum():
    rm = RiskManager(_config(min_position_thb=500))
    # Tiny capital → position would be < 500 THB
    size = rm.position_size(capital_thb=100, entry=1_000_000, stop_loss=990_000)
    assert size == 0.0


def test_position_size_zero_when_no_risk():
    rm = RiskManager(_config())
    # entry == stop_loss → division by zero must not crash
    size = rm.position_size(capital_thb=100000, entry=1_000_000, stop_loss=1_000_000)
    assert size == 0.0


def test_can_trade_blocks_at_max_open_trades():
    rm = RiskManager(_config(max_open_trades=2))
    state = {
        "open_trades": [{"amount_thb": 1000}, {"amount_thb": 1000}],
        "daily_pnl": 0,
        "recent_results": [],
    }
    ok, issues = rm.can_trade(state, total_capital=100000)
    assert not ok
    assert any("max_open_trades" in i for i in issues)


def test_can_trade_blocks_at_daily_loss_limit():
    rm = RiskManager(_config(daily_loss_limit_pct=3.0))
    state = {
        "open_trades": [],
        "daily_pnl": -5000,  # 5% of 100k capital → over 3% limit
        "recent_results": [],
    }
    ok, issues = rm.can_trade(state, total_capital=100000)
    assert not ok
    assert any("daily_loss_limit" in i for i in issues)


def test_can_trade_blocks_during_cooldown():
    rm = RiskManager(_config(cooldown_after_losses=3, cooldown_hours=6))
    state = {
        "open_trades": [],
        "daily_pnl": 0,
        "recent_results": [-100, -100, -100],
        "last_loss_time": (datetime.now() - timedelta(hours=1)).isoformat(),
    }
    ok, issues = rm.can_trade(state, total_capital=100000)
    assert not ok
    assert any("cooldown" in i for i in issues)


def test_can_trade_allows_after_cooldown_expired():
    rm = RiskManager(_config(cooldown_after_losses=3, cooldown_hours=6))
    state = {
        "open_trades": [],
        "daily_pnl": 0,
        "recent_results": [-100, -100, -100],
        "last_loss_time": (datetime.now() - timedelta(hours=10)).isoformat(),
    }
    ok, _ = rm.can_trade(state, total_capital=100000)
    assert ok


def test_can_trade_passes_when_clear():
    rm = RiskManager(_config())
    state = {"open_trades": [], "daily_pnl": 0, "recent_results": []}
    ok, issues = rm.can_trade(state, total_capital=100000)
    assert ok
    assert issues == []


def test_check_exits_stop_loss_triggered():
    rm = RiskManager(_config())
    trade = {
        "entry_price": 1_000_000,
        "stop_loss": 990_000,
        "initial_sl": 990_000,
        "direction": "long",
        "amount_btc": 0.001,
        "amount_thb": 1000,
        "highest_price": 1_000_000,
        "entry_time": datetime.now().isoformat(),
        "partial_exited": False,
    }
    decisions = rm.check_exits(current_price=985_000, open_trades=[trade])
    assert len(decisions) == 1
    assert decisions[0]["action"] == "close"
    assert "STOP_LOSS" in decisions[0]["reason"]


def test_check_exits_tp1_partial():
    rm = RiskManager(_config(tp1_rr_ratio=2.0))
    # Risk = 10k, TP1 at 2R = 1,020,000
    trade = {
        "entry_price": 1_000_000,
        "stop_loss": 990_000,
        "initial_sl": 990_000,
        "direction": "long",
        "amount_btc": 0.001,
        "amount_thb": 1000,
        "highest_price": 1_000_000,
        "entry_time": datetime.now().isoformat(),
        "partial_exited": False,
    }
    decisions = rm.check_exits(current_price=1_021_000, open_trades=[trade])
    assert len(decisions) == 1
    assert decisions[0]["action"] == "partial"
    assert "TP1" in decisions[0]["reason"]


def test_check_exits_trailing_stop_after_partial():
    rm = RiskManager(_config(trailing_stop_pct=1.0))
    trade = {
        "entry_price": 1_000_000,
        "stop_loss": 1_000_000,        # moved to breakeven after TP1
        "initial_sl": 990_000,
        "direction": "long",
        "amount_btc": 0.0005,
        "amount_thb": 500,
        "highest_price": 1_050_000,    # peak
        "entry_time": datetime.now().isoformat(),
        "partial_exited": True,
    }
    # Drawdown 1.5% from 1,050,000 = 1,034,250 → triggers 1% trail
    decisions = rm.check_exits(current_price=1_034_250, open_trades=[trade])
    assert len(decisions) == 1
    assert "TRAILING_STOP" in decisions[0]["reason"]


def test_check_exits_no_action_when_in_profit_no_threshold_hit():
    rm = RiskManager(_config())
    trade = {
        "entry_price": 1_000_000,
        "stop_loss": 990_000,
        "initial_sl": 990_000,
        "direction": "long",
        "amount_btc": 0.001,
        "amount_thb": 1000,
        "highest_price": 1_000_000,
        "entry_time": datetime.now().isoformat(),
        "partial_exited": False,
    }
    # Up 0.5% — neither TP1 nor SL hit
    decisions = rm.check_exits(current_price=1_005_000, open_trades=[trade])
    assert decisions == []
