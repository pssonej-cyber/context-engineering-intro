#!/usr/bin/env python3
"""
================================================================
  BACKTEST v8 — 4H Structure + Tighter Zone Detection
================================================================
  Changes from v7:
    1. RETEST_PROXIMITY 0.5% → 1.0% — easier zone touch
    2. LOOKBACK 50 → 30 — more recent zones only
    3. 1H structure for entry timing (same as v7)

  Usage:
    python3 backtest_v8.py 7
    python3 backtest_v8.py 15
    python3 backtest_v8.py 30
    python3 backtest_v8.py 45
    python3 backtest_v8.py 90
"""

import sys, os
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from btc_strategy_v7_zone import (
    get_ohlcv, find_swings, analyze_structure, detect_bos,
    find_zones, is_price_at_zone, is_rejection_candle, is_engulfing,
    TP_RR_RATIO, TP2_RR_RATIO, PARTIAL_EXIT_PCT, TRAILING_STOP_PCT,
    RISK_PER_TRADE_PCT, MAX_OPEN_TRADES, MIN_POSITION_THB,
)

# ── v8 tuned parameters ──────────────────────────────
LOOKBACK     = 30     # zone lookback — was 50, now 30 (more recent zones)
RETEST_PCT   = 1.0    # was 0.5 — easier zone touch
FEE_RATE     = 0.25
INITIAL_BALANCE = 10000


def map_1h_to_4h(candles_1h, end_idx):
    """Group 1h candles into 4h candles up to end_idx"""
    subset = candles_1h[:end_idx+1]
    if len(subset) < 20:
        return []
    candles_4h = []
    for i in range(0, len(subset) - 3, 4):
        group = subset[i:i+4]
        if len(group) < 4:
            continue
        candles_4h.append({
            "time":  group[0]["time"],
            "open":  group[0]["open"],
            "high":  max(c["high"] for c in group),
            "low":   min(c["low"]  for c in group),
            "close": group[-1]["close"],
            "volume": sum(c.get("volume", 0) for c in group),
        })
    return candles_4h


def check_entry_at_idx(candles_1h, idx):
    """v8: 4H structure + 1H zones + direction-aware rejection entry"""
    if idx < 30:
        return None

    subset_1h = candles_1h[:idx+1]
    subset_4h = map_1h_to_4h(candles_1h, idx)

    if len(subset_1h) < 30 or len(subset_4h) < 10:
        return None

    # ── 4H structure ──
    swings_4h = find_swings(subset_4h, left=3, right=3)
    structure = analyze_structure(swings_4h)

    if structure["trend"] == "undefined":
        return None

    direction = structure["trend"]   # "uptrend" or "downtrend"

    # ── 1H zones ──
    swings_1h = find_swings(subset_1h, left=2, right=2)
    zones = find_zones(subset_1h, swings_1h, lookback=LOOKBACK)

    if not zones:
        return None

    zone_type = "demand" if direction == "uptrend" else "supply"
    matching_zones = [z for z in zones if z["type"] == zone_type]
    if not matching_zones:
        return None

    current_price = subset_1h[-1]["close"]

    # ── At zone? (v8: easier with 1.0%) ──
    touched = None
    for z in matching_zones:
        buf = current_price * RETEST_PCT / 100
        if (z["low"] - buf) <= current_price <= (z["high"] + buf):
            touched = z
            break

    if not touched:
        return None

    # ── Rejection candle ──
    rej_dir = "bullish" if direction == "uptrend" else "bearish"
    last_c  = subset_1h[-2]
    prev_c  = subset_1h[-3]

    rej_ok, rej_type = is_rejection_candle(last_c, rej_dir)
    eng_ok, eng_type = is_engulfing(prev_c, last_c, rej_dir)

    if not rej_ok and not eng_ok:
        return None

    pattern = rej_type if rej_ok else eng_type

    return {
        "direction": direction,
        "pattern":   pattern,
        "zone":      touched,
        "price":    current_price,
    }


def backtest(days=45):
    print("=" * 64)
    print(f"  📊 BACKTEST v8 — Tighter Zone Detection")
    print(f"  Period: {days} days")
    print("=" * 64)

    candles = get_ohlcv("60", min(days * 24, 5000))
    if len(candles) < 100:
        print("❌ Not enough data"); return

    print(f"  Candles loaded: {len(candles)}")
    print(f"  Pattern: 4H structure + 1H zone + rejection")
    print(f"  Retest proximity: {RETEST_PCT}%")
    print(f"  Zone lookback: {LOOKBACK}")
    print(f"  TP: 2R (50% exit) / 4R (trail)")
    print(f"  Fee: {FEE_RATE}%")
    print(f"  Long-only (Bitkub spot)")
    print("=" * 64)

    balance    = INITIAL_BALANCE
    open_pos   = []
    all_trades = []
    peak       = INITIAL_BALANCE
    max_dd     = 0
    total_fees = 0
    equity_curve = []

    no_structure = wrong_trend = no_zones = no_retest = no_rejection = 0
    signals_taken = 0

    for i in range(60, len(candles)):
        price = candles[i]["close"]
        high  = candles[i]["high"]
        low   = candles[i]["low"]

        # ── Exits ──
        for pos in open_pos[:]:
            if pos["direction"] == "buy":
                if low <= pos["sl"]:
                    exit_price = pos["sl"]
                    fee = (pos["amt_btc"] * exit_price) * FEE_RATE / 100
                    pnl = pos["amt_btc"] * (exit_price - pos["entry"]) - fee
                    balance += pos["amt_thb"] + pnl; total_fees += fee
                    all_trades.append({"pnl": pnl, "pnl_pct": (exit_price-pos["entry"])/pos["entry"]*100,
                                        "type": "SL", "entry": pos["entry"], "exit": exit_price,
                                        "pattern": pos["pattern"], "hold": i - pos["idx"], "direction": "buy"})
                    open_pos.remove(pos); continue

                if high >= pos["tp2"] and pos.get("partial_exited"):
                    exit_price = pos["tp2"]
                    fee = (pos["amt_btc"] * exit_price) * FEE_RATE / 100
                    pnl = pos["amt_btc"] * (exit_price - pos["entry"]) - fee
                    balance += pos["amt_thb"] + pnl; total_fees += fee
                    all_trades.append({"pnl": pnl, "pnl_pct": (exit_price-pos["entry"])/pos["entry"]*100,
                                        "type": "TP2", "entry": pos["entry"], "exit": exit_price,
                                        "pattern": pos["pattern"], "hold": i - pos["idx"], "direction": "buy"})
                    open_pos.remove(pos); continue

                if high >= pos["tp1"] and not pos.get("partial_exited"):
                    exit_price = pos["tp1"]
                    exit_btc  = pos["amt_btc"] * (PARTIAL_EXIT_PCT / 100)
                    fee = (exit_btc * exit_price) * FEE_RATE / 100
                    pnl = exit_btc * (exit_price - pos["entry"]) - fee
                    balance += (pos["amt_thb"] * PARTIAL_EXIT_PCT / 100) + pnl
                    total_fees += fee
                    pos["amt_btc"] -= exit_btc
                    pos["amt_thb"] *= (1 - PARTIAL_EXIT_PCT / 100)
                    pos["sl"] = pos["entry"]
                    pos["partial_exited"] = True
                    pos["highest"] = high
                    all_trades.append({"pnl": pnl, "pnl_pct": (exit_price-pos["entry"])/pos["entry"]*100,
                                        "type": "TP1", "entry": pos["entry"], "exit": exit_price,
                                        "pattern": pos["pattern"], "hold": i - pos["idx"], "direction": "buy"}); continue

                if pos.get("partial_exited"):
                    if high > pos.get("highest", pos["entry"]): pos["highest"] = high
                    dd = (pos["highest"] - low) / pos["highest"] * 100
                    if dd >= TRAILING_STOP_PCT:
                        exit_price = pos["highest"] * (1 - TRAILING_STOP_PCT / 100)
                        fee = (pos["amt_btc"] * exit_price) * FEE_RATE / 100
                        pnl = pos["amt_btc"] * (exit_price - pos["entry"]) - fee
                        balance += pos["amt_thb"] + pnl; total_fees += fee
                        all_trades.append({"pnl": pnl, "pnl_pct": (exit_price-pos["entry"])/pos["entry"]*100,
                                            "type": "TRAIL", "entry": pos["entry"], "exit": exit_price,
                                            "pattern": pos["pattern"], "hold": i - pos["idx"], "direction": "buy"})
                        open_pos.remove(pos)

        # ── Equity ──
        open_value = sum(p["amt_btc"] * price for p in open_pos)
        total_val  = balance + open_value
        equity_curve.append(total_val)
        if total_val > peak: peak = total_val
        dd = (peak - total_val) / peak * 100
        if dd > max_dd: max_dd = dd

        # ── Entry ──
        if len(open_pos) >= MAX_OPEN_TRADES:
            continue

        # Filter counters
        subset_4h_chk = map_1h_to_4h(candles, i)
        if len(subset_4h_chk) < 10:
            no_structure += 1; continue
        swings_chk = find_swings(subset_4h_chk, left=3, right=3)
        struct_chk = analyze_structure(swings_chk)
        if struct_chk["trend"] == "undefined":
            no_structure += 1; continue
        if struct_chk["trend"] != "uptrend":
            wrong_trend += 1; continue

        zone_swings_chk = find_swings(candles[:i+1], left=2, right=2)
        zones_chk = find_zones(candles[:i+1], zone_swings_chk, lookback=LOOKBACK)
        demand_chk = [z for z in zones_chk if z["type"] == "demand"]
        if not demand_chk:
            no_zones += 1; continue

        buf = price * RETEST_PCT / 100
        touched = any((z["low"]-buf) <= price <= (z["high"]+buf) for z in demand_chk)
        if not touched:
            no_retest += 1; continue

        signal = check_entry_at_idx(candles, i)
        if signal is None:
            no_rejection += 1; continue

        signals_taken += 1

        entry  = price
        zone   = signal["zone"]
        sl     = zone["low"] * 0.998
        risk   = entry - sl
        if risk <= 0: continue
        tp1    = entry + risk * TP_RR_RATIO
        tp2    = entry + risk * TP2_RR_RATIO

        risk_amount   = balance * (RISK_PER_TRADE_PCT / 100)
        stop_dist_pct = risk / entry
        pos_thb  = risk_amount / stop_dist_pct if stop_dist_pct > 0 else 0
        pos_thb  = min(pos_thb, balance * 0.3)
        if pos_thb < MIN_POSITION_THB or pos_thb > balance:
            continue

        fee = pos_thb * FEE_RATE / 100
        total_fees += fee
        balance    -= pos_thb

        open_pos.append({
            "direction": "buy",
            "entry":     entry,
            "amt_thb":   pos_thb - fee,
            "amt_btc":   (pos_thb - fee) / entry,
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       tp2,
            "idx":       i,
            "pattern":   signal["pattern"],
            "highest":   entry,
        })

    # ── Close remaining ──
    final_price = candles[-1]["close"]
    for pos in open_pos:
        fee = (pos["amt_btc"] * final_price) * FEE_RATE / 100
        pnl = pos["amt_btc"] * (final_price - pos["entry"]) - fee
        balance += pos["amt_thb"] + pnl; total_fees += fee
        all_trades.append({"pnl": pnl, "pnl_pct": (final_price-pos["entry"])/pos["entry"]*100,
                            "type": "CLOSE", "entry": pos["entry"], "exit": final_price,
                            "pattern": pos["pattern"], "hold": len(candles)-1-pos["idx"], "direction": "buy"})

    # ── Results ──
    wins   = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    total  = len(all_trades)
    total_pnl  = sum(t["pnl"] for t in all_trades)
    win_rate   = len(wins) / total * 100 if total > 0 else 0
    avg_win    = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss   = np.mean([t["pnl"] for t in losses]) if losses else 0
    rr_actual  = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    bnh        = (candles[-1]["close"] / candles[60]["close"] - 1) * INITIAL_BALANCE
    gross_win  = sum(t["pnl"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 1
    pf         = gross_win / gross_loss if gross_loss > 0 else 0
    avg_hold   = np.mean([t.get("hold", 0) for t in all_trades]) if all_trades else 0

    by_type = {}
    for t in all_trades:
        by_type.setdefault(t["type"], {"count": 0, "pnl": 0, "wins": 0})
        by_type[t["type"]]["count"] += 1
        by_type[t["type"]]["pnl"]   += t["pnl"]
        if t["pnl"] > 0: by_type[t["type"]]["wins"] += 1

    by_pattern = {}
    for t in all_trades:
        by_pattern.setdefault(t["pattern"], {"count": 0, "pnl": 0, "wins": 0})
        by_pattern[t["pattern"]]["count"] += 1
        by_pattern[t["pattern"]]["pnl"]   += t["pnl"]
        if t["pnl"] > 0: by_pattern[t["pattern"]]["wins"] += 1

    print(f"\n{'='*64}")
    print(f"  📊 BACKTEST v8 RESULTS — {days} days")
    print(f"{'='*64}")
    print(f"  Final balance:  {balance:,.0f} THB")
    print(f"  P&L:            {total_pnl:,.1f} THB ({total_pnl/INITIAL_BALANCE*100:+.2f}%)")
    print(f"  Total fees:     {total_fees:,.1f} THB")
    print(f"  Max drawdown:  {max_dd:.1f}%")
    print(f"  Profit factor:  {pf:.2f}")
    print(f"  Buy & hold:    {bnh:,.0f} THB ({bnh/INITIAL_BALANCE*100:+.2f}%)")
    print(f"  vs B&H:        {total_pnl - bnh:+,.0f} THB")

    print(f"\n  📈 TRADE STATS")
    print(f"  Total trades:  {total}")
    print(f"  Trades/month:  {total / max(days/30, 1):.1f}")
    print(f"  Wins:          {len(wins)} ({win_rate:.0f}%)")
    print(f"  Losses:        {len(losses)}")
    print(f"  Avg win:       {avg_win:,.1f} THB")
    print(f"  Avg loss:      {avg_loss:,.1f} THB")
    print(f"  Actual R:R:   {rr_actual:.2f}")
    print(f"  Avg hold:      {avg_hold:.0f}h")
    if all_trades:
        print(f"  Best trade:   {max(t['pnl'] for t in all_trades):+,.1f} THB")
        print(f"  Worst trade: {min(t['pnl'] for t in all_trades):+,.1f} THB")

    print(f"\n  🔍 FILTER FUNNEL")
    print(f"  No 4H structure:  {no_structure}")
    print(f"  Wrong trend:    {wrong_trend}  (downtrend skipped)")
    print(f"  No demand zones: {no_zones}")
    print(f"  No retest:      {no_retest}")
    print(f"  No rejection:   {no_rejection}")
    print(f"  Signals taken:  {signals_taken}")

    if by_type:
        print(f"\n  📋 BY EXIT TYPE")
        for typ in ["TP1", "TP2", "TRAIL", "SL", "CLOSE"]:
            if typ in by_type:
                d = by_type[typ]
                wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
                print(f"  {typ:7s}  {d['count']:3d}  Win:{wr:3.0f}%  P&L: {d['pnl']:+,.1f}")

    if by_pattern:
        print(f"\n  🎯 BY PATTERN")
        for p, d in sorted(by_pattern.items(), key=lambda x: -x[1]["pnl"]):
            wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
            print(f"  {p:22s}  {d['count']:3d}  Win:{wr:3.0f}%  P&L: {d['pnl']:+,.1f}")

    if equity_curve:
        ec = np.array(equity_curve)
        print(f"\n  📉 EQUITY CURVE")
        print(f"  Start:  {ec[0]:,.0f}")
        print(f"  Low:    {ec.min():,.0f} ({(ec.min()/INITIAL_BALANCE-1)*100:+.2f}%)")
        print(f"  High:   {ec.max():,.0f} ({(ec.max()/INITIAL_BALANCE-1)*100:+.2f}%)")
        print(f"  End:    {ec[-1]:,.0f} ({(ec[-1]/INITIAL_BALANCE-1)*100:+.2f}%)")

    print(f"\n{'='*64}")
    if total == 0:
        print(f"  ⚪ NO TRADES")
    elif total_pnl > 0 and win_rate >= 50:
        print(f"  ✅ PROFITABLE + WIN RATE GOOD")
    elif total_pnl > 0:
        print(f"  🟡 PROFITABLE — PA relies on big R:R")
    elif total_pnl > bnh:
        print(f"  🟡 BEATS BUY & HOLD")
    else:
        print(f"  🔴 NEEDS TUNING")
    print(f"{'='*64}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    backtest(days)
