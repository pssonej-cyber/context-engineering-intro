"""Risk management: position sizing, gating new trades, exit logic.

The RiskManager has no I/O — it operates on the state dict and returns
decisions for the orchestrator to act on. This makes it trivially unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from btc_bot.config import BotConfig


class RiskManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    # ─── Pre-trade gating ───────────────────────────────────────────
    def can_trade(self, state: dict[str, Any], total_capital: float) -> tuple[bool, list[str]]:
        """Check all gates before opening a new position.

        Returns (ok, list_of_reasons_blocked).
        """
        issues: list[str] = []

        # Daily loss limit
        if state.get("daily_pnl", 0) < 0 and total_capital > 0:
            loss_pct = abs(state["daily_pnl"]) / total_capital * 100
            if loss_pct > self.config.daily_loss_limit_pct:
                issues.append(f"daily_loss_limit ({loss_pct:.1f}%)")

        # Portfolio heat (sum of capital at risk in open trades)
        heat = self._portfolio_heat(state, total_capital)
        if heat >= self.config.max_portfolio_heat_pct:
            issues.append(f"portfolio_heat_max ({heat:.1f}%)")

        # Max simultaneous trades
        if len(state.get("open_trades", [])) >= self.config.max_open_trades:
            issues.append(f"max_open_trades ({self.config.max_open_trades})")

        # Cooldown after consecutive losses
        recent = state.get("recent_results", [])
        n = self.config.cooldown_after_losses
        if len(recent) >= n and all(r < 0 for r in recent[-n:]):
            last = state.get("last_loss_time", "")
            if last:
                try:
                    lt = datetime.fromisoformat(last)
                    if datetime.now() - lt < timedelta(hours=self.config.cooldown_hours):
                        issues.append(f"cooldown ({n} losses)")
                except ValueError:
                    pass

        return (len(issues) == 0, issues)

    def _portfolio_heat(self, state: dict[str, Any], total_capital: float) -> float:
        if total_capital <= 0:
            return 0.0
        deployed = sum(t.get("amount_thb", 0) for t in state.get("open_trades", []))
        return deployed / total_capital * 100

    # ─── Position sizing ────────────────────────────────────────────
    def position_size(self, capital_thb: float, entry: float, stop_loss: float) -> float:
        """Risk-based position sizing in THB.

        Risk = capital * RISK_PCT. Position scaled so a SL hit loses exactly that.
        Capped at min(MIN_POSITION, 30% of capital, available capital).
        """
        risk_amount = capital_thb * (self.config.risk_per_trade_pct / 100)
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit == 0 or entry == 0:
            return 0.0

        stop_dist_pct = risk_per_unit / entry
        position_thb = risk_amount / stop_dist_pct if stop_dist_pct > 0 else 0
        position_thb = min(position_thb, capital_thb * 0.3)

        if position_thb < self.config.min_position_thb:
            return 0.0
        return float(int(position_thb))

    # ─── Exit logic ─────────────────────────────────────────────────
    def check_exits(
        self, current_price: float, open_trades: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Determine which trades should exit (or partially exit) at current price.

        Returns a list of decisions: {trade, action, pct, reason, pnl_pct}
        action ∈ {"close", "partial"}; pct is the % of position to exit.
        """
        decisions: list[dict[str, Any]] = []

        for trade in open_trades:
            entry = trade["entry_price"]
            sl = trade["stop_loss"]
            direction = trade["direction"]
            partial_done = trade.get("partial_exited", False)

            if direction == "long":
                pnl_pct = (current_price - entry) / entry * 100
            else:
                pnl_pct = (entry - current_price) / entry * 100

            # 1. Stop loss
            sl_hit = (
                (direction == "long" and current_price <= sl)
                or (direction == "short" and current_price >= sl)
            )
            if sl_hit:
                decisions.append(
                    {
                        "trade": trade,
                        "action": "close",
                        "pct": 100,
                        "reason": f"STOP_LOSS ({pnl_pct:+.1f}%)",
                        "pnl_pct": pnl_pct,
                    }
                )
                continue

            # 2. Partial exit at TP1 (only once)
            risk = abs(entry - trade.get("initial_sl", sl))
            if direction == "long":
                tp1 = entry + risk * self.config.tp1_rr_ratio
                tp2 = entry + risk * self.config.tp2_rr_ratio
            else:
                tp1 = entry - risk * self.config.tp1_rr_ratio
                tp2 = entry - risk * self.config.tp2_rr_ratio

            tp2_hit = (
                (direction == "long" and current_price >= tp2)
                or (direction == "short" and current_price <= tp2)
            )
            if tp2_hit and partial_done:
                decisions.append(
                    {
                        "trade": trade,
                        "action": "close",
                        "pct": 100,
                        "reason": f"TP2 ({pnl_pct:+.1f}%)",
                        "pnl_pct": pnl_pct,
                    }
                )
                continue

            tp1_hit = (
                (direction == "long" and current_price >= tp1)
                or (direction == "short" and current_price <= tp1)
            )
            if tp1_hit and not partial_done:
                decisions.append(
                    {
                        "trade": trade,
                        "action": "partial",
                        "pct": self.config.partial_exit_pct,
                        "reason": f"TP1_PARTIAL ({pnl_pct:+.1f}%)",
                        "pnl_pct": pnl_pct,
                    }
                )
                continue

            # 3. Trailing stop after partial exit
            if partial_done and pnl_pct > 0:
                highest = trade.get("highest_price", entry)
                if direction == "long" and current_price > highest:
                    trade["highest_price"] = current_price
                    highest = current_price
                if direction == "short":
                    lowest = trade.get("lowest_price", entry)
                    if current_price < lowest:
                        trade["lowest_price"] = current_price
                        lowest = current_price
                    drawup = (current_price - lowest) / lowest * 100
                    if drawup >= self.config.trailing_stop_pct:
                        decisions.append(
                            {
                                "trade": trade,
                                "action": "close",
                                "pct": 100,
                                "reason": f"TRAILING_STOP ({pnl_pct:+.1f}%)",
                                "pnl_pct": pnl_pct,
                            }
                        )
                        continue
                else:
                    drawdown = (highest - current_price) / highest * 100
                    if drawdown >= self.config.trailing_stop_pct:
                        decisions.append(
                            {
                                "trade": trade,
                                "action": "close",
                                "pct": 100,
                                "reason": f"TRAILING_STOP ({pnl_pct:+.1f}%)",
                                "pnl_pct": pnl_pct,
                            }
                        )
                        continue

            # 4. Time exit — held too long with insufficient profit
            entry_time_str = trade.get("entry_time", "")
            if entry_time_str:
                try:
                    entry_time = datetime.fromisoformat(entry_time_str)
                    held_hours = (datetime.now() - entry_time).total_seconds() / 3600
                    if (
                        held_hours >= self.config.max_hold_hours
                        and pnl_pct < 0.3
                    ):
                        decisions.append(
                            {
                                "trade": trade,
                                "action": "close",
                                "pct": 100,
                                "reason": f"TIME_EXIT (held {held_hours:.0f}h, {pnl_pct:+.1f}%)",
                                "pnl_pct": pnl_pct,
                            }
                        )
                except ValueError:
                    pass

        return decisions
