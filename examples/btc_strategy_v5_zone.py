#!/usr/bin/env python3
"""
================================================================
  BTC ZONE TRADING SYSTEM v5 — "1 Setup Trade"
  Based on: Supply/Demand + Market Structure + BoS

================================================================
  CORE PHILOSOPHY: "ใช้แค่ 1 ท่าเทรด"
  - 1 Setup: Supply/Demand Zone + Retest + BoS
  - No indicators — pure price action
  - 4H = structure, 1H = entry

  SYSTEM COMPONENTS:
  1. MARKET STRUCTURE
     - Uptrend: HH + HL
     - Downtrend: LH + LL

  2. SUPPLY/DEMAND ZONES
     - Supply (red box) = sell density = resistance
     - Demand (green box) = buy density = support
     - Zones form at breakout points (Breaker Blocks)

  3. ENTRY: RETEST + REJECTION CANDLE
     - Price returns to zone = Retest/Mitigation
     - Entry on rejection candle (engulfing / strong reversal)

  4. BoS CONFIRMATION
     - Price breaks HH/LH = trend continuation confirmed
     - Wait for retest to zone, then enter

  5. STOP LOSS: opposite side of zone

  6. NO TAKE PROFIT — trailing stop or zone exit
================================================================
"""

import hashlib, hmac, json, sys, os, time, math, pickle, warnings
from datetime import datetime, timedelta
from collections import defaultdict

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
API_KEY=os.getenv("BITKUB_API_KEY", "")
API_SECRET=os.getenv("BITKUB_API_SECRET", "")
HOST = "https://api.bitkub.com"

BASE_DIR = os.path.expanduser("~/.openclaw/skills/bitkub-trader")
STATE_FILE = os.path.join(BASE_DIR, "v5_zone_state.json")
LOG_FILE = os.path.join(BASE_DIR, "v5_zone_log.json")

# ─── Risk Parameters ───
RISK_PER_TRADE_PCT = 2.0      # 2% per trade (conservative for zone trading)
MAX_PORTFOLIO_HEAT = 6.0      # max 3 trades × 2%
MAX_OPEN_TRADES = 3           # 3 simultaneous trades
DAILY_LOSS_LIMIT_PCT = 3.0    # daily loss limit

# ─── Zone Parameters ───
ZONE_LOOKBACK = 20            # candles to look back for zone formation
ZONE_WIDTH_PCT = 3.0          # zone width as % of price (3.0% — optimal from sweep)
REJECTION_THRESHOLD = 0.3     # min bounce/rejection strength % (from sweep)

# ─── Market Structure ───
SWING_LOOKBACK = 5            # lookback for swing high/low detection
BOS_CONFIRMATION = True       # require BoS confirmation
STRUCTURE_FILTER = "trend_only"  # "any", "trend_only", "bos_required" — optimal: trend_only

# ─── Entry Confirmation ───
REJECTION_CANDLES = 2         # require N rejection candles before entry
VOLUME_SPIKE_THRESHOLD = 1.5  # volume must be 1.5x average

# ─── Trailing Stop ───
TRAILING_STOP_PCT = 1.0       # trail after entry (1.0% — optimal from sweep)

# ─── Time-based Exit ───
MAX_HOLD_HOURS = 96           # 4 days max hold
MIN_PROFIT_TO_HOLD = 0.3     # % — if profit < this after MAX_HOLD_HOURS, exit

# ─── No-Trade Hours ───
NO_TRADE_HOURS = {
    "saturday_morning": {"enabled": True, "days": [5], "hours": list(range(0, 12))},
    "asian_dead_zone":  {"enabled": True, "hours": [2, 3, 4, 5], "only_regime": "ranging"},
}

# ─── Cooldown ───
COOLDOWN_AFTER_LOSSES = 3
COOLDOWN_HOURS = 6

# ══════════════════════════════════════════════
#  BITKUB API
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
        print(f"  ⚠️ Price error: {e}")
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

def place_buy(amount_thb, rate=0, typ="market", maker=False, bid_price=0):
    """Place buy order"""
    path = "/api/v3/market/place-bid"
    MAKER_FEE_OFFSET_PCT = 0.1
    LIMIT_ORDER_TIMEOUT_MIN = 10

    if maker and bid_price > 0:
        maker_price = int(bid_price * (1 - MAKER_FEE_OFFSET_PCT / 100))
        body_dict = {"sym": "btc_thb", "amt": int(amount_thb), "rat": maker_price, "typ": "limit"}
        body = json.dumps(body_dict)
        headers = make_headers("POST", path, body)
        result = requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()
        if result.get("error") == 0:
            print(f"     🎯 Maker limit @ {maker_price:,.0f}")
            return result
        print(f"     Maker limit failed, trying taker...")

    body_dict = {"sym": "btc_thb", "amt": int(amount_thb), "rat": rate, "typ": typ}
    body = json.dumps(body_dict)
    headers = make_headers("POST", path, body)
    result = requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()

    if result.get("error") != 0 and typ == "limit":
        body_dict["rat"] = 0
        body_dict["typ"] = "market"
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
    """Place sell order"""
    path = "/api/v3/market/place-ask"
    body_dict = {"sym": "btc_thb", "amt": amount_btc, "rat": rate, "typ": typ}
    body = json.dumps(body_dict)
    headers = make_headers("POST", path, body)
    return requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()


# ══════════════════════════════════════════════
#  PRICE ACTION INDICATORS (No Indicators = Pure Price Action)
# ══════════════════════════════════════════════

def get_swing_highs_lows(candles, lookback=5):
    """
    Find swing highs and lows using lookback window.
    Swing High: highest point in the middle of a lookback window
    Swing Low: lowest point in the middle of a lookback window
    Returns lists of {index, price, time}
    """
    highs = []
    lows = []

    for i in range(lookback, len(candles) - lookback):
        window_highs = [candles[j]["high"] for j in range(i - lookback, i + lookback + 1)]
        window_lows = [candles[j]["low"] for j in range(i - lookback, i + lookback + 1)]

        if candles[i]["high"] == max(window_highs):
            highs.append({"index": i, "price": candles[i]["high"], "time": candles[i]["time"]})
        if candles[i]["low"] == min(window_lows):
            lows.append({"index": i, "price": candles[i]["low"], "time": candles[i]["time"]})

    return highs, lows


def detect_market_structure(candles, highs, lows):
    """
    Detect market structure:
    - Uptrend: HH + HL (Higher Highs, Higher Lows)
    - Downtrend: LH + LL (Lower Highs, Lower Lows)
    - Ranging: no clear HH/HL or LH/LL

    Returns: "uptrend", "downtrend", "ranging"
    """
    if len(highs) < 2 or len(lows) < 2:
        return "ranging"

    # Get last few HHs and HLs
    recent_highs = highs[-4:]
    recent_lows = lows[-4:]

    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return "ranging"

    # Check for HH/HL (uptrend)
    hh_count = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i]["price"] > recent_highs[i-1]["price"])
    hl_count = sum(1 for i in range(1, len(recent_lows)) if recent_lows[i]["price"] > recent_lows[i-1]["price"])

    # Check for LH/LL (downtrend)
    lh_count = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i]["price"] < recent_highs[i-1]["price"])
    ll_count = sum(1 for i in range(1, len(recent_lows)) if recent_lows[i]["price"] < recent_lows[i-1]["price"])

    # Require at least 2 confirmations
    if hh_count >= 2 and hl_count >= 2:
        return "uptrend"
    elif lh_count >= 2 and ll_count >= 2:
        return "downtrend"
    else:
        return "ranging"


def find_supply_demand_zones(candles, highs, lows, lookback=15, width_pct=1.0):
    """
    Find Supply and Demand zones.

    From video:
    - Zone = area where price consolidated before breakout
    - When price breaks UP through resistance → that resistance becomes DEMAND (support)
    - When price breaks DOWN through support → that support becomes SUPPLY (resistance)
    - Entry = retest of the broken zone

    Zone = swing low area (for demand) or swing high area (for supply)
    where price made a significant move away after.
    """
    zones = {"supply": [], "demand": []}

    if len(candles) < lookback + 5:
        return zones

    closes = np.array([c["close"] for c in candles], dtype=float)

    # Get recent swing points - these define the zones
    recent_highs = [h for h in highs if h["index"] >= len(candles) - lookback - 5]
    recent_lows = [l for l in lows if l["index"] >= len(candles) - lookback - 5]

    # For each swing low: check if price bounced after it (demand zone)
    for low in recent_lows:
        i = low["index"]
        low_price = low["price"]

        if i + 2 >= len(candles):
            continue

        # Calculate bounce after this low
        # Take the best close in next 3 candles
        future_max = max(closes[i+1:min(i+4, len(closes))])
        bounce_pct = (future_max - low_price) / low_price * 100 if low_price > 0 else 0

        if bounce_pct > 0.5:  # Some bounce = zone has meaning
            # Zone is around the low
            zone_low = low_price * (1 - width_pct / 100)
            zone_high = low_price * (1 + width_pct / 100)

            zones["demand"].append({
                "zone_low": float(zone_low),
                "zone_high": float(zone_high),
                "origin_idx": i,
                "origin_price": float(low_price),
                "origin_time": candles[i]["time"],
                "bounce_pct": float(bounce_pct),
                "type": "demand"
            })

    # For each swing high: check if price rejected after it (supply zone)
    for high in recent_highs:
        i = high["index"]
        high_price = high["price"]

        if i + 2 >= len(candles):
            continue

        future_min = min(closes[i+1:min(i+4, len(closes))])
        rejection_pct = (high_price - future_min) / high_price * 100 if high_price > 0 else 0

        if rejection_pct > 0.5:
            zone_low = high_price * (1 - width_pct / 100)
            zone_high = high_price * (1 + width_pct / 100)

            zones["supply"].append({
                "zone_low": float(zone_low),
                "zone_high": float(zone_high),
                "origin_idx": i,
                "origin_price": float(high_price),
                "origin_time": candles[i]["time"],
                "rejection_pct": float(rejection_pct),
                "type": "supply"
            })

    # Remove duplicates/overlaps - keep stronger ones
    zones["demand"] = _remove_overlapping_zones(zones["demand"])
    zones["supply"] = _remove_overlapping_zones(zones["supply"])

    # Sort by freshness
    zones["demand"].sort(key=lambda x: x["origin_idx"], reverse=True)
    zones["supply"].sort(key=lambda x: x["origin_idx"], reverse=True)

    return zones


def _remove_overlapping_zones(zones):
    """Remove zones that overlap too much"""
    if len(zones) <= 1:
        return zones

    filtered = []
    for zone in zones:
        overlaps = False
        for existing in filtered:
            # Check overlap
            overlap_low = max(zone["zone_low"], existing["zone_low"])
            overlap_high = min(zone["zone_high"], existing["zone_high"])
            if overlap_high > overlap_low:
                # They overlap - keep the one with stronger bounce/rejection
                if zone.get("bounce_pct", 0) > existing.get("bounce_pct", 0):
                    filtered.remove(existing)
                else:
                    overlaps = True
                    break
        if not overlaps:
            filtered.append(zone)

    return filtered


def detect_rejection_candles(candles, zone, direction="demand"):
    """
    Detect rejection candles at a zone.
    For DEMAND (buy zone): looking for bullish rejection = price bounced from zone
    For SUPPLY (sell zone): looking for bearish rejection = price rejected from zone

    Returns list of rejection candle indices
    """
    rejection_indices = []
    zone_low = zone["zone_low"]
    zone_high = zone["zone_high"]

    for i in range(max(0, zone["origin_idx"] - 3), min(len(candles), zone["origin_idx"] + 3)):
        c = candles[i]
        body = abs(c["close"] - c["open"])
        range_total = c["high"] - c["low"]

        if range_total == 0:
            continue

        body_pct = body / range_total * 100

        if direction == "demand":
            # Bullish rejection: price near zone low, closed higher, small body
            if c["low"] <= zone_high and c["close"] > c["open"]:
                # Small body, long lower wick
                lower_wick = min(c["close"], c["open"]) - c["low"]
                if lower_wick / range_total > 0.5 and body_pct < 40:
                    rejection_indices.append(i)
        else:  # supply
            # Bearish rejection: price near zone high, closed lower, small body
            if c["high"] >= zone_low and c["close"] < c["open"]:
                upper_wick = c["high"] - max(c["close"], c["open"])
                if upper_wick / range_total > 0.5 and body_pct < 40:
                    rejection_indices.append(i)

    return rejection_indices


def check_breakeven_of_structure(candles, highs, lows, current_idx, direction="long"):
    """
    Check if price broke structure (BoS) confirming trend.

    For LONG: Price must break above recent HH to confirm uptrend
    For SHORT: Price must break below recent LL to confirm downtrend
    """
    if len(highs) < 2 or len(lows) < 2:
        return False

    # Get recent highs/lows before current candle
    recent_highs = [h for h in highs if h["index"] < current_idx][-3:]
    recent_lows = [l for l in lows if l["index"] < current_idx][-3:]

    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return False

    current_price = candles[current_idx]["close"]

    if direction == "long":
        # Check if price broke above recent HH
        last_hh = recent_highs[-1]["price"]
        if current_price > last_hh:
            return True

    elif direction == "short":
        # Check if price broke below recent LL
        last_ll = recent_lows[-1]["price"]
        if current_price < last_ll:
            return True

    return False


def is_price_in_zone(price, zone):
    """Check if current price is within a zone"""
    return zone["zone_low"] <= price <= zone["zone_high"]


def get_volume_ratio(candles, lookback=20):
    """Calculate current volume vs average volume"""
    if len(candles) < lookback + 1:
        return 1.0

    vols = [c["volume"] for c in candles[-lookback-1:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 1
    current_vol = candles[-1]["volume"]

    return current_vol / avg_vol if avg_vol > 0 else 1.0


# ══════════════════════════════════════════════
#  ZONE SCANNER
# ══════════════════════════════════════════════
class ZoneScanner:
    """
    Scans for tradeable zone setups.
    Strategy: Supply/Demand + Retest + Rejection + BoS
    """

    def __init__(self):
        self.state = self._load()

    def _load(self):
        default = {
            "open_trades": [], "closed_trades": [], "daily_pnl": 0,
            "daily_date": datetime.now().strftime("%Y-%m-%d"),
            "total_trades": 0, "winning_trades": 0, "total_pnl": 0,
            "recent_results": [], "last_loss_time": "",
            "detected_zones": {"supply": [], "demand": []}
        }
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    s = json.load(f)
                if s.get("daily_date") != datetime.now().strftime("%Y-%m-%d"):
                    s["daily_pnl"] = 0
                    s["daily_date"] = datetime.now().strftime("%Y-%m-%d")
                for k, v in default.items():
                    if k not in s: s[k] = v
                return s
        except:
            pass
        return default

    def _save(self):
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def _log(self, action, details):
        entry = {"time": datetime.now().isoformat(), "action": action, **details}
        try:
            logs = json.load(open(LOG_FILE)) if os.path.exists(LOG_FILE) else []
        except:
            logs = []
        logs.append(entry)
        with open(LOG_FILE, "w") as f:
            json.dump(logs[-1000:], f, indent=2, ensure_ascii=False)

    def analyze(self, candles_4h, candles_1h):
        """
        Main analysis: 4H for structure, 1H for entry signals
        Returns dict with structure, zones, and signals
        """
        result = {
            "structure": "ranging",
            "zones": {"supply": [], "demand": []},
            "signals": [],
            "volume_ratio": 1.0,
        }

        # === 4H: Market Structure ===
        highs_4h, lows_4h = get_swing_highs_lows(candles_4h, lookback=SWING_LOOKBACK)
        structure = detect_market_structure(candles_4h, highs_4h, lows_4h)
        result["structure"] = structure

        # === 1H: Find Zones ===
        highs_1h, lows_1h = get_swing_highs_lows(candles_1h, lookback=SWING_LOOKBACK)
        zones = find_supply_demand_zones(candles_1h, highs_1h, lows_1h,
                                         lookback=ZONE_LOOKBACK, width_pct=ZONE_WIDTH_PCT)
        result["zones"] = zones

        # === Volume Check ===
        vol_1h = get_volume_ratio(candles_1h)
        result["volume_ratio"] = vol_1h

        # === Check for Trade Setups ===
        current_price = candles_1h[-1]["close"]
        current_idx = len(candles_1h) - 1

        # LONG SETUP: Demand zone approaching or retesting
        for zone in zones.get("demand", [])[:3]:  # Top 3 recent demand zones
            zone_proximity = (zone["zone_high"] - current_price) / current_price * 100
            in_zone = is_price_in_zone(current_price, zone)
            near_zone = zone_proximity < 0.5 and zone_proximity > 0  # within 0.5% above

            if in_zone or near_zone:
                # Check for rejection candles
                rejections = detect_rejection_candles(candles_1h, zone, direction="demand")
                if len(rejections) >= 1 or in_zone:  # If in zone, don't need rejection yet
                    # BoS check
                    bos_confirm = True
                    if BOS_CONFIRMATION:
                        bos_confirm = check_breakeven_of_structure(candles_1h, highs_1h, lows_1h,
                                                                   current_idx, direction="long")

                    # In uptrend or BoS confirmed = good entry
                    if bos_confirm or structure == "uptrend":
                        sl = zone["zone_low"] * 0.995  # 0.5% buffer below zone
                        rr_distance = (current_price - sl) / current_price * 100

                        if rr_distance >= 0.8:  # At least 0.8% risk
                            entry_type = "retest" if in_zone else "approach"
                            result["signals"].append({
                                "direction": "long",
                                "entry": current_price,
                                "stop_loss": sl,
                                "zone": zone,
                                "reason": f"Demand {entry_type} {'+ BoS' if bos_confirm else '+ uptrend'}",
                                "structure": structure,
                                "rr_potential": round(rr_distance, 2),
                            })

        # SHORT SETUP: Supply zone approaching or retesting
        for zone in zones.get("supply", [])[:3]:
            zone_proximity = (current_price - zone["zone_low"]) / current_price * 100
            in_zone = is_price_in_zone(current_price, zone)
            near_zone = zone_proximity < 0.5 and zone_proximity > 0  # within 0.5% below

            if in_zone or near_zone:
                rejections = detect_rejection_candles(candles_1h, zone, direction="supply")
                if len(rejections) >= 1 or in_zone:
                    bos_confirm = True
                    if BOS_CONFIRMATION:
                        bos_confirm = check_breakeven_of_structure(candles_1h, highs_1h, lows_1h,
                                                                   current_idx, direction="short")

                    if bos_confirm or structure == "downtrend":
                        sl = zone["zone_high"] * 1.005  # 0.5% buffer above zone
                        rr_distance = (sl - current_price) / current_price * 100

                        if rr_distance >= 0.8:
                            entry_type = "retest" if in_zone else "approach"
                            result["signals"].append({
                                "direction": "short",
                                "entry": current_price,
                                "stop_loss": sl,
                                "zone": zone,
                                "reason": f"Supply {entry_type} {'+ BoS' if bos_confirm else '+ downtrend'}",
                                "structure": structure,
                                "rr_potential": round(rr_distance, 2),
                            })

        return result

    def run(self, dry_run=True):
        print("=" * 64)
        print("  📦 BTC ZONE TRADING SYSTEM v5 — 1 Setup Trade")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 64)

        # === Market Data ===
        price_data = get_btc_price()
        if not price_data:
            print("❌ No price data"); return

        candles_4h = get_ohlcv("240", 200)
        candles_1h = get_ohlcv("60", 200)
        time.sleep(0.3)

        if len(candles_1h) < 50:
            print(f"⚠️ Not enough data"); return

        current = price_data["last"]
        print(f"\n📊 BTC/THB: {current:,.2f}  ({price_data['change']:+.2f}%)")

        # === Analyze ===
        analysis = self.analyze(candles_4h, candles_1h)

        # === Market Structure ===
        struct = analysis["structure"]
        struct_icon = {"uptrend": "🟢", "downtrend": "🔴", "ranging": "⚪"}.get(struct, "⚪")
        print(f"\n📈 STRUCTURE ({struct_icon} {struct.upper()}):")

        # Show recent zones
        print(f"\n📍 ZONES:")
        for zone_type in ["demand", "supply"]:
            zones = analysis["zones"].get(zone_type, [])[:2]
            for z in zones:
                icon = "🟢" if zone_type == "demand" else "🔴"
                print(f"   {icon} {zone_type.upper()}: {z['zone_low']:,.0f} - {z['zone_high']:,.0f}")
                print(f"      Bounce: {z.get('bounce_pct', z.get('rejection_pct', 0)):+.1f}%")

        # === Volume Check ===
        vol = analysis["volume_ratio"]
        vol_ok = vol >= VOLUME_SPIKE_THRESHOLD
        vol_icon = "✅" if vol_ok else "⚠️"
        print(f"\n📊 VOLUME: {vol:.1f}x {vol_icon}")

        # === Signals ===
        print(f"\n🎯 SIGNALS:")
        if not analysis["signals"]:
            print("   ⚪ No signals currently")
        else:
            for sig in analysis["signals"]:
                dir_icon = "🟢" if sig["direction"] == "long" else "🔴"
                print(f"   {dir_icon} {sig['direction'].upper()}: {sig['reason']}")
                print(f"      Entry: {sig['entry']:,.0f}  SL: {sig['stop_loss']:,.0f}")
                print(f"      R:R potential: {sig['rr_potential']:.1f}%")

        # === Risk Check ===
        balances = get_balances() if not dry_run else {"thb": 100000, "btc": 0}
        total = balances["thb"] + balances["btc"] * current
        risk_check = self._check_risk(total)
        print(f"\n🛡️ Portfolio: {total:,.0f} THB  Heat: {risk_check['heat']:.1f}%")
        print(f"   Trades: {len(self.state['open_trades'])}/{MAX_OPEN_TRADES}  {'✅' if risk_check['can_trade'] else '❌'}")

        for iss in risk_check.get("issues", []):
            print(f"   ⚠️ {iss}")

        # === Check Exits ===
        self._check_exits(current, dry_run)

        # === Entry Decision ===
        print(f"\n{'='*64}")

        if analysis["signals"] and risk_check["can_trade"]:
            sig = analysis["signals"][0]  # Take first signal

            # Position sizing
            risk_amount = total * (RISK_PER_TRADE_PCT / 100)
            sl_distance = abs(sig["entry"] - sig["stop_loss"])
            pos_size = int(min(risk_amount / (sl_distance / sig["entry"]),
                               total * 0.3, balances["thb"]))

            if pos_size >= 500:
                print(f"\n🟢 {sig['direction'].upper()} SIGNAL — {sig['reason']}")
                print(f"   Entry: {sig['entry']:,.0f}  SL: {sig['stop_loss']:,.0f}")
                print(f"   Amount: {pos_size:,} THB")

                if not dry_run:
                    result = place_buy(pos_size, 0, "market", maker=True, bid_price=price_data["bid"])
                    if result.get("error") != 0:
                        result = place_buy(pos_size, 0, "market")
                    if result.get("error") == 0:
                        self.state["open_trades"].append({
                            "entry_price": sig["entry"],
                            "entry_time": datetime.now().isoformat(),
                            "amount_thb": pos_size,
                            "amount_btc": pos_size / sig["entry"],
                            "stop_loss": sig["stop_loss"],
                            "direction": sig["direction"],
                            "zone": sig["zone"],
                            "highest_price": sig["entry"],
                            "structure": sig["structure"],
                        })
                        self._log("BUY", {"price": sig["entry"], "amount": pos_size,
                                         "direction": sig["direction"], "dry_run": dry_run})
            else:
                print(f"\n⚠️ Insufficient balance for position")
        else:
            print(f"\n⚪ NO TRADE — waiting for zone setup")

        print(f"{'='*64}")

        # === Summary ===
        wr = self.state["winning_trades"] / max(1, self.state["total_trades"]) * 100
        print(f"\n📋 Trades: {len(self.state['open_trades'])} open | {self.state['total_trades']} total | {wr:.0f}% win")
        print(f"   Today: {self.state['daily_pnl']:,.0f} | Total: {self.state['total_pnl']:,.0f} THB")

        for i, t in enumerate(self.state["open_trades"]):
            pnl = (current - t["entry_price"]) / t["entry_price"] * 100
            print(f"   #{i+1} {t['direction']} {t['entry_price']:,.0f}→{current:,.0f} ({pnl:+.1f}%)")

        self._save()

    def _check_risk(self, total):
        issues = []
        heat = sum(t.get("amount_thb", 0) for t in self.state.get("open_trades", [])) / total * 100 if total > 0 else 0

        if self.state.get("daily_pnl", 0) < 0:
            loss_pct = abs(self.state["daily_pnl"]) / total * 100
            if loss_pct > DAILY_LOSS_LIMIT_PCT:
                issues.append(f"Daily loss limit: {loss_pct:.1f}%")

        if heat >= MAX_PORTFOLIO_HEAT:
            issues.append(f"Heat maxed: {heat:.1f}%")

        if len(self.state.get("open_trades", [])) >= MAX_OPEN_TRADES:
            issues.append(f"Max trades: {MAX_OPEN_TRADES}")

        # Cooldown check
        recent = self.state.get("recent_results", [])
        if len(recent) >= COOLDOWN_AFTER_LOSSES and all(r < 0 for r in recent[-COOLDOWN_AFTER_LOSSES:]):
            last_loss = self.state.get("last_loss_time", "")
            if last_loss:
                try:
                    lt = datetime.fromisoformat(last_loss)
                    if datetime.now() - lt < timedelta(hours=COOLDOWN_HOURS):
                        issues.append(f"Cooldown: {COOLDOWN_AFTER_LOSSES} losses")
                except:
                    pass

        return {"can_trade": len(issues) == 0, "issues": issues, "heat": heat}

    def _check_exits(self, current_price, dry_run):
        """Check exits for all open trades"""
        for trade in self.state["open_trades"][:]:
            entry = trade["entry_price"]
            sl = trade.get("stop_loss", entry * 0.98)
            direction = trade.get("direction", "long")

            should_exit = False
            reason = ""
            pnl_pct = (current_price - entry) / entry * 100 if direction == "long" else (entry - current_price) / entry * 100

            # Stop loss
            if direction == "long" and current_price <= sl:
                should_exit, reason = True, f"STOP LOSS ({pnl_pct:+.1f}%)"
            elif direction == "short" and current_price >= sl:
                should_exit, reason = True, f"STOP LOSS ({pnl_pct:+.1f}%)"

            # Trailing stop (after 1% profit)
            if not should_exit and pnl_pct > 1.0:
                highest = trade.get("highest_price", entry)
                if direction == "long" and current_price > highest:
                    trade["highest_price"] = current_price
                    highest = current_price

                dd_pct = (highest - current_price) / highest * 100 if direction == "long" else (current_price - highest) / highest * 100
                if dd_pct >= TRAILING_STOP_PCT:
                    should_exit, reason = True, f"TRAILING STOP ({pnl_pct:+.1f}%)"

            # Time-based exit
            if not should_exit:
                entry_time_str = trade.get("entry_time", "")
                if entry_time_str:
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str)
                        held_hours = (datetime.now() - entry_time).total_seconds() / 3600
                        if held_hours >= MAX_HOLD_HOURS and pnl_pct < MIN_PROFIT_TO_HOLD:
                            should_exit, reason = True, f"TIME EXIT — held {held_hours:.0f}h ({pnl_pct:+.1f}%)"
                    except:
                        pass

            if should_exit:
                pnl_thb = trade["amount_btc"] * (current_price - entry) if direction == "long" else trade["amount_btc"] * (entry - current_price)
                print(f"\n  🔴 EXIT: {reason} = {pnl_thb:,.0f} THB")

                if not dry_run:
                    place_sell(trade["amount_btc"])

                self.state["daily_pnl"] += pnl_thb
                self.state["total_pnl"] += pnl_thb
                self.state["total_trades"] += 1
                if pnl_thb > 0:
                    self.state["winning_trades"] += 1
                self.state["recent_results"].append(pnl_thb)
                self.state["recent_results"] = self.state["recent_results"][-50:]

                if pnl_thb < 0:
                    self.state["last_loss_time"] = datetime.now().isoformat()

                self.state["closed_trades"].append({
                    **trade, "exit_price": current_price, "pnl": pnl_thb,
                    "exit_reason": reason, "exit_time": datetime.now().isoformat()
                })
                self.state["closed_trades"] = self.state["closed_trades"][-200:]
                self.state["open_trades"].remove(trade)
                self._log("SELL", {"reason": reason, "pnl": pnl_thb, "dry_run": dry_run})


# ══════════════════════════════════════════════
#  BACKTEST MODULE
# ══════════════════════════════════════════════

def run_backtest():
    """
    Full backtest of the zone trading strategy.
    Uses historical Bitkub data to simulate trades.
    """
    import requests
    from datetime import datetime

    print("=" * 64)
    print("  📊 BTC ZONE STRATEGY — BACKTEST")
    print("=" * 64)

    # ─── Load Historical Data ───
    DATA_SOURCE = os.environ.get("DATA_SOURCE", "bitkub")  # "binance" or "bitkub"

    if DATA_SOURCE == "binance":
        print(f"\n📂 Loading Binance data from /tmp...")
        with open("/tmp/btc_1h.json") as f:
            candles_1h = json.load(f)
        with open("/tmp/btc_4h.json") as f:
            candles_4h = json.load(f)
        print(f"   1H candles: {len(candles_1h)} | 4H candles: {len(candles_4h)}")
        print(f"   Period: {datetime.fromtimestamp(candles_1h[0]['time'])} → {datetime.fromtimestamp(candles_1h[-1]['time'])}")
    else:
        print(f"\n📥 Downloading Bitkub data...")
        now = int(time.time())
        DAYS = 60
        url_1h = f"{HOST}/tradingview/history?symbol=BTC_THB&resolution=60&from={now - DAYS*86400}&to={now}"
        data_1h = requests.get(url_1h, timeout=20).json()
        url_4h = f"{HOST}/tradingview/history?symbol=BTC_THB&resolution=240&from={now - DAYS*86400}&to={now}"
        data_4h = requests.get(url_4h, timeout=20).json()
        if data_1h.get("s") != "ok" or data_4h.get("s") != "ok":
            print("❌ Failed to download data"); return
        candles_1h = [{"time": data_1h["t"][i], "open": float(data_1h["o"][i]),
                       "high": float(data_1h["h"][i]), "low": float(data_1h["l"][i]),
                       "close": float(data_1h["c"][i]), "volume": float(data_1h["v"][i])}
                      for i in range(len(data_1h["t"]))]
        candles_4h = [{"time": data_4h["t"][i], "open": float(data_4h["o"][i]),
                       "high": float(data_4h["h"][i]), "low": float(data_4h["l"][i]),
                       "close": float(data_4h["c"][i]), "volume": float(data_4h["v"][i])}
                      for i in range(len(data_4h["t"]))]
        print(f"   1H candles: {len(candles_1h)} | 4H candles: {len(candles_4h)}")
        print(f"   Period: {datetime.fromtimestamp(candles_1h[0]['time'])} → {datetime.fromtimestamp(candles_1h[-1]['time'])}")

    # ─── Backtest Parameters ───
    INITIAL_CAPITAL = 100_000  # THB
    RISK_PCT = 2.0             # % risk per trade
    TRAILING_STOP_PCT = 1.0    # trailing stop % (optimal from sweep)
    MAX_HOLD_HOURS = 96        # 4 days max
    ZONE_LOOKBACK = 20
    ZONE_WIDTH_PCT = 3.0       # optimal from sweep
    REJECTION_THRESHOLD = 0.3  # optimal from sweep
    SWING_LOOKBACK = 5
    STRUCTURE_FILTER = "trend_only"  # optimal from sweep

    capital = INITIAL_CAPITAL
    equity_curve = []
    trades = []
    open_trades = []
    wins = losses = 0
    max_dd = 0
    peak_capital = INITIAL_CAPITAL

    print(f"\n⚙️  Params: Risk={RISK_PCT}%/trade, Trailing={TRAILING_STOP_PCT}%, MaxHold={MAX_HOLD_HOURS}h")

    # ─── Convert 4H structure to 1H indices ───
    # Map 4H candle times to 1H indices for alignment
    h4_time_to_idx = {c["time"]: i for i, c in enumerate(candles_4h)}

    # ─── Pre-compute swing points for all 1H candles (rolling) ───
    print("\n📐 Pre-computing zones (rolling window)...")

    # We'll process each 1H candle and check for zone setups
    for i in range(SWING_LOOKBACK + ZONE_LOOKBACK + 5, len(candles_1h) - 1):
        current_candle = candles_1h[i]
        current_price = current_candle["close"]
        current_time = current_candle["time"]

        # ─── Get relevant 4H structure for this candle ───
        # Find the 4H candle that covers this 1H time
        h4_idx = 0
        for j, hc in enumerate(candles_4h):
            if hc["time"] <= current_time < hc["time"] + 240 * 60:
                h4_idx = j
                break

        # Get 4H swing data (up to current 4H candle)
        h4_subset = candles_4h[:h4_idx+1]
        highs_4h, lows_4h = get_swing_highs_lows(h4_subset, lookback=SWING_LOOKBACK)
        structure = detect_market_structure(h4_subset, highs_4h, lows_4h)

        # Get 1H swing data for zone detection (lookback window)
        window_start = max(0, i - ZONE_LOOKBACK - SWING_LOOKBACK)
        h1_window = candles_1h[window_start:i+1]
        highs_1h, lows_1h = get_swing_highs_lows(h1_window, lookback=SWING_LOOKBACK)

        # Find zones from this window
        zones = find_supply_demand_zones(h1_window, highs_1h, lows_1h,
                                          lookback=min(ZONE_LOOKBACK, len(h1_window)-5),
                                          width_pct=ZONE_WIDTH_PCT)

        # ─── Check existing open trades for exits ───
        for trade in open_trades[:]:
            entry = trade["entry_price"]
            direction = trade["direction"]
            sl = trade["stop_loss"]
            entry_time = trade["entry_time"]
            highest = trade.get("highest_price", entry)

            held_hours = (current_time - entry_time) / 3600
            pnl_pct = (current_price - entry) / entry * 100 if direction == "long" else (entry - current_price) / entry * 100

            exit_reason = None

            # Stop loss
            if direction == "long" and current_price <= sl:
                exit_reason = "SL"
            elif direction == "short" and current_price >= sl:
                exit_reason = "SL"

            # Trailing stop (after 1% profit)
            if not exit_reason and pnl_pct > 1.0:
                if direction == "long" and current_price > highest:
                    trade["highest_price"] = current_price
                    highest = current_price
                dd = (highest - current_price) / highest * 100 if direction == "long" else (current_price - highest) / highest * 100
                if dd >= TRAILING_STOP_PCT:
                    exit_reason = "TS"

            # Time exit
            if not exit_reason and held_hours >= MAX_HOLD_HOURS and pnl_pct < 0.3:
                exit_reason = "TIME"

            if exit_reason:
                pnl = trade["amount_btc"] * (current_price - entry) if direction == "long" else trade["amount_btc"] * (entry - current_price)
                capital += pnl
                trades.append({**trade, "exit_price": current_price, "exit_time": current_time,
                                "pnl": pnl, "pnl_pct": pnl_pct, "exit_reason": exit_reason})
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                open_trades.remove(trade)

        # ─── Check for new entries ───
        if len(open_trades) < 3:  # MAX_OPEN_TRADES
            for zone_type in ["demand", "supply"]:
                for zone in zones.get(zone_type, [])[:1]:  # Only top zone
                    if zone_type == "demand":
                        zone_proximity = (zone["zone_high"] - current_price) / current_price * 100
                        in_zone = is_price_in_zone(current_price, zone)
                        near_zone = 0 < zone_proximity < 0.5
                    else:
                        zone_proximity = (current_price - zone["zone_low"]) / current_price * 100
                        in_zone = is_price_in_zone(current_price, zone)
                        near_zone = 0 < zone_proximity < 0.5  # optimal near_thresh

                    if not (in_zone or near_zone):
                        continue

                    # Check zone strength (bounce/rejection %)
                    zone_strength = zone.get("bounce_pct", zone.get("rejection_pct", 0))
                    if zone_strength < REJECTION_THRESHOLD:
                        continue

                    # Structure filter (optimal: trend_only)
                    if STRUCTURE_FILTER == "bos_required":
                        rel_idx = i - window_start
                        bos_ok = check_breakeven_of_structure(h1_window, highs_1h, lows_1h,
                                                            rel_idx,
                                                            direction="long" if zone_type == "demand" else "short")
                        if not bos_ok:
                            continue
                    elif STRUCTURE_FILTER == "trend_only":
                        if not (structure in ("uptrend" if zone_type == "demand" else "downtrend")):
                            continue
                    # "any" → no filter

                    # Direction
                    direction = "long" if zone_type == "demand" else "short"

                    # Stop loss: opposite side of zone + 0.5% buffer
                    if direction == "long":
                        sl = zone["zone_low"] * 0.995
                        rr = (current_price - sl) / current_price * 100
                    else:
                        sl = zone["zone_high"] * 1.005
                        rr = (sl - current_price) / current_price * 100

                    if rr < 0.5:  # Min risk (optimal: 0.5%)
                        continue

                    # Position sizing
                    risk_amount = capital * (RISK_PCT / 100)
                    pos_size = min(risk_amount / (rr / 100), 2000)  # cap per trade at 2000 THB

                    if pos_size < 500:
                        continue

                    # Entry price (use next candle open to simulate realistic fill)
                    if i + 1 < len(candles_1h):
                        entry_price = candles_1h[i+1]["open"]
                    else:
                        entry_price = current_price

                    sl_distance = abs(entry_price - sl)
                    amount_btc = pos_size / entry_price

                    open_trades.append({
                        "entry_price": entry_price,
                        "entry_time": current_time,
                        "stop_loss": sl,
                        "direction": direction,
                        "zone": zone,
                        "amount_btc": amount_btc,
                        "highest_price": entry_price,
                        "structure": structure,
                    })
                    break  # Only one new trade per candle

        # ─── Track equity ───
        open_pnl = sum(
            (current_price - t["entry_price"]) / t["entry_price"] * t["amount_btc"] if t["direction"] == "long"
            else (t["entry_price"] - current_price) / t["entry_price"] * t["amount_btc"]
            for t in open_trades
        )
        total_equity = capital + open_pnl
        equity_curve.append({"time": current_time, "equity": total_equity})
        peak_capital = max(peak_capital, total_equity)
        dd = (peak_capital - total_equity) / peak_capital * 100
        max_dd = max(max_dd, dd)

    # ─── Close any remaining trades at last price ───
    final_price = candles_1h[-1]["close"]
    for trade in open_trades[:]:
        entry = trade["entry_price"]
        direction = trade["direction"]
        pnl = trade["amount_btc"] * (final_price - entry) if direction == "long" else trade["amount_btc"] * (entry - final_price)
        capital += pnl
        trades.append({**trade, "exit_price": final_price, "exit_time": candles_1h[-1]["time"],
                        "pnl": pnl, "pnl_pct": 0, "exit_reason": "END"})
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        open_trades.remove(trade)

    # ─── Summary Statistics ───
    total_trades = len(trades)
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0
    total_pnl = capital - INITIAL_CAPITAL
    profit_factor = wins / max(1, losses)

    # Sharpe Ratio (simplified)
    if len(trades) > 1:
        returns = [t["pnl"] / INITIAL_CAPITAL * 100 for t in trades]
        mean_ret = sum(returns) / len(returns)
        std_ret = (sum((r - mean_ret)**2 for r in returns) / len(returns)) ** 0.5
        sharpe = mean_ret / std_ret * (252 ** 0.5) if std_ret > 0 else 0
    else:
        sharpe = 0

    # Best/Worst
    best_trade = max(t["pnl"] for t in trades) if trades else 0
    worst_trade = min(t["pnl"] for t in trades) if trades else 0
    avg_win = sum(t["pnl"] for t in trades if t["pnl"] > 0) / max(1, wins)
    avg_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0) / max(1, losses)

    # ─── Print Results ───
    print("\n" + "=" * 64)
    print("  📊 BACKTEST RESULTS")
    print("=" * 64)
    print(f"\n  📅 Period: {datetime.fromtimestamp(candles_1h[0]['time']).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(candles_1h[-1]['time']).strftime('%Y-%m-%d')}")
    print(f"  💰 Initial: {INITIAL_CAPITAL:,.0f} THB  →  Final: {capital:,.0f} THB")
    print(f"\n  ┌─────────────────────────────────────┐")
    print(f"  │  Total PnL:    {total_pnl:+,.0f} THB ({total_pnl/INITIAL_CAPITAL*100:+.1f}%)        │")
    print(f"  │  Win Rate:     {win_rate:.0f}% ({wins}W/{losses}L)         │")
    print(f"  │  Profit Factor: {profit_factor:.2f}                    │")
    print(f"  │  Sharpe Ratio: {sharpe:.2f}                       │")
    print(f"  │  Max Drawdown: {max_dd:.1f}%                        │")
    print(f"  └─────────────────────────────────────┘")
    print(f"\n  📈 Trade Stats:")
    print(f"     Best trade:   {best_trade:+,.0f} THB")
    print(f"     Worst trade:  {worst_trade:+,.0f} THB")
    print(f"     Avg win:      {avg_win:+,.0f} THB")
    print(f"     Avg loss:     {avg_loss:+,.0f} THB")
    print(f"\n  📋 Exit Reasons:")
    exit_reasons = {}
    for t in trades:
        r = t.get("exit_reason", "END")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1
    for reason, count in sorted(exit_reasons.items(), key=lambda x: -x[1]):
        print(f"     {reason}: {count}")

    # Equity curve (text-based)
    if equity_curve:
        print(f"\n  📉 Equity Curve (last 20 checkpoints):")
        step = max(1, len(equity_curve) // 20)
        checkpoints = equity_curve[::step][:21]
        min_eq = min(e["equity"] for e in checkpoints)
        max_eq = max(e["equity"] for e in checkpoints)
        range_eq = max_eq - min_eq if max_eq > min_eq else 1

        for cp in checkpoints:
            bar_len = int((cp["equity"] - min_eq) / range_eq * 30)
            dt = datetime.fromtimestamp(cp["time"]).strftime("%m/%d")
            print(f"     {dt} {'█' * bar_len}{'░' * (30-bar_len)} {cp['equity']:,.0f}")

    print(f"\n  Total trades: {total_trades} | Open: {len(open_trades)}")
    print("=" * 64)


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
if __name__ == "__main__":
    cmds = {
        "analyze": "Analyze (no trade)",
        "dry-run": "Simulate",
        "live": "REAL trading",
        "status": "Show positions",
        "log": "Trade log",
        "backtest": "Backtest strategy",
        "reset": "Reset state",
    }

    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print("BTC Zone Trading System v5")
        print("=" * 40)
        for c, d in cmds.items():
            print(f"  {c:12s}  {d}")
        sys.exit(1)

    cmd = sys.argv[1]
    scanner = ZoneScanner()

    if cmd in ("analyze", "dry-run"):
        scanner.run(dry_run=True)
    elif cmd == "live":
        if not API_KEY or not API_SECRET:
            print("❌ Set API_KEY and API_SECRET"); sys.exit(1)
        print("⚠️ LIVE MODE — 5s to cancel...")
        try: time.sleep(5)
        except KeyboardInterrupt: print("\nCancelled."); sys.exit(0)
        scanner.run(dry_run=False)
    elif cmd == "status":
        p = get_btc_price()
        if p:
            print(f"BTC: {p['last']:,.2f}")
            for i, t in enumerate(scanner.state.get("open_trades", [])):
                pnl = (p["last"] - t["entry_price"]) / t["entry_price"] * 100
                print(f"\n  Trade #{i+1} [{t.get('direction','')}]")
                print(f"    Entry:  {t['entry_price']:,.0f}")
                print(f"    Now:    {p['last']:,.0f}  ({pnl:+.1f}%)")
                print(f"    SL:     {t.get('stop_loss', 0):,.0f}")
        if not scanner.state.get("open_trades"):
            print("No open trades.")
    elif cmd == "log":
        if os.path.exists(LOG_FILE):
            for e in json.load(open(LOG_FILE))[-20:]:
                print(f"  {e['time'][:19]} {e['action']:12s} {json.dumps({k:v for k,v in e.items() if k not in ['time','action']})}")
    elif cmd == "reset":
        for f in [STATE_FILE, LOG_FILE]:
            if os.path.exists(f): os.remove(f)
        print("✅ Reset done.")
    elif cmd == "backtest":
        run_backtest()
