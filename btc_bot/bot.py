"""Main orchestration loop. Run() is invoked once per systemd timer firing.

Flow:
  1. Fetch price + 4H/1H OHLCV
  2. Generate signals (THE ONE PATTERN)
  3. Process exits on existing trades
  4. Check risk gates → optionally enter new trade
  5. Persist state
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from btc_bot.api.bitkub import BitkubClient
from btc_bot.config import BotConfig
from btc_bot.risk.manager import RiskManager
from btc_bot.state import append_log, load_state, save_state
from btc_bot.strategy.signals import generate_signals


class Bot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.api = BitkubClient(config)
        self.risk = RiskManager(config)

    def run(self, dry_run: bool = True) -> dict[str, Any]:
        """Execute one analysis cycle. Returns a summary dict for logging/tests."""
        print("=" * 64)
        mode = "DRY-RUN" if dry_run else "LIVE"
        print(f"  📊 BTC Bot — {mode} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 64)

        state = load_state(self.config)

        # ── Market data ─────────────────────────────────────────────
        price_data = self.api.get_price()
        if not price_data:
            print("❌ No price data — aborting cycle")
            return {"status": "no_data"}

        candles_1h = self.api.get_ohlcv("60", 200)
        candles_4h = self.api.get_ohlcv("240", 80)

        if len(candles_1h) < 50 or len(candles_4h) < 20:
            print(
                f"❌ Insufficient candles "
                f"(1h={len(candles_1h)}, 4h={len(candles_4h)})"
            )
            return {"status": "insufficient_data"}

        current = price_data["last"]
        print(f"\n📊 BTC/THB: {current:,.2f}  ({price_data['change']:+.2f}%)")

        # ── Generate signals ────────────────────────────────────────
        signals = generate_signals(candles_1h, candles_4h, self.config)

        # ── Process exits BEFORE checking gates for new entries ────
        balances = (
            self.api.get_balances()
            if not dry_run and self.config.bitkub_api_key
            else {"thb": 100000.0, "btc": 0.0}
        )
        total_capital = balances["thb"] + balances["btc"] * current

        exit_decisions = self.risk.check_exits(current, state["open_trades"])
        for decision in exit_decisions:
            self._execute_exit(decision, current, state, dry_run)

        # ── Risk gates ──────────────────────────────────────────────
        can_trade, issues = self.risk.can_trade(state, total_capital)
        print(
            f"\n🛡️ Capital: {total_capital:,.0f} THB | "
            f"Open: {len(state['open_trades'])}/{self.config.max_open_trades}"
        )
        for issue in issues:
            print(f"   ⚠️ {issue}")

        # ── Print signals ──────────────────────────────────────────
        if signals:
            sig = signals[0]
            print(
                f"\n🎯 SIGNAL: {sig.direction.upper()} @ {sig.entry:,.0f}  "
                f"SL={sig.stop_loss:,.0f}  TP1={sig.tp1:,.0f}  "
                f"({sig.pattern})"
            )
        else:
            print("\n⚪ No signal — waiting for setup")

        # ── Entry execution ────────────────────────────────────────
        entered = False
        if signals and can_trade:
            sig = signals[0]
            # Bitkub spot is long-only — short signals are recorded but skipped
            if sig.direction == "short":
                print("   ⏭️  Short signal skipped (spot market = long-only)")
            else:
                pos_thb = self.risk.position_size(
                    total_capital, sig.entry, sig.stop_loss
                )
                if pos_thb < self.config.min_position_thb:
                    print(f"   ⚠️ Position too small ({pos_thb:.0f} THB) — skipping")
                else:
                    print(f"   🟢 Opening LONG: {pos_thb:,.0f} THB")
                    entered = self._execute_entry(sig, pos_thb, state, dry_run)

        save_state(self.config, state)
        self._print_summary(state, current)

        return {
            "status": "ok",
            "current_price": current,
            "signals_count": len(signals),
            "entered": entered,
            "exits_count": len(exit_decisions),
            "open_trades": len(state["open_trades"]),
            "blocked_by": issues,
        }

    # ─── Entry execution ────────────────────────────────────────────
    def _execute_entry(
        self,
        sig: Any,
        pos_thb: float,
        state: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        if not dry_run:
            result = self.api.place_buy(pos_thb)
            if result.get("error") != 0:
                print(f"   ❌ Order failed: {result}")
                return False
            entry_price = float(result.get("result", {}).get("rat", sig.entry)) or sig.entry
        else:
            entry_price = sig.entry

        amount_btc = pos_thb / entry_price
        trade = {
            "entry_price": entry_price,
            "entry_time": datetime.now().isoformat(),
            "amount_thb": pos_thb,
            "amount_btc": amount_btc,
            "stop_loss": sig.stop_loss,
            "initial_sl": sig.stop_loss,
            "direction": sig.direction,
            "zone": sig.zone,
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "structure": sig.structure_trend,
            "pattern": sig.pattern,
            "partial_exited": False,
        }
        state["open_trades"].append(trade)
        append_log(
            self.config,
            "BUY",
            {
                "price": entry_price,
                "amount_thb": pos_thb,
                "pattern": sig.pattern,
                "dry_run": dry_run,
            },
        )
        return True

    # ─── Exit execution ─────────────────────────────────────────────
    def _execute_exit(
        self,
        decision: dict[str, Any],
        current_price: float,
        state: dict[str, Any],
        dry_run: bool,
    ) -> None:
        trade = decision["trade"]
        action = decision["action"]
        reason = decision["reason"]
        entry = trade["entry_price"]
        direction = trade["direction"]
        amount_btc = trade["amount_btc"]

        if action == "partial":
            exit_btc = amount_btc * (decision["pct"] / 100)
            pnl = exit_btc * (current_price - entry) if direction == "long" else 0
            print(f"\n  🟡 PARTIAL: {reason} — selling {decision['pct']:.0f}% = {pnl:,.0f} THB")
            if not dry_run:
                self.api.place_sell(exit_btc)
            trade["amount_btc"] -= exit_btc
            trade["amount_thb"] *= 1 - decision["pct"] / 100
            trade["partial_exited"] = True
            trade["stop_loss"] = entry  # Move SL to breakeven
            state["daily_pnl"] += pnl
            state["total_pnl"] += pnl
            append_log(
                self.config,
                "PARTIAL_SELL",
                {"reason": reason, "pnl": pnl, "dry_run": dry_run},
            )
            return

        # Full close
        pnl = amount_btc * (current_price - entry) if direction == "long" else 0
        print(f"\n  🔴 EXIT: {reason} = {pnl:,.0f} THB")
        if not dry_run:
            self.api.place_sell(amount_btc)

        state["daily_pnl"] += pnl
        state["total_pnl"] += pnl
        state["total_trades"] += 1
        if pnl > 0:
            state["winning_trades"] += 1
        state["recent_results"].append(pnl)
        state["recent_results"] = state["recent_results"][-50:]
        if pnl < 0:
            state["last_loss_time"] = datetime.now().isoformat()

        state["closed_trades"].append(
            {
                **trade,
                "exit_price": current_price,
                "exit_time": datetime.now().isoformat(),
                "pnl": pnl,
                "exit_reason": reason,
            }
        )
        state["closed_trades"] = state["closed_trades"][-200:]
        state["open_trades"].remove(trade)
        append_log(
            self.config,
            "SELL",
            {"reason": reason, "pnl": pnl, "dry_run": dry_run},
        )

    # ─── Summary print ──────────────────────────────────────────────
    def _print_summary(self, state: dict[str, Any], current: float) -> None:
        total = state["total_trades"]
        wins = state["winning_trades"]
        wr = wins / total * 100 if total > 0 else 0.0
        print("\n" + "=" * 64)
        print(
            f"📋 Trades: {len(state['open_trades'])} open | {total} closed | "
            f"WR {wr:.0f}%"
        )
        print(
            f"   PnL today: {state['daily_pnl']:+,.0f} THB | "
            f"Total: {state['total_pnl']:+,.0f} THB"
        )
        for i, t in enumerate(state["open_trades"]):
            pnl_pct = (current - t["entry_price"]) / t["entry_price"] * 100
            partial = " (partial)" if t.get("partial_exited") else ""
            print(
                f"   #{i+1} {t['direction']} "
                f"{t['entry_price']:,.0f}→{current:,.0f} "
                f"({pnl_pct:+.1f}%){partial}"
            )
        print("=" * 64)
