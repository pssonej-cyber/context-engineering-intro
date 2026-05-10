"""Backtest engine — replay historical 1H candles through the live strategy code.

Mirrors examples/backtest_v8.py but uses the modular strategy functions so
parameter changes flow through to both live and backtest with no code drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from btc_bot.api.bitkub import BitkubClient
from btc_bot.config import BotConfig
from btc_bot.strategy.signals import is_engulfing, is_rejection_candle
from btc_bot.strategy.structure import analyze_structure, find_swings
from btc_bot.strategy.zones import find_zones, is_price_at_zone

FEE_RATE_PCT = 0.25
INITIAL_BALANCE_THB = 10_000


@dataclass
class BacktestResult:
    days: int
    total_trades: int
    win_rate: float
    total_pnl_thb: float
    total_pnl_pct: float
    max_drawdown_pct: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    actual_rr: float
    final_balance: float
    trades: list[dict[str, Any]]
    by_exit_type: dict[str, dict[str, Any]]


def _map_1h_to_4h(candles_1h: list[dict[str, Any]], end_idx: int) -> list[dict[str, Any]]:
    """Aggregate 1H candles into 4H candles up to end_idx (inclusive)."""
    subset = candles_1h[: end_idx + 1]
    if len(subset) < 20:
        return []
    h4: list[dict[str, Any]] = []
    for i in range(0, len(subset) - 3, 4):
        group = subset[i : i + 4]
        if len(group) < 4:
            continue
        h4.append(
            {
                "time": group[0]["time"],
                "open": group[0]["open"],
                "high": max(c["high"] for c in group),
                "low": min(c["low"] for c in group),
                "close": group[-1]["close"],
                "volume": sum(c.get("volume", 0) for c in group),
            }
        )
    return h4


def _check_entry_at_idx(
    candles_1h: list[dict[str, Any]],
    idx: int,
    config: BotConfig,
) -> dict[str, Any] | None:
    """Apply the One Pattern at a specific 1H candle index. Long-only (Bitkub spot)."""
    if idx < 30:
        return None

    subset_1h = candles_1h[: idx + 1]
    subset_4h = _map_1h_to_4h(candles_1h, idx)
    if len(subset_1h) < 30 or len(subset_4h) < 10:
        return None

    swings_4h = find_swings(
        subset_4h,
        left=config.swing_lookback_left,
        right=config.swing_lookback_right,
    )
    structure = analyze_structure(swings_4h)
    if structure["trend"] != "uptrend":
        return None

    zones = find_zones(subset_1h, config)
    demand = [z for z in zones if z["type"] == "demand"]
    if not demand:
        return None

    current_price = subset_1h[-1]["close"]
    touched = next(
        (z for z in demand if is_price_at_zone(current_price, z, config.retest_proximity_pct)),
        None,
    )
    if touched is None:
        return None

    if len(subset_1h) < 3:
        return None
    last_c, prev_c = subset_1h[-2], subset_1h[-3]
    rej_ok, rej_type = is_rejection_candle(last_c, "bullish", config)
    eng_ok, eng_type = is_engulfing(prev_c, last_c, "bullish")
    if not rej_ok and not eng_ok:
        return None

    return {
        "direction": "buy",
        "pattern": rej_type if rej_ok else eng_type,
        "zone": touched,
        "price": current_price,
    }


def run_backtest(days: int, config: BotConfig) -> BacktestResult:
    """Download recent Bitkub data and replay the strategy."""
    print("=" * 64)
    print(f"  📊 BTC Bot — Backtest ({days} days)")
    print("=" * 64)

    api = BitkubClient(config)
    candles = api.get_ohlcv("60", min(days * 24, 5000))
    if len(candles) < 100:
        print(f"❌ Not enough data ({len(candles)} candles)")
        return BacktestResult(
            days=days, total_trades=0, win_rate=0, total_pnl_thb=0,
            total_pnl_pct=0, max_drawdown_pct=0, profit_factor=0,
            avg_win=0, avg_loss=0, actual_rr=0,
            final_balance=INITIAL_BALANCE_THB, trades=[], by_exit_type={},
        )

    print(f"  Candles loaded: {len(candles)}")
    print(f"  Retest: {config.retest_proximity_pct}%  |  Lookback: {config.zone_lookback}")
    print(f"  TP1/TP2: {config.tp1_rr_ratio}R / {config.tp2_rr_ratio}R  |  Trail: {config.trailing_stop_pct}%")
    print(f"  Risk/trade: {config.risk_per_trade_pct}%  |  Max trades: {config.max_open_trades}")
    print("=" * 64)

    balance = float(INITIAL_BALANCE_THB)
    open_pos: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    peak = INITIAL_BALANCE_THB
    max_dd = 0.0
    total_fees = 0.0

    for i in range(60, len(candles)):
        price = candles[i]["close"]
        high = candles[i]["high"]
        low = candles[i]["low"]

        # ── Process exits on open positions ────────────────────────
        for pos in open_pos[:]:
            entry = pos["entry"]

            # Stop loss
            if low <= pos["sl"]:
                fee = (pos["amt_btc"] * pos["sl"]) * FEE_RATE_PCT / 100
                pnl = pos["amt_btc"] * (pos["sl"] - entry) - fee
                balance += pos["amt_thb"] + pnl
                total_fees += fee
                all_trades.append(
                    {
                        "pnl": pnl,
                        "pnl_pct": (pos["sl"] - entry) / entry * 100,
                        "type": "SL",
                        "entry": entry, "exit": pos["sl"],
                        "pattern": pos["pattern"], "hold": i - pos["idx"],
                    }
                )
                open_pos.remove(pos)
                continue

            # TP2 (after partial done)
            if high >= pos["tp2"] and pos.get("partial_exited"):
                fee = (pos["amt_btc"] * pos["tp2"]) * FEE_RATE_PCT / 100
                pnl = pos["amt_btc"] * (pos["tp2"] - entry) - fee
                balance += pos["amt_thb"] + pnl
                total_fees += fee
                all_trades.append(
                    {
                        "pnl": pnl,
                        "pnl_pct": (pos["tp2"] - entry) / entry * 100,
                        "type": "TP2",
                        "entry": entry, "exit": pos["tp2"],
                        "pattern": pos["pattern"], "hold": i - pos["idx"],
                    }
                )
                open_pos.remove(pos)
                continue

            # TP1 partial
            if high >= pos["tp1"] and not pos.get("partial_exited"):
                exit_btc = pos["amt_btc"] * (config.partial_exit_pct / 100)
                fee = (exit_btc * pos["tp1"]) * FEE_RATE_PCT / 100
                pnl = exit_btc * (pos["tp1"] - entry) - fee
                balance += (pos["amt_thb"] * config.partial_exit_pct / 100) + pnl
                total_fees += fee
                pos["amt_btc"] -= exit_btc
                pos["amt_thb"] *= 1 - config.partial_exit_pct / 100
                pos["sl"] = entry  # breakeven
                pos["partial_exited"] = True
                pos["highest"] = high
                all_trades.append(
                    {
                        "pnl": pnl,
                        "pnl_pct": (pos["tp1"] - entry) / entry * 100,
                        "type": "TP1",
                        "entry": entry, "exit": pos["tp1"],
                        "pattern": pos["pattern"], "hold": i - pos["idx"],
                    }
                )
                continue

            # Trailing stop after partial
            if pos.get("partial_exited"):
                if high > pos.get("highest", entry):
                    pos["highest"] = high
                drawdown = (pos["highest"] - low) / pos["highest"] * 100
                if drawdown >= config.trailing_stop_pct:
                    exit_price = pos["highest"] * (1 - config.trailing_stop_pct / 100)
                    fee = (pos["amt_btc"] * exit_price) * FEE_RATE_PCT / 100
                    pnl = pos["amt_btc"] * (exit_price - entry) - fee
                    balance += pos["amt_thb"] + pnl
                    total_fees += fee
                    all_trades.append(
                        {
                            "pnl": pnl,
                            "pnl_pct": (exit_price - entry) / entry * 100,
                            "type": "TRAIL",
                            "entry": entry, "exit": exit_price,
                            "pattern": pos["pattern"], "hold": i - pos["idx"],
                        }
                    )
                    open_pos.remove(pos)

        # ── Equity tracking ─────────────────────────────────────────
        open_value = sum(p["amt_btc"] * price for p in open_pos)
        total_val = balance + open_value
        if total_val > peak:
            peak = total_val
        dd = (peak - total_val) / peak * 100
        if dd > max_dd:
            max_dd = dd

        # ── Entry check ─────────────────────────────────────────────
        if len(open_pos) >= config.max_open_trades:
            continue

        signal = _check_entry_at_idx(candles, i, config)
        if signal is None:
            continue

        entry = price
        sl = signal["zone"]["low"] * 0.998
        risk = entry - sl
        if risk <= 0:
            continue

        tp1 = entry + risk * config.tp1_rr_ratio
        tp2 = entry + risk * config.tp2_rr_ratio

        risk_amt = balance * (config.risk_per_trade_pct / 100)
        stop_dist_pct = risk / entry
        pos_thb = risk_amt / stop_dist_pct if stop_dist_pct > 0 else 0
        pos_thb = min(pos_thb, balance * 0.3)
        if pos_thb < config.min_position_thb or pos_thb > balance:
            continue

        fee = pos_thb * FEE_RATE_PCT / 100
        total_fees += fee
        balance -= pos_thb

        open_pos.append(
            {
                "entry": entry,
                "amt_thb": pos_thb - fee,
                "amt_btc": (pos_thb - fee) / entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "idx": i,
                "pattern": signal["pattern"],
                "highest": entry,
            }
        )

    # ── Close remaining at final price ──────────────────────────────
    final_price = candles[-1]["close"]
    for pos in open_pos:
        fee = (pos["amt_btc"] * final_price) * FEE_RATE_PCT / 100
        pnl = pos["amt_btc"] * (final_price - pos["entry"]) - fee
        balance += pos["amt_thb"] + pnl
        total_fees += fee
        all_trades.append(
            {
                "pnl": pnl,
                "pnl_pct": (final_price - pos["entry"]) / pos["entry"] * 100,
                "type": "CLOSE",
                "entry": pos["entry"], "exit": final_price,
                "pattern": pos["pattern"], "hold": len(candles) - 1 - pos["idx"],
            }
        )

    # ── Stats ───────────────────────────────────────────────────────
    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    total = len(all_trades)
    total_pnl = sum(t["pnl"] for t in all_trades)
    win_rate = len(wins) / total * 100 if total else 0
    avg_win = float(np.mean([t["pnl"] for t in wins])) if wins else 0
    avg_loss = float(np.mean([t["pnl"] for t in losses])) if losses else 0
    rr_actual = abs(avg_win / avg_loss) if avg_loss else 0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses)) or 1
    pf = gross_win / gross_loss

    by_exit: dict[str, dict[str, Any]] = {}
    for t in all_trades:
        d = by_exit.setdefault(t["type"], {"count": 0, "pnl": 0.0, "wins": 0})
        d["count"] += 1
        d["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            d["wins"] += 1

    print(f"\n  Final balance:  {balance:,.0f} THB")
    print(f"  P&L:            {total_pnl:+,.0f} THB ({total_pnl/INITIAL_BALANCE_THB*100:+.1f}%)")
    print(f"  Total fees:     {total_fees:,.0f} THB")
    print(f"  Max drawdown:  {max_dd:.1f}%")
    print(f"  Profit factor:  {pf:.2f}")
    print(f"\n  Trades:         {total}  (Wins: {len(wins)}, Losses: {len(losses)})")
    print(f"  Win rate:       {win_rate:.0f}%")
    print(f"  Avg win:        {avg_win:+,.0f} THB")
    print(f"  Avg loss:       {avg_loss:+,.0f} THB")
    print(f"  Actual R:R:    {rr_actual:.2f}")
    if by_exit:
        print("\n  By exit type:")
        for typ in ("TP1", "TP2", "TRAIL", "SL", "CLOSE"):
            if typ in by_exit:
                d = by_exit[typ]
                wr = d["wins"] / d["count"] * 100 if d["count"] else 0
                print(f"    {typ:7s}  {d['count']:3d}  WR={wr:3.0f}%  P&L: {d['pnl']:+,.0f}")
    print("=" * 64)

    return BacktestResult(
        days=days,
        total_trades=total,
        win_rate=win_rate,
        total_pnl_thb=total_pnl,
        total_pnl_pct=total_pnl / INITIAL_BALANCE_THB * 100,
        max_drawdown_pct=max_dd,
        profit_factor=pf,
        avg_win=avg_win,
        avg_loss=avg_loss,
        actual_rr=rr_actual,
        final_balance=balance,
        trades=all_trades,
        by_exit_type=by_exit,
    )
