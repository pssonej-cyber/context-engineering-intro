#!/usr/bin/env python3
"""
================================================================
  PRICE ACTION TRADING BOT v6 — "One Pattern Only"
  Based on MJ OPO Forex Trading video (Feb 2026)
================================================================

  Core Philosophy: ใช้แค่ 1 ท่าเทรด
  ระบบ: Supply & Demand + Market Structure + Break of Structure

  THE ONE SETUP (auto-detected):
    1. Identify trend via HH/HL (uptrend) or LH/LL (downtrend)
    2. Find supply/demand zone (breaker block) where price broke out
    3. Wait for BoS (Break of Structure) confirmation
    4. Wait for RETEST of the zone
    5. Enter on REJECTION candle (engulfing/strong reversal)
    6. SL on other side of zone
    7. TP at next opposing zone or R:R multiple

  KEY DIFFERENCE from v4.1:
    v4.1 = indicator ensemble (meanrev + macd + trend)
    v6   = pure price action (no indicators)

  Timeframes:
    4H = main structure
    1H = entry timing

  Usage:
    python3 btc_pa_v6.py analyze   — Show current analysis
    python3 btc_pa_v6.py live      — Execute live trades
    python3 btc_pa_v6.py status    — Show open positions with zones
    python3 btc_pa_v6.py log       — Recent trade log
    python3 btc_pa_v6.py zones     — List current detected zones
    python3 btc_pa_v6.py reset     — Reset state
"""

import hashlib, hmac, json, sys, os, time, warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

try:
    import requests
except ImportError:
    os.system("pip install requests --break-system-packages -q")
    import requests

try:
    import numpy as np
except ImportError:
    os.system("pip install numpy --break-system-packages -q")
    import numpy as np

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
API_KEY = os.environ.get("BITKUB_API_KEY", "")
API_SECRET = os.environ.get("BITKUB_API_SECRET", "")
HOST = "https://api.bitkub.com"

BASE_DIR = os.path.expanduser("~/.openclaw/skills/bitkub-trader")
STATE_FILE = os.path.join(BASE_DIR, "v6_state.json")
LOG_FILE = os.path.join(BASE_DIR, "v6_log.json")

# Trade parameters
RISK_PER_TRADE_PCT = 3.0       # Risk 3% per trade (PA is precise → tighter)
MAX_OPEN_TRADES = 2
MIN_POSITION_THB = 500
DAILY_LOSS_LIMIT_PCT = 3.0

# Zone detection parameters
SWING_LOOKBACK = 20            # Bars to look back for swing points
ZONE_PADDING_PCT = 0.2         # Expand zone edges by this %
MIN_ZONE_SIZE_PCT = 0.3        # Smallest allowable zone (0.3% of price)
MAX_ZONE_AGE_BARS = 100        # Ignore zones older than N bars
MIN_BOS_STRENGTH_PCT = 0.3     # Price must break structure by ≥ 0.3%

# Retest & entry parameters
RETEST_PROXIMITY_PCT = 0.5     # Price is "at zone" if within 0.5%
REJECTION_MIN_WICK_RATIO = 1.5 # Wick must be ≥ 1.5x body for pin bar
REJECTION_MIN_BODY_PCT = 0.4   # Engulfing body must be ≥ 0.4% of price

# Exit parameters
TP_RR_RATIO = 2.0              # TP1 at 2:1 R:R
TP2_RR_RATIO = 4.0             # TP2 at 4:1 R:R
PARTIAL_EXIT_PCT = 50          # Exit 50% at TP1
TRAILING_STOP_PCT = 1.5        # After partial, trail 1.5%


# ══════════════════════════════════════════════
#  BITKUB API (shared with v4.1)
# ══════════════════════════════════════════════
def gen_sign(secret, payload_string):
    return hmac.new(secret.encode("utf-8"), payload_string.encode("utf-8"), hashlib.sha256).hexdigest()


def server_time():
    try:
        return str(requests.get(f"{HOST}/api/v3/servertime", timeout=10).text.strip())
    except:
        return str(int(time.time() * 1000))


def make_headers(method, path, body_or_query=""):
    ts = server_time()
    sig = gen_sign(API_SECRET, ts + method + path + body_or_query)
    return {"Accept": "application/json", "Content-Type": "application/json",
            "X-BTK-APIKEY": API_KEY, "X-BTK-TIMESTAMP": ts, "X-BTK-SIGN": sig}


def get_btc_price():
    try:
        data = requests.get(f"{HOST}/api/v3/market/ticker", timeout=10).json()
        if isinstance(data, list):
            btc = next((t for t in data if t.get("symbol") == "BTC_THB"), None)
            if btc:
                return {"last": float(btc["last"]), "bid": float(btc["highest_bid"]),
                        "ask": float(btc["lowest_ask"]), "high": float(btc["high_24_hr"]),
                        "low": float(btc["low_24_hr"]), "volume": float(btc["quote_volume"]),
                        "change": float(btc["percent_change"])}
    except Exception as e:
        print(f"⚠️ Price error: {e}")
    return None


def get_ohlcv(timeframe="60", limit=200):
    now = int(time.time())
    frm = now - (limit * int(timeframe) * 60)
    for url in [
        f"{HOST}/tradingview/history?symbol=BTC_THB&resolution={timeframe}&from={frm}&to={now}",
        f"{HOST}/api/market/tradingview?sym=BTC_THB&int={timeframe}&frm={frm}&to={now}",
    ]:
        try:
            data = requests.get(url, timeout=15).json()
            if data.get("s") == "ok":
                return [{"time": data["t"][i], "open": float(data["o"][i]),
                         "high": float(data["h"][i]), "low": float(data["l"][i]),
                         "close": float(data["c"][i]),
                         "volume": float(data["v"][i]) if "v" in data else 0}
                        for i in range(len(data["t"]))]
        except:
            continue
    return []


def get_balances():
    path = "/api/v3/market/balances"
    body = json.dumps({})
    headers = make_headers("POST", path, body)
    try:
        data = requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()
        if data.get("error") == 0:
            r = data["result"]
            return {"thb": float(r.get("THB", {}).get("available", 0)),
                    "btc": float(r.get("BTC", {}).get("available", 0))}
    except:
        pass
    return {"thb": 0, "btc": 0}


def place_buy(amount_thb, rate=0, typ="market"):
    path = "/api/v3/market/place-bid"
    body_dict = {"sym": "btc_thb", "amt": int(amount_thb), "rat": rate, "typ": typ}
    body = json.dumps(body_dict)
    headers = make_headers("POST", path, body)
    result = requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()
    if result.get("error") == 0:
        rec = result.get("result", {}).get("rec", 0)
        print(f"     ✅ Filled! Got {rec} BTC")
    else:
        print(f"     ❌ Order failed: error {result.get('error')}")
    return result


def place_sell(amount_btc, rate=0, typ="market"):
    path = "/api/v3/market/place-ask"
    body_dict = {"sym": "btc_thb", "amt": amount_btc, "rat": rate, "typ": typ}
    body = json.dumps(body_dict)
    headers = make_headers("POST", path, body)
    return requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()


# ══════════════════════════════════════════════
#  SWING POINT DETECTION
# ══════════════════════════════════════════════
def find_swings(candles, left=2, right=2):
    """
    Find swing highs and swing lows.
    A swing high = candle with higher high than N candles each side.
    A swing low = candle with lower low than N candles each side.
    """
    swings = []  # list of (idx, "high"/"low", price, time)
    for i in range(left, len(candles) - right):
        h = candles[i]["high"]
        l = candles[i]["low"]

        is_high = all(candles[j]["high"] < h for j in range(i-left, i)) and \
                  all(candles[j]["high"] < h for j in range(i+1, i+right+1))
        is_low = all(candles[j]["low"] > l for j in range(i-left, i)) and \
                 all(candles[j]["low"] > l for j in range(i+1, i+right+1))

        if is_high:
            swings.append({"idx": i, "type": "high", "price": h, "time": candles[i]["time"]})
        elif is_low:
            swings.append({"idx": i, "type": "low", "price": l, "time": candles[i]["time"]})

    return swings


# ══════════════════════════════════════════════
#  MARKET STRUCTURE
# ══════════════════════════════════════════════
def analyze_structure(swings):
    """
    Determine market structure:
    - UPTREND: Higher Highs (HH) + Higher Lows (HL)
    - DOWNTREND: Lower Highs (LH) + Lower Lows (LL)
    - RANGING: no clear pattern
    """
    if len(swings) < 4:
        return {"trend": "undefined", "last_high": None, "last_low": None,
                "prev_high": None, "prev_low": None}

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "undefined", "last_high": highs[-1] if highs else None,
                "last_low": lows[-1] if lows else None,
                "prev_high": None, "prev_low": None}

    last_high = highs[-1]
    prev_high = highs[-2]
    last_low = lows[-1]
    prev_low = lows[-2]

    hh = last_high["price"] > prev_high["price"]
    hl = last_low["price"] > prev_low["price"]
    lh = last_high["price"] < prev_high["price"]
    ll = last_low["price"] < prev_low["price"]

    if hh and hl:
        trend = "uptrend"
    elif lh and ll:
        trend = "downtrend"
    else:
        trend = "ranging"

    return {
        "trend": trend,
        "last_high": last_high,
        "last_low": last_low,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "hh": hh, "hl": hl, "lh": lh, "ll": ll,
    }


# ══════════════════════════════════════════════
#  BREAK OF STRUCTURE (BoS)
# ══════════════════════════════════════════════
def detect_bos(candles, structure, current_price):
    """
    BoS = price has broken the last significant high/low.
    - Uptrend BoS: price > last swing high (trend continuation up)
    - Downtrend BoS: price < last swing low (trend continuation down)
    """
    if structure["trend"] == "undefined":
        return {"bos": None, "broken_level": None}

    if structure["trend"] == "uptrend" and structure["last_high"]:
        level = structure["last_high"]["price"]
        # Check if any recent candle's HIGH broke the level
        recent_high = max(c["high"] for c in candles[-5:])
        if recent_high > level * (1 + MIN_BOS_STRENGTH_PCT / 100):
            return {"bos": "bullish", "broken_level": level,
                    "broken_swing": structure["last_high"]}

    if structure["trend"] == "downtrend" and structure["last_low"]:
        level = structure["last_low"]["price"]
        recent_low = min(c["low"] for c in candles[-5:])
        if recent_low < level * (1 - MIN_BOS_STRENGTH_PCT / 100):
            return {"bos": "bearish", "broken_level": level,
                    "broken_swing": structure["last_low"]}

    return {"bos": None, "broken_level": None}


# ══════════════════════════════════════════════
#  SUPPLY / DEMAND ZONES (Breaker Blocks)
# ══════════════════════════════════════════════
def find_zones(candles, swings, lookback=50):
    """
    Find supply/demand zones:
    - Demand zone: base (consolidation) before strong up-move
    - Supply zone: base before strong down-move

    A "base" is 1-3 candles before a strong impulse candle.
    Strong impulse = candle range > 2x average range of previous 10 candles.
    """
    if len(candles) < 30:
        return []

    zones = []
    start_idx = max(0, len(candles) - lookback)

    for i in range(start_idx + 10, len(candles) - 2):
        # Calculate average range of previous 10 candles
        ranges = [candles[j]["high"] - candles[j]["low"] for j in range(i-10, i)]
        avg_range = np.mean(ranges)
        if avg_range == 0:
            continue

        current = candles[i]
        curr_range = current["high"] - current["low"]
        curr_body = abs(current["close"] - current["open"])

        # Strong impulse candle
        if curr_range > 2 * avg_range and curr_body > 0.6 * curr_range:
            # Direction of impulse
            is_up = current["close"] > current["open"]

            # The base is 1-3 candles before the impulse
            base_start = max(0, i - 3)
            base_end = i  # exclusive

            base_candles = candles[base_start:base_end]
            if not base_candles:
                continue

            base_high = max(c["high"] for c in base_candles)
            base_low = min(c["low"] for c in base_candles)

            # Skip if zone too small
            zone_size_pct = (base_high - base_low) / current["close"] * 100
            if zone_size_pct < MIN_ZONE_SIZE_PCT:
                continue

            # Pad zone edges slightly
            padding = (base_high - base_low) * (ZONE_PADDING_PCT / 100)
            zone_high = base_high + padding
            zone_low = base_low - padding

            zone_type = "demand" if is_up else "supply"
            zones.append({
                "type": zone_type,
                "high": zone_high,
                "low": zone_low,
                "created_idx": i,
                "created_time": current["time"],
                "age_bars": len(candles) - 1 - i,
                "tested": False,
                "impulse_strength": curr_range / avg_range,
            })

    # Filter: remove old zones
    zones = [z for z in zones if z["age_bars"] <= MAX_ZONE_AGE_BARS]

    # Check if zones have been broken (invalidated)
    for z in zones:
        z_created = z["created_idx"]
        if z["type"] == "demand":
            # Demand broken if any close below zone_low after creation
            broken = any(c["close"] < z["low"] for c in candles[z_created+1:])
            z["valid"] = not broken
        else:
            # Supply broken if any close above zone_high after creation
            broken = any(c["close"] > z["high"] for c in candles[z_created+1:])
            z["valid"] = not broken

    zones = [z for z in zones if z["valid"]]

    return zones


def is_price_at_zone(price, zone):
    """Check if current price is touching or within zone (with proximity buffer)"""
    buffer = price * RETEST_PROXIMITY_PCT / 100
    return (zone["low"] - buffer) <= price <= (zone["high"] + buffer)


# ══════════════════════════════════════════════
#  REJECTION CANDLE DETECTION
# ══════════════════════════════════════════════
def is_rejection_candle(candle, direction):
    """
    Check last 1-2 candles for:
    - Bullish rejection (for demand zone): long lower wick or bullish engulfing
    - Bearish rejection (for supply zone): long upper wick or bearish engulfing
    """
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    candle_range = h - l
    if candle_range == 0:
        return False, "no_range"

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if direction == "bullish":
        # Pin bar: long lower wick
        if lower_wick >= REJECTION_MIN_WICK_RATIO * body and lower_wick > upper_wick * 1.5:
            return True, "bullish_pin_bar"
        # Strong bullish close
        if c > o and body > candle_range * 0.7 and body > c * REJECTION_MIN_BODY_PCT / 100:
            return True, "strong_bullish_close"
    else:  # bearish
        if upper_wick >= REJECTION_MIN_WICK_RATIO * body and upper_wick > lower_wick * 1.5:
            return True, "bearish_pin_bar"
        if c < o and body > candle_range * 0.7 and body > o * REJECTION_MIN_BODY_PCT / 100:
            return True, "strong_bearish_close"

    return False, "no_rejection"


def is_engulfing(prev, curr, direction):
    """Bullish/bearish engulfing pattern on 2 candles"""
    prev_body = abs(prev["close"] - prev["open"])
    curr_body = abs(curr["close"] - curr["open"])

    if direction == "bullish":
        # Previous red, current green, current body engulfs previous
        if prev["close"] < prev["open"] and curr["close"] > curr["open"]:
            if curr["close"] > prev["open"] and curr["open"] < prev["close"]:
                if curr_body > prev_body:
                    return True, "bullish_engulfing"
    else:
        if prev["close"] > prev["open"] and curr["close"] < curr["open"]:
            if curr["close"] < prev["open"] and curr["open"] > prev["close"]:
                if curr_body > prev_body:
                    return True, "bearish_engulfing"

    return False, "not_engulfing"


# ══════════════════════════════════════════════
#  SIGNAL LOGIC — THE ONE PATTERN
# ══════════════════════════════════════════════
def check_entry_signal(candles_1h, candles_4h):
    """
    THE ONE PATTERN from video:
    1. 4H trend established (HH/HL or LH/LL)
    2. BoS confirmed
    3. 1H price at valid supply/demand zone
    4. Rejection candle at zone

    Returns dict with signal info or None
    """
    # Step 1: Structure on 4H
    swings_4h = find_swings(candles_4h, left=3, right=3)
    structure_4h = analyze_structure(swings_4h)

    if structure_4h["trend"] == "undefined":
        return {"signal": None, "reason": "4H structure undefined"}

    # Step 2: BoS on 4H
    bos = detect_bos(candles_4h, structure_4h, candles_4h[-1]["close"])

    # Step 3: Zones on 1H
    swings_1h = find_swings(candles_1h, left=2, right=2)
    zones_1h = find_zones(candles_1h, swings_1h)

    if not zones_1h:
        return {"signal": None, "reason": "No valid zones on 1H",
                "structure_4h": structure_4h, "zones": []}

    current_price = candles_1h[-1]["close"]

    # Step 4: Match zone with trend direction
    # In uptrend, we want to BUY at demand zones
    # In downtrend, we want to SELL at supply zones
    matching_zones = []
    if structure_4h["trend"] == "uptrend":
        direction = "buy"
        rejection_dir = "bullish"
        matching_zones = [z for z in zones_1h if z["type"] == "demand"]
    elif structure_4h["trend"] == "downtrend":
        direction = "sell"
        rejection_dir = "bearish"
        matching_zones = [z for z in zones_1h if z["type"] == "supply"]
    else:
        return {"signal": None, "reason": "Ranging — no trade",
                "structure_4h": structure_4h, "zones": zones_1h}

    # Step 5: Find zone where price is currently testing (retest)
    touched_zone = None
    for z in matching_zones:
        if is_price_at_zone(current_price, z):
            touched_zone = z
            break

    if not touched_zone:
        return {"signal": None, "reason": "No zone retest",
                "structure_4h": structure_4h,
                "zones": matching_zones, "bos": bos}

    # Step 6: Check rejection candle (last closed candle)
    last_candle = candles_1h[-2]  # -1 is current forming, -2 is last closed
    prev_candle = candles_1h[-3]

    rej_ok, rej_type = is_rejection_candle(last_candle, rejection_dir)
    eng_ok, eng_type = is_engulfing(prev_candle, last_candle, rejection_dir)

    if not rej_ok and not eng_ok:
        return {"signal": None, "reason": "At zone but no rejection candle yet",
                "structure_4h": structure_4h, "touched_zone": touched_zone,
                "bos": bos, "zones": matching_zones}

    pattern = rej_type if rej_ok else eng_type

    # All conditions met — signal!
    return {
        "signal": direction,
        "reason": "THE ONE PATTERN triggered",
        "structure_4h": structure_4h,
        "touched_zone": touched_zone,
        "bos": bos,
        "pattern": pattern,
        "zones": matching_zones,
        "current_price": current_price,
    }


# ══════════════════════════════════════════════
#  RISK MANAGEMENT
# ══════════════════════════════════════════════
def calculate_position_size(balance_thb, entry, sl):
    """Size position based on risk % of account"""
    risk_amount = balance_thb * (RISK_PER_TRADE_PCT / 100)
    risk_per_unit = abs(entry - sl)
    if risk_per_unit == 0:
        return 0
    # position = risk_amount / (stop_distance / entry)
    stop_dist_pct = risk_per_unit / entry
    position_thb = risk_amount / stop_dist_pct if stop_dist_pct > 0 else 0
    position_thb = min(position_thb, balance_thb * 0.3)  # Max 30% of balance
    return max(MIN_POSITION_THB, int(position_thb)) if position_thb >= MIN_POSITION_THB else 0


# ══════════════════════════════════════════════
#  STATE MANAGEMENT
# ══════════════════════════════════════════════
def load_state():
    default = {"open_trades": [], "closed_trades": [], "daily_pnl": 0,
               "daily_date": datetime.now().strftime("%Y-%m-%d"),
               "total_trades": 0, "winning_trades": 0, "total_pnl": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            if s.get("daily_date") != datetime.now().strftime("%Y-%m-%d"):
                s["daily_pnl"] = 0
                s["daily_date"] = datetime.now().strftime("%Y-%m-%d")
            for k, v in default.items():
                if k not in s:
                    s[k] = v
            return s
        except:
            pass
    return default


def save_state(state):
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def log_event(action, details):
    entry = {"time": datetime.now().isoformat(), "action": action, **details}
    try:
        logs = json.load(open(LOG_FILE)) if os.path.exists(LOG_FILE) else []
    except:
        logs = []
    logs.append(entry)
    with open(LOG_FILE, "w") as f:
        json.dump(logs[-500:], f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════
#  EXIT LOGIC
# ══════════════════════════════════════════════
def check_exits(state, current_price, dry_run=True):
    """Check all open trades for exit conditions"""
    for trade in state["open_trades"][:]:
        entry = trade["entry_price"]
        sl = trade["stop_loss"]
        tp1 = trade["take_profit_1"]
        tp2 = trade["take_profit_2"]
        direction = trade["direction"]
        pnl_pct = (current_price - entry) / entry * 100 if direction == "buy" else (entry - current_price) / entry * 100

        should_exit = False
        partial = False
        reason = ""

        if direction == "buy":
            if current_price <= sl:
                should_exit, reason = True, f"STOP LOSS ({pnl_pct:+.1f}%)"
            elif current_price >= tp2:
                should_exit, reason = True, f"TP2 ({pnl_pct:+.1f}%)"
            elif current_price >= tp1 and not trade.get("partial_exited"):
                partial, reason = True, f"Partial TP1 ({pnl_pct:+.1f}%)"
            elif trade.get("partial_exited"):
                highest = trade.get("highest_price", entry)
                if current_price > highest:
                    trade["highest_price"] = current_price
                dd = (trade["highest_price"] - current_price) / trade["highest_price"] * 100
                if dd >= TRAILING_STOP_PCT:
                    should_exit, reason = True, f"TRAIL ({pnl_pct:+.1f}%)"

        if partial:
            exit_btc = trade["amount_btc"] * (PARTIAL_EXIT_PCT / 100)
            pnl_thb = exit_btc * (current_price - entry)
            print(f"\n  🟡 PARTIAL EXIT: {reason} — selling {PARTIAL_EXIT_PCT}%")
            if not dry_run:
                place_sell(exit_btc)
            trade["amount_btc"] -= exit_btc
            trade["amount_thb"] *= (1 - PARTIAL_EXIT_PCT / 100)
            trade["partial_exited"] = True
            trade["stop_loss"] = entry  # Move SL to breakeven
            state["daily_pnl"] += pnl_thb
            state["total_pnl"] += pnl_thb
            log_event("PARTIAL_SELL", {"reason": reason, "pnl": pnl_thb, "dry_run": dry_run})

        elif should_exit:
            pnl_thb = trade["amount_btc"] * (current_price - entry) if direction == "buy" else 0
            print(f"\n  🔴 EXIT: {reason} = {pnl_thb:,.0f} THB")
            if not dry_run:
                place_sell(trade["amount_btc"])
            state["daily_pnl"] += pnl_thb
            state["total_pnl"] += pnl_thb
            state["total_trades"] += 1
            if pnl_thb > 0:
                state["winning_trades"] += 1
            state["closed_trades"].append({
                **trade, "exit_price": current_price, "pnl": pnl_thb,
                "exit_reason": reason, "exit_time": datetime.now().isoformat()
            })
            state["closed_trades"] = state["closed_trades"][-100:]
            state["open_trades"].remove(trade)
            log_event("SELL", {"reason": reason, "pnl": pnl_thb, "dry_run": dry_run})


# ══════════════════════════════════════════════
#  MAIN ANALYSIS & TRADE LOGIC
# ══════════════════════════════════════════════
def run(dry_run=True, quiet=False):
    # Quiet mode: suppress all output unless BUY signal
    import io
    import sys

    if quiet:
        # Capture all prints to discard unless BUY
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

    if not quiet:
        print("=" * 64)
        print("  📊 PRICE ACTION BOT v6 — One Pattern Only")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 64)

    price_data = get_btc_price()
    if not price_data:
        print("❌ No price data")
        if quiet:
            sys.stdout = old_stdout; sys.stderr = old_stderr
        return

    candles_1h = get_ohlcv("60", 150)
    candles_4h = get_ohlcv("240", 80)
    time.sleep(0.3)

    if len(candles_1h) < 50 or len(candles_4h) < 20:
        print(f"❌ Not enough data (1h: {len(candles_1h)}, 4h: {len(candles_4h)})")
        if quiet:
            sys.stdout = old_stdout; sys.stderr = old_stderr
        return

    current = price_data["last"]
    print(f"\n📊 BTC/THB: {current:,.2f}  ({price_data['change']:+.2f}%)")

    # Analyze
    signal_info = check_entry_signal(candles_1h, candles_4h)

    # Print structure
    if signal_info.get("structure_4h"):
        s = signal_info["structure_4h"]
        print(f"\n🏗️ 4H STRUCTURE: {s['trend'].upper()}")
        if s["last_high"]:
            print(f"   Last HIGH: {s['last_high']['price']:,.0f}")
        if s["last_low"]:
            print(f"   Last LOW:  {s['last_low']['price']:,.0f}")
        if s["trend"] == "uptrend":
            print(f"   ✅ HH: {s['hh']}  HL: {s['hl']}")
        elif s["trend"] == "downtrend":
            print(f"   ✅ LH: {s['lh']}  LL: {s['ll']}")

    # Print BoS
    if signal_info.get("bos"):
        bos = signal_info["bos"]
        if bos["bos"]:
            print(f"\n💥 BoS: {bos['bos'].upper()} — broke {bos['broken_level']:,.0f}")
        else:
            print(f"\n💥 BoS: none")

    # Print zones
    zones = signal_info.get("zones", [])
    if zones:
        print(f"\n📦 ZONES ({len(zones)} valid):")
        for i, z in enumerate(zones[:5]):
            at = "🎯" if is_price_at_zone(current, z) else "  "
            print(f"   {at} {z['type']:7s} [{z['low']:,.0f} – {z['high']:,.0f}]  age:{z['age_bars']}b  strength:{z['impulse_strength']:.1f}x")

    # Print touched zone
    if signal_info.get("touched_zone"):
        z = signal_info["touched_zone"]
        print(f"\n🎯 RETEST: price in {z['type']} zone [{z['low']:,.0f} – {z['high']:,.0f}]")

    # Check exits first
    state = load_state()
    check_exits(state, current, dry_run)

    # Entry decision
    print(f"\n{'='*64}")
    if signal_info["signal"] is None:
        print(f"  ⚪ HOLD — {signal_info['reason']}")
    else:
        direction = signal_info["signal"]
        pattern = signal_info["pattern"]
        zone = signal_info["touched_zone"]

        # Calculate entry, SL, TP
        entry = current
        if direction == "buy":
            sl = zone["low"] * 0.998  # Slightly below zone
            risk = entry - sl
            tp1 = entry + risk * TP_RR_RATIO
            tp2 = entry + risk * TP2_RR_RATIO
        else:
            sl = zone["high"] * 1.002  # Slightly above zone
            risk = sl - entry
            tp1 = entry - risk * TP_RR_RATIO
            tp2 = entry - risk * TP2_RR_RATIO

        rr = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0

        # Check portfolio
        balances = get_balances() if not dry_run else {"thb": 30000, "btc": 0}
        pos_size = calculate_position_size(balances["thb"], entry, sl)

        # Risk checks
        if len(state["open_trades"]) >= MAX_OPEN_TRADES:
            print(f"  ⚪ HOLD — Max trades reached ({MAX_OPEN_TRADES})")
        elif pos_size < MIN_POSITION_THB:
            print(f"  ⚪ HOLD — Position size too small ({pos_size} < {MIN_POSITION_THB})")
        elif balances["thb"] < pos_size:
            print(f"  ⚪ HOLD — Insufficient balance ({balances['thb']:,.0f} < {pos_size})")
        elif state["daily_pnl"] < 0 and abs(state["daily_pnl"]) / (balances["thb"] + balances["btc"] * current) * 100 > DAILY_LOSS_LIMIT_PCT:
            print(f"  ⚪ HOLD — Daily loss limit reached")
        else:
            print(f"  🟢 {direction.upper()} — {pattern}")
            print(f"     Zone: [{zone['low']:,.0f} – {zone['high']:,.0f}] ({zone['type']})")
            print(f"     Entry:   {entry:,.0f}")
            print(f"     SL:      {sl:,.0f}  ({(sl-entry)/entry*100:+.2f}%)")
            print(f"     TP1:     {tp1:,.0f}  ({(tp1-entry)/entry*100:+.2f}%)  [2R, exit 50%]")
            print(f"     TP2:     {tp2:,.0f}  ({(tp2-entry)/entry*100:+.2f}%)  [4R, exit rest]")
            print(f"     R:R:     {rr:.2f}")
            print(f"     Size:    {pos_size:,} THB")

            if direction == "buy":  # Only buy for now (long-only)
                if not dry_run:
                    result = place_buy(pos_size, 0, "market")
                    if result.get("error") != 0:
                        print(f"     ❌ Order failed")
                        if quiet:
                            sys.stdout = old_stdout; sys.stderr = old_stderr
                        return

                trade = {
                    "entry_price": entry, "amount_thb": pos_size,
                    "amount_btc": pos_size / entry,
                    "entry_time": datetime.now().isoformat(),
                    "highest_price": entry,
                    "stop_loss": sl, "take_profit_1": tp1, "take_profit_2": tp2,
                    "partial_exited": False, "direction": direction,
                    "zone_low": zone["low"], "zone_high": zone["high"],
                    "zone_type": zone["type"], "pattern": pattern,
                    "structure_4h": signal_info["structure_4h"]["trend"],
                }
                state["open_trades"].append(trade)
                log_event("BUY", {
                    "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "pattern": pattern, "zone": zone["type"],
                    "structure": signal_info["structure_4h"]["trend"],
                    "amount": pos_size, "dry_run": dry_run,
                })
            else:
                print(f"     ⚠️ SELL signal — bot is long-only, skipping")

    print(f"{'='*64}")

    # Summary
    wr = state["winning_trades"] / state["total_trades"] * 100 if state["total_trades"] > 0 else 0
    print(f"\n📋 Trades: {len(state['open_trades'])} open | {state['total_trades']} total | {wr:.0f}% win")
    print(f"   Today: {state['daily_pnl']:,.0f} | Total: {state['total_pnl']:,.0f} THB")
    for i, t in enumerate(state["open_trades"]):
        pnl = (current - t["entry_price"]) / t["entry_price"] * 100
        print(f"   #{i+1} {t['entry_price']:,.0f}→{current:,.0f} ({pnl:+.1f}%) SL:{t['stop_loss']:,.0f}")

    save_state(state)

    if quiet:
        captured = sys.stdout.getvalue()
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        # Only print if BUY signal was triggered
        if "🟢 BUY" in captured or "BUY" in captured:
            print(captured)


# ══════════════════════════════════════════════
#  ZONE INSPECTOR
# ══════════════════════════════════════════════
def show_zones():
    candles_1h = get_ohlcv("60", 100)
    if len(candles_1h) < 30:
        print("❌ Not enough data"); return

    swings = find_swings(candles_1h)
    zones = find_zones(candles_1h, swings)
    current = candles_1h[-1]["close"]

    print(f"📦 ZONES on 1H BTC/THB — current {current:,.0f}")
    print("=" * 70)
    if not zones:
        print("No valid zones detected")
        return

    for z in sorted(zones, key=lambda x: abs((x["high"]+x["low"])/2 - current)):
        at = "🎯 AT ZONE" if is_price_at_zone(current, z) else ""
        mid = (z["high"] + z["low"]) / 2
        dist_pct = (mid - current) / current * 100
        print(f"{z['type']:7s}  [{z['low']:,.0f} – {z['high']:,.0f}]  "
              f"age:{z['age_bars']:>3}b  strength:{z['impulse_strength']:.1f}x  "
              f"dist:{dist_pct:+.2f}%  {at}")


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
if __name__ == "__main__":
    cmds = {
        "analyze": "Show current analysis (dry run, no trades)",
        "live":    "Execute live trading",
        "status":  "Show open positions",
        "log":     "Show recent trade log",
        "zones":   "Show all detected zones",
        "reset":   "Reset state",
    }

    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    quiet = "--quiet" in sys.argv
    if "--quiet" in sys.argv:
        sys.argv = [x for x in sys.argv if x != "--quiet"]

    if cmd not in cmds:
        print("Price Action Bot v6 — One Pattern Only")
        print("=" * 45)
        for c, d in cmds.items():
            print(f"  {c:10s}  {d}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "analyze":
        run(dry_run=True, quiet=quiet)
    elif cmd == "live":
        if not API_KEY or not API_SECRET:
            print("❌ Set BITKUB_API_KEY and BITKUB_API_SECRET"); sys.exit(1)
        print("⚠️ LIVE MODE — 5s to cancel...")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nCancelled."); sys.exit(0)
        run(dry_run=False, quiet=quiet)
    elif cmd == "status":
        p = get_btc_price()
        state = load_state()
        if p and state["open_trades"]:
            print(f"BTC: {p['last']:,.2f}")
            for i, t in enumerate(state["open_trades"]):
                pnl = (p["last"] - t["entry_price"]) / t["entry_price"] * 100
                print(f"\n  Trade #{i+1} [{t.get('pattern','')}]")
                print(f"    Entry:  {t['entry_price']:,.0f}")
                print(f"    Now:    {p['last']:,.0f}  ({pnl:+.1f}%)")
                print(f"    Zone:   [{t['zone_low']:,.0f} – {t['zone_high']:,.0f}]")
                print(f"    SL:     {t['stop_loss']:,.0f}")
                print(f"    TP1:    {t['take_profit_1']:,.0f}")
                print(f"    TP2:    {t['take_profit_2']:,.0f}")
        else:
            print("No open trades")
    elif cmd == "log":
        if os.path.exists(LOG_FILE):
            for e in json.load(open(LOG_FILE))[-15:]:
                print(f"  {e['time'][:19]} {e['action']:12s} {json.dumps({k:v for k,v in e.items() if k not in ['time','action']})}")
    elif cmd == "zones":
        show_zones()
    elif cmd == "reset":
        for f in [STATE_FILE, LOG_FILE]:
            if os.path.exists(f):
                os.remove(f)
        print("✅ Reset done")
