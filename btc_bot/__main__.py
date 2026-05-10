"""CLI entry point: `python -m btc_bot <command>`.

Commands:
  dry-run    Run one analysis cycle without placing orders
  live       Run one analysis cycle and place real orders (requires API keys)
  status     Show open positions and recent PnL
  log        Show last 20 log entries
  backtest   Run historical backtest (--days N, default 60)
  reset      Wipe state file
"""

from __future__ import annotations

import argparse
import json
import sys

from btc_bot.config import BotConfig


def _cmd_run(args: argparse.Namespace, config: BotConfig) -> int:
    from btc_bot.bot import Bot

    dry = args.command == "dry-run"
    if not dry:
        if not config.bitkub_api_key or not config.bitkub_api_secret:
            print("❌ BITKUB_API_KEY and BITKUB_API_SECRET required for live mode")
            return 1
        print("⚠️  LIVE MODE — 5s to cancel...")
        try:
            import time
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 0

    Bot(config).run(dry_run=dry)
    return 0


def _cmd_status(args: argparse.Namespace, config: BotConfig) -> int:
    from btc_bot.api.bitkub import BitkubClient
    from btc_bot.state import load_state

    state = load_state(config)
    price = BitkubClient(config).get_price()
    if price:
        print(f"BTC/THB: {price['last']:,.2f}  ({price['change']:+.2f}%)")
    print(f"\nOpen trades: {len(state['open_trades'])}")
    for i, t in enumerate(state["open_trades"]):
        pnl = (
            (price["last"] - t["entry_price"]) / t["entry_price"] * 100
            if price else 0
        )
        print(f"  #{i+1} {t['direction']} entry={t['entry_price']:,.0f} "
              f"SL={t['stop_loss']:,.0f} ({pnl:+.1f}%)")
    print(
        f"\nClosed: {state['total_trades']} | "
        f"Wins: {state['winning_trades']} "
        f"({(state['winning_trades']/max(1, state['total_trades'])*100):.0f}%)"
    )
    print(f"PnL today: {state['daily_pnl']:+,.0f} | Total: {state['total_pnl']:+,.0f} THB")
    return 0


def _cmd_log(args: argparse.Namespace, config: BotConfig) -> int:
    log_path = config.log_path()
    if not log_path.exists():
        print("(no log entries)")
        return 0
    with log_path.open() as f:
        logs = json.load(f)
    for e in logs[-20:]:
        rest = {k: v for k, v in e.items() if k not in ("time", "action")}
        print(f"  {e['time'][:19]}  {e['action']:14s}  {json.dumps(rest)}")
    return 0


def _cmd_backtest(args: argparse.Namespace, config: BotConfig) -> int:
    from btc_bot.backtest import run_backtest

    run_backtest(days=args.days, config=config)
    return 0


def _cmd_reset(args: argparse.Namespace, config: BotConfig) -> int:
    for path in (config.state_path(), config.log_path()):
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    print("✅ Reset done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="btc_bot", description="BTC Trading Bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("dry-run", help="Run analysis without placing orders")
    sub.add_parser("live", help="Run analysis and place real orders")
    sub.add_parser("status", help="Show open positions and PnL")
    sub.add_parser("log", help="Show recent trade log")
    bt = sub.add_parser("backtest", help="Run historical backtest")
    bt.add_argument("--days", type=int, default=60, help="Number of days to backtest")
    sub.add_parser("reset", help="Wipe state file")

    args = parser.parse_args(argv)
    config = BotConfig()  # type: ignore[call-arg]  # pydantic-settings reads from .env

    handlers = {
        "dry-run": _cmd_run,
        "live": _cmd_run,
        "status": _cmd_status,
        "log": _cmd_log,
        "backtest": _cmd_backtest,
        "reset": _cmd_reset,
    }
    return handlers[args.command](args, config)


if __name__ == "__main__":
    sys.exit(main())
