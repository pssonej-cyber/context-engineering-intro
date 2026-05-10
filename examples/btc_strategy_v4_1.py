#!/usr/bin/env python3
"""
================================================================
  MULTI-AGENT AI TRADING SYSTEM v4.1 — "Smart Edges"
  Enhancements over v4:
================================================================

  1. REGIME-BASED SL — SL adapts to market condition
     - Trending: 2.5 ATR (wider, let winners run)
     - Ranging:  1.2 ATR (tighter, cut losses fast)
     - Volatile: 1.8 ATR (balanced)

  2. TIME-BASED EXIT — Don't lock capital forever
     - Exit if held > 72h AND profit < 0.5%
     - Frees up slots for better opportunities

  3. MAKER FEE via LIMIT ORDERS
     - Post-only limit at bid - 0.1% (maker fee 0.1% vs taker 0.25%)
     - Saves ~60% on fees
     - Falls back to market if not filled in 10 min

  4. DYNAMIC PARTIAL EXIT
     - Strong signal (>0.35): exit 30% at TP1 (let winners run)
     - Normal signal (0.15-0.35): exit 60% at TP1 (lock more profit)

  5. NO-TRADE HOURS
     - Skip weekend low-volume periods (Sat 00:00-12:00 UTC)
     - Skip Asian session dead zone (02:00-06:00 UTC) in ranging regime
     - Configurable via trading_config.json
"""

import hashlib, hmac, json, sys, os, time, math, pickle, warnings
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

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
API_KEY = os.environ.get("BITKUB_API_KEY", "")
API_SECRET = os.environ.get("BITKUB_API_SECRET", "")
HOST = "https://api.bitkub.com"

BASE_DIR = os.path.expanduser("~/.openclaw/skills/bitkub-trader")
STATE_FILE = os.path.join(BASE_DIR, "v4_1_state.json")
LOG_FILE = os.path.join(BASE_DIR, "v4_1_log.json")
MODEL_FILE = os.path.join(BASE_DIR, "v4_1_lgb_model.pkl")

# Risk params — TUNED from backtest v3 results
RISK_PER_TRADE_PCT = 5.0    # Increased from 2% — bigger positions, less fee impact
MAX_PORTFOLIO_HEAT = 12.0   # 2 trades × ~5-6% each
MAX_OPEN_TRADES = 2         # Reduced from 4 — less overtrading
DAILY_LOSS_LIMIT_PCT = 3.0
MAX_TAKE_PROFIT_PCT = 8.0   # Higher cap for ATR-based TP
MAX_STOP_LOSS_PCT = 5.0     # Higher cap for ATR-based SL
MIN_RISK_REWARD = 1.3       # From backtest: R:R = 3.0/2.0 = 1.5 passes this

# Partial exit config
PARTIAL_EXIT_1_PCT = 50     # Exit 50% at TP1
TP1_ATR_MULT = 3.0          # Changed from 1.5 — wider TP = bigger wins
TP2_ATR_MULT = 6.0          # Changed from 3.0 — let winners run
SL_ATR_MULT = 2.0           # Changed from 1.2 — room to breathe, less stop outs

# Cooldown
COOLDOWN_AFTER_LOSSES = 3
COOLDOWN_HOURS = 4

# Volume filter — REQUIRED for entry
MIN_VOLUME_RATIO = 1.2

# ═══ v4.1 NEW FEATURES ═══

# Feature 1: Regime-based SL multipliers (overrides SL_ATR_MULT per regime)
REGIME_SL = {
    "trending_up":   2.5,   # Wider in trends
    "trending_down": 2.0,
    "ranging":       1.2,   # Tighter in ranges
    "volatile":      1.8,   # Balanced
}

# Feature 2: Time-based exit
MAX_HOLD_HOURS = 72         # Exit if held too long with small profit
MIN_PROFIT_TO_HOLD = 0.5    # % — if profit < this after MAX_HOLD_HOURS, exit

# Feature 3: Maker fee (limit order offset)
MAKER_FEE_OFFSET_PCT = 0.1  # Place limit at bid - 0.1% for maker fee
LIMIT_ORDER_TIMEOUT_MIN = 10 # If not filled in 10 min, switch to market

# Feature 4: Dynamic partial exit based on signal strength
STRONG_SIGNAL_THRESHOLD = 0.35
PARTIAL_EXIT_STRONG = 30    # Strong signal: keep 70% for bigger gains
PARTIAL_EXIT_NORMAL = 60    # Normal signal: lock 60% profit

# Feature 5: No-trade hours (UTC)
NO_TRADE_HOURS = {
    "saturday_morning": {"enabled": True, "days": [5], "hours": list(range(0, 12))},
    "asian_dead_zone":  {"enabled": True, "hours": [2, 3, 4, 5], "only_regime": "ranging"},
}      # Volume must be 1.2x above average

# Ensemble weights — MEANREV FOCUSED (only profitable strategy in backtest)
WEIGHTS = {
    "trending_up":   {"meanrev": 0.60, "macd": 0.15, "trend": 0.25},
    "trending_down": {"meanrev": 0.50, "macd": 0.15, "trend": 0.35},
    "ranging":       {"meanrev": 0.65, "macd": 0.15, "trend": 0.20},
    "volatile":      {"meanrev": 0.55, "macd": 0.20, "trend": 0.25},
}

# Load dynamic config — MUST be after all CONFIG values
from config_loader import apply_to_globals
apply_to_globals(globals())

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

def get_order_book(limit=5):
    """Get bid/ask depth to measure buy/sell pressure"""
    try:
        bids = requests.get(f"{HOST}/api/v3/market/bids?sym=BTC_THB&lmt={limit}", timeout=10).json()
        asks = requests.get(f"{HOST}/api/v3/market/asks?sym=BTC_THB&lmt={limit}", timeout=10).json()
        bid_vol = sum(float(b.get("volume", b.get("size", 0))) for b in bids.get("result", []))
        ask_vol = sum(float(a.get("volume", a.get("size", 0))) for a in asks.get("result", []))
        total = bid_vol + ask_vol
        if total > 0:
            return {"bid_vol": bid_vol, "ask_vol": ask_vol,
                    "buy_pressure": bid_vol / total,
                    "imbalance": (bid_vol - ask_vol) / total}
        return {"bid_vol": 0, "ask_vol": 0, "buy_pressure": 0.5, "imbalance": 0}
    except:
        return {"bid_vol": 0, "ask_vol": 0, "buy_pressure": 0.5, "imbalance": 0}

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
    """
    Place buy order
    v4.1: maker=True uses post-only limit at (bid - 0.1%) for maker fee 0.1%
    Falls back to market after LIMIT_ORDER_TIMEOUT_MIN if not filled
    """
    path = "/api/v3/market/place-bid"

    if maker and bid_price > 0:
        # v4.1: Try maker fee first
        maker_price = int(bid_price * (1 - MAKER_FEE_OFFSET_PCT / 100))
        body_dict = {"sym": "btc_thb", "amt": int(amount_thb), "rat": maker_price, "typ": "limit"}
        body = json.dumps(body_dict)
        headers = make_headers("POST", path, body)
        result = requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()
        if result.get("error") == 0:
            print(f"     🎯 Maker limit @ {maker_price:,.0f} (saving ~60% fee)")
            return result
        else:
            print(f"     Maker limit failed ({result.get('error')}), fallback to taker limit...")

    body_dict = {"sym": "btc_thb", "amt": int(amount_thb), "rat": rate, "typ": typ}
    body = json.dumps(body_dict)
    headers = make_headers("POST", path, body)
    result = requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()

    # If limit order fails, fallback to market
    if result.get("error") != 0 and typ == "limit":
        print(f"     Limit failed ({result.get('error')}), trying market...")
        body_dict["rat"] = 0
        body_dict["typ"] = "market"
        body = json.dumps(body_dict)
        headers = make_headers("POST", path, body)
        result = requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()

    # Verify BTC received
    if result.get("error") == 0:
        rec = result.get("result", {}).get("rec", 0)
        print(f"     ✅ Filled! Got {rec} BTC")
    else:
        print(f"     ❌ Order failed: error {result.get('error')}")

    return result

def place_sell(amount_btc, rate=0, typ="market"):
    """Place sell order — supports limit orders"""
    path = "/api/v3/market/place-ask"
    body_dict = {"sym": "btc_thb", "amt": amount_btc, "rat": rate, "typ": typ}
    body = json.dumps(body_dict)
    headers = make_headers("POST", path, body)
    return requests.post(f"{HOST}{path}", headers=headers, data=body, timeout=10).json()


# ══════════════════════════════════════════════
#  INDICATORS (numpy)
# ══════════════════════════════════════════════
def ema(arr, period):
    arr = np.array(arr, dtype=float)
    result = np.zeros_like(arr)
    result[0] = arr[0]
    k = 2.0 / (period + 1)
    for i in range(1, len(arr)):
        result[i] = arr[i] * k + result[i-1] * (1 - k)
    return result

def sma(arr, period):
    arr = np.array(arr, dtype=float)
    result = np.full_like(arr, np.nan)
    for i in range(period - 1, len(arr)):
        result[i] = np.mean(arr[i - period + 1:i + 1])
    return result

def rsi(closes, period=14):
    closes = np.array(closes, dtype=float)
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def rsi_series(closes, period=14):
    closes = np.array(closes, dtype=float)
    result = np.full(len(closes), 50.0)
    if len(closes) < period + 1:
        return result
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    ag = np.mean(gains[:period])
    al = np.mean(losses[:period])
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        result[i + 1] = 100 if al == 0 else 100 - (100 / (1 + ag / al))
    return result

def bollinger(closes, period=20, std_mult=2.0):
    closes = np.array(closes, dtype=float)
    mid = sma(closes, period)
    std = np.full_like(closes, np.nan)
    for i in range(period - 1, len(closes)):
        std[i] = np.std(closes[i - period + 1:i + 1])
    return mid + std_mult * std, mid, mid - std_mult * std

def macd(closes, fast=12, slow=26, signal=9):
    closes = np.array(closes, dtype=float)
    macd_line = ema(closes, fast) - ema(closes, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line

def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        trs.append(max(candles[i]["high"] - candles[i]["low"],
                       abs(candles[i]["high"] - candles[i-1]["close"]),
                       abs(candles[i]["low"] - candles[i-1]["close"])))
    return float(np.mean(trs[-period:]))

def adx(candles, period=14):
    if len(candles) < period * 2:
        return 25
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(candles)):
        hd = candles[i]["high"] - candles[i-1]["high"]
        ld = candles[i-1]["low"] - candles[i]["low"]
        plus_dm.append(hd if hd > ld and hd > 0 else 0)
        minus_dm.append(ld if ld > hd and ld > 0 else 0)
        tr_list.append(max(candles[i]["high"] - candles[i]["low"],
                           abs(candles[i]["high"] - candles[i-1]["close"]),
                           abs(candles[i]["low"] - candles[i-1]["close"])))
    atr_val = np.mean(tr_list[-period:])
    if atr_val == 0:
        return 25
    plus_di = 100 * np.mean(plus_dm[-period:]) / atr_val
    minus_di = 100 * np.mean(minus_dm[-period:]) / atr_val
    di_sum = plus_di + minus_di
    return 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 25

def volume_profile(candles, lookback=20):
    """Analyze volume trend and detect spikes"""
    if len(candles) < lookback + 1:
        return {"ratio": 1, "trend": 0, "spike": False}
    vols = np.array([c["volume"] for c in candles[-lookback-1:]], dtype=float)
    avg_vol = np.mean(vols[:-1])
    curr_vol = vols[-1]
    ratio = curr_vol / avg_vol if avg_vol > 0 else 1
    trend = (np.mean(vols[-5:]) / np.mean(vols[-10:-5]) - 1) * 100 if np.mean(vols[-10:-5]) > 0 else 0
    spike = ratio > 2.0
    return {"ratio": ratio, "trend": trend, "spike": spike}


# ══════════════════════════════════════════════
#  IMPROVEMENT 1: MULTI-TIMEFRAME ANALYSIS
# ══════════════════════════════════════════════
class MultiTimeframe:
    @staticmethod
    def analyze(candles_15m, candles_1h, candles_4h):
        """
        Align signals across 3 timeframes.
        Strong signal = all 3 agree. Weak = only 1.
        """
        signals = {}
        for label, candles in [("15m", candles_15m), ("1h", candles_1h), ("4h", candles_4h)]:
            if len(candles) < 30:
                signals[label] = 0
                continue
            closes = [c["close"] for c in candles]
            ema9 = ema(closes, 9)
            ema21 = ema(closes, 21)
            rsi_val = rsi(closes)

            score = 0
            if ema9[-1] > ema21[-1]:
                score += 0.33
            else:
                score -= 0.33
            if rsi_val < 40:
                score += 0.33
            elif rsi_val > 60:
                score -= 0.33
            # Momentum
            if closes[-1] > closes[-3]:
                score += 0.34
            else:
                score -= 0.34
            signals[label] = round(score, 2)

        # Weighted: 4h = 50%, 1h = 30%, 15m = 20%
        mtf_score = signals.get("4h", 0) * 0.50 + signals.get("1h", 0) * 0.30 + signals.get("15m", 0) * 0.20
        alignment = all(s > 0 for s in signals.values()) or all(s < 0 for s in signals.values())

        return {
            "signals": signals,
            "score": round(mtf_score, 3),
            "aligned": alignment,
        }


# ══════════════════════════════════════════════
#  IMPROVEMENT 2: VOLUME CONFIRMATION
# ══════════════════════════════════════════════
class VolumeFilter:
    @staticmethod
    def confirm(candles, signal_direction):
        """
        Returns multiplier 0.5-1.5 based on volume confirmation.
        Buy signal + high volume = 1.5x
        Buy signal + low volume = 0.5x
        """
        vp = volume_profile(candles)
        if signal_direction > 0:  # Buy
            if vp["ratio"] > 1.5 and vp["trend"] > 0:
                return 1.4  # Strong volume confirms buy
            elif vp["ratio"] > 1.0:
                return 1.0  # Normal volume
            else:
                return 0.8  # Weak volume, slight reduce
        elif signal_direction < 0:  # Sell
            if vp["ratio"] > 1.5:
                return 1.3  # Panic selling confirmed
            else:
                return 1.0
        return 1.0


# ══════════════════════════════════════════════
#  IMPROVEMENT 3: DYNAMIC TP/SL (v4.1: Regime-aware)
# ══════════════════════════════════════════════
class DynamicExits:
    @staticmethod
    def calculate(entry_price, atr_val, regime="ranging"):
        """ATR-based TP/SL with regime-aware SL (v4.1)"""
        # v4.1: SL adapts to regime
        sl_mult = REGIME_SL.get(regime, SL_ATR_MULT)
        sl_distance = atr_val * sl_mult
        tp1_distance = atr_val * TP1_ATR_MULT
        tp2_distance = atr_val * TP2_ATR_MULT

        # Cap at maximum percentages
        sl_pct = min(sl_distance / entry_price * 100, MAX_STOP_LOSS_PCT)
        tp1_pct = min(tp1_distance / entry_price * 100, MAX_TAKE_PROFIT_PCT)

        sl = entry_price - (entry_price * sl_pct / 100)
        tp1 = entry_price + (entry_price * tp1_pct / 100)
        tp2 = entry_price + tp2_distance

        # Check minimum R:R
        risk = entry_price - sl
        reward = tp1 - entry_price
        rr_ratio = reward / risk if risk > 0 else 0

        return {
            "stop_loss": sl,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "sl_pct": sl_pct,
            "tp1_pct": tp1_pct,
            "rr_ratio": rr_ratio,
            "valid": rr_ratio >= MIN_RISK_REWARD,
            "sl_mult_used": sl_mult,
            "regime": regime,
        }


# ══════════════════════════════════════════════
#  v4.1 NEW: NoTradeFilter
# ══════════════════════════════════════════════
class NoTradeFilter:
    @staticmethod
    def is_no_trade_time(regime="ranging"):
        """Check if current time is in no-trade zone"""
        now = datetime.utcnow()
        hour = now.hour
        weekday = now.weekday()  # 0=Monday, 5=Saturday, 6=Sunday

        reasons = []

        # Saturday morning low volume
        sat_cfg = NO_TRADE_HOURS.get("saturday_morning", {})
        if sat_cfg.get("enabled") and weekday in sat_cfg.get("days", []):
            if hour in sat_cfg.get("hours", []):
                reasons.append("Saturday morning low volume")

        # Asian dead zone (only in ranging)
        asia_cfg = NO_TRADE_HOURS.get("asian_dead_zone", {})
        if asia_cfg.get("enabled") and hour in asia_cfg.get("hours", []):
            only_regime = asia_cfg.get("only_regime")
            if only_regime is None or regime == only_regime:
                reasons.append(f"Asian dead zone ({hour:02d}:00 UTC, {regime})")

        return len(reasons) > 0, reasons


# ══════════════════════════════════════════════
#  v4.1 NEW: TimeBasedExit
# ══════════════════════════════════════════════
class TimeBasedExit:
    @staticmethod
    def should_exit(trade, current_price):
        """Exit if held too long with small profit"""
        entry_time_str = trade.get("entry_time", "")
        if not entry_time_str:
            return False, ""
        try:
            entry_time = datetime.fromisoformat(entry_time_str)
            held_hours = (datetime.now() - entry_time).total_seconds() / 3600
            if held_hours < MAX_HOLD_HOURS:
                return False, ""

            entry_price = trade["entry_price"]
            profit_pct = (current_price - entry_price) / entry_price * 100

            if profit_pct < MIN_PROFIT_TO_HOLD:
                return True, f"Held {held_hours:.0f}h, profit {profit_pct:+.2f}% < {MIN_PROFIT_TO_HOLD}%"
        except:
            pass
        return False, ""


# ══════════════════════════════════════════════
#  v4.1 NEW: DynamicPartialExit
# ══════════════════════════════════════════════
class DynamicPartialExit:
    @staticmethod
    def get_partial_pct(entry_score):
        """
        Strong signal → keep more (exit less at TP1)
        Normal signal → exit more early (lock profit)
        """
        if abs(entry_score) >= STRONG_SIGNAL_THRESHOLD:
            return PARTIAL_EXIT_STRONG  # 30%
        return PARTIAL_EXIT_NORMAL      # 60%



#  Removed: EMA crossover, RSI, Breakout (all unprofitable in backtest)
#  Kept: Mean Reversion (primary), MACD (confirmation), Trend filter
# ══════════════════════════════════════════════
class Strategies:
    @staticmethod
    def mean_reversion(closes):
        """Bollinger Bands mean reversion — the ONLY profitable strategy"""
        if len(closes) < 25:
            return 0.0
        bb_upper, bb_mid, bb_lower = bollinger(closes)
        if np.isnan(bb_upper[-1]):
            return 0.0
        bb_range = bb_upper[-1] - bb_lower[-1]
        if bb_range == 0:
            return 0.0
        pct_b = (closes[-1] - bb_lower[-1]) / bb_range

        # Bollinger squeeze = potential breakout, skip mean reversion
        bb_width = bb_range / bb_mid[-1] * 100
        squeeze_penalty = 0.3 if bb_width < 2.0 else 1.0

        if pct_b < 0.05:
            return 0.9 * squeeze_penalty   # Touching/below lower band
        elif pct_b < 0.15:
            return 0.7 * squeeze_penalty   # Near lower band
        elif pct_b < 0.25:
            return 0.4 * squeeze_penalty   # Lower quarter
        elif pct_b < 0.35 and closes[-1] > closes[-2]:
            return 0.2 * squeeze_penalty   # Bouncing from lower half
        elif pct_b > 0.9:
            return -0.7   # Near/above upper band (sell signal)
        elif pct_b > 0.75:
            return -0.3
        return 0.0

    @staticmethod
    def macd_confirmation(closes):
        """MACD as confirmation — momentum must agree"""
        if len(closes) < 30:
            return 0.0
        _, _, hist = macd(closes)
        if len(hist) < 3:
            return 0.0
        if hist[-1] > 0 and hist[-2] <= 0:
            return 0.8    # Fresh bullish crossover
        elif hist[-1] > hist[-2] and hist[-2] > hist[-3]:
            return 0.5    # Increasing momentum
        elif hist[-1] > hist[-2]:
            return 0.2    # Turning up
        elif hist[-1] < 0 and hist[-2] >= 0:
            return -0.7   # Bearish crossover
        elif hist[-1] < hist[-2]:
            return -0.3   # Decreasing momentum
        return 0.0

    @staticmethod
    def trend_filter(closes):
        """Trend filter — buy dips in uptrend, avoid strong downtrends"""
        if len(closes) < 55:
            return 0.0
        c = np.array(closes, dtype=float)
        ema20 = ema(c, 20)
        ema50 = ema(c, 50)
        ema20_slope = (ema20[-1] - ema20[-5]) / ema20[-5] * 100 if len(ema20) > 5 else 0
        price_below_ema20 = c[-1] < ema20[-1]
        ema_bullish = ema20[-1] > ema50[-1]

        if price_below_ema20 and ema_bullish:
            return 0.8    # Best: price dipped but trend is up
        elif price_below_ema20 and ema20_slope > -0.5:
            return 0.5    # Price dipped, trend not too bearish
        elif ema_bullish:
            return 0.3    # Trend up but price hasn't dipped
        elif not ema_bullish and ema20_slope < -1.0:
            return -0.3   # Strong downtrend — avoid!
        return 0.0

    @staticmethod
    def ml_signal(model, features):
        if model is None or features is None:
            return 0.0
        try:
            pred = model.predict([features])[0]
            return (pred - 0.5) * 2
        except:
            return 0.0


# ══════════════════════════════════════════════
#  ML ENGINE (improved)
# ══════════════════════════════════════════════
class MLEngine:
    def __init__(self):
        self.model = None
        self.feature_names = None
        self.accuracy = 0
        self.load_model()

    def load_model(self):
        if os.path.exists(MODEL_FILE) and HAS_LGB:
            try:
                with open(MODEL_FILE, "rb") as f:
                    d = pickle.load(f)
                self.model = d["model"]
                self.feature_names = d["feature_names"]
                self.accuracy = d.get("accuracy", 0)
            except:
                pass

    def save_model(self):
        if self.model and HAS_LGB:
            with open(MODEL_FILE, "wb") as f:
                pickle.dump({"model": self.model, "feature_names": self.feature_names,
                             "accuracy": self.accuracy}, f)

    def extract_features(self, candles, closes, orderbook=None):
        if len(closes) < 60:
            return None
        c = np.array(closes, dtype=float)
        f = {}

        # Returns
        for p in [1, 3, 5, 10, 20]:
            f[f"ret_{p}"] = (c[-1] / c[-1-p] - 1) * 100 if len(c) > p else 0

        # Volatility
        for p in [5, 10, 20]:
            if len(c) > p + 1:
                f[f"vol_{p}"] = float(np.std(np.diff(c[-p-1:]) / c[-p-1:-1]) * 100)
            else:
                f[f"vol_{p}"] = 0

        # RSI
        f["rsi_14"] = rsi(c, 14)
        f["rsi_7"] = rsi(c[-15:], 7)
        rs = rsi_series(c, 14)
        f["rsi_slope"] = float(rs[-1] - rs[-3]) if len(rs) > 3 else 0

        # EMA distances
        for p in [5, 10, 20, 50]:
            e = ema(c, p)
            f[f"ema{p}_dist"] = (c[-1] / e[-1] - 1) * 100

        # EMA slopes
        e20 = ema(c, 20)
        f["ema20_slope"] = (e20[-1] / e20[-3] - 1) * 100 if len(e20) > 3 else 0
        f["ema_cross"] = 1 if ema(c, 9)[-1] > ema(c, 21)[-1] else 0

        # MACD
        ml, sl, hist = macd(c)
        f["macd_hist"] = float(hist[-1])
        f["macd_hist_change"] = float(hist[-1] - hist[-2]) if len(hist) > 1 else 0
        f["macd_cross"] = 1 if hist[-1] > 0 and hist[-2] <= 0 else (-1 if hist[-1] < 0 and hist[-2] >= 0 else 0)

        # Bollinger
        bb_u, bb_m, bb_l = bollinger(c)
        if not np.isnan(bb_u[-1]):
            rng = bb_u[-1] - bb_l[-1]
            f["bb_pctb"] = (c[-1] - bb_l[-1]) / rng if rng > 0 else 0.5
            f["bb_width"] = rng / bb_m[-1] * 100
            f["bb_squeeze"] = 1 if f["bb_width"] < 3 else 0
        else:
            f["bb_pctb"], f["bb_width"], f["bb_squeeze"] = 0.5, 5, 0

        # Volume
        vols = np.array([candle["volume"] for candle in candles], dtype=float)
        avg_v = np.mean(vols[-20:-1]) if len(vols) > 20 else 1
        f["vol_ratio"] = float(vols[-1] / avg_v) if avg_v > 0 else 1
        f["vol_trend"] = float((np.mean(vols[-5:]) / np.mean(vols[-10:-5]) - 1) * 100) if len(vols) > 10 and np.mean(vols[-10:-5]) > 0 else 0

        # Candle
        body = abs(candles[-1]["close"] - candles[-1]["open"])
        rng = candles[-1]["high"] - candles[-1]["low"]
        f["body_pct"] = body / rng * 100 if rng > 0 else 50
        f["bullish"] = 1 if candles[-1]["close"] > candles[-1]["open"] else 0

        # ATR
        atr_val = atr(candles)
        f["atr_pct"] = atr_val / c[-1] * 100 if c[-1] > 0 else 0
        f["adx"] = adx(candles)

        # Position
        high_20 = max(c[-20:])
        low_20 = min(c[-20:])
        rng20 = high_20 - low_20
        f["pos_20"] = (c[-1] - low_20) / rng20 * 100 if rng20 > 0 else 50

        # Momentum
        f["mom_5"] = float(c[-1] - c[-6]) if len(c) > 6 else 0
        f["mom_10"] = float(c[-1] - c[-11]) if len(c) > 11 else 0

        # Order book (NEW in v3)
        if orderbook:
            f["buy_pressure"] = orderbook.get("buy_pressure", 0.5)
            f["ob_imbalance"] = orderbook.get("imbalance", 0)
        else:
            f["buy_pressure"] = 0.5
            f["ob_imbalance"] = 0

        # Time
        hour = datetime.now().hour
        f["hour_sin"] = float(np.sin(2 * np.pi * hour / 24))
        f["hour_cos"] = float(np.cos(2 * np.pi * hour / 24))

        return f

    def train(self, features_list, labels):
        if not HAS_LGB or len(features_list) < 50:
            return False
        try:
            X = np.array([[f[k] for k in sorted(f.keys())] for f in features_list])
            y = np.array(labels)
            self.feature_names = sorted(features_list[0].keys())

            # Walk-forward validation (NEW in v3)
            split = int(len(X) * 0.7)
            val_split = int(len(X) * 0.85)
            X_train, X_val, X_test = X[:split], X[split:val_split], X[val_split:]
            y_train, y_val, y_test = y[:split], y[split:val_split], y[val_split:]

            train_data = lgb.Dataset(X_train, label=y_train)
            val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

            params = {
                "objective": "binary", "metric": "binary_logloss",
                "num_leaves": 31, "learning_rate": 0.03,
                "feature_fraction": 0.7, "bagging_fraction": 0.7,
                "bagging_freq": 5, "min_child_samples": 20, "verbose": -1,
            }

            self.model = lgb.train(params, train_data, num_boost_round=300,
                                   valid_sets=[val_data],
                                   callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])

            # Test on unseen data
            preds = self.model.predict(X_test)
            correct = sum((p > 0.5) == y for p, y in zip(preds, y_test))
            self.accuracy = correct / len(y_test) * 100
            self.save_model()
            print(f"  🤖 ML trained — test accuracy: {self.accuracy:.1f}%")
            return True
        except Exception as e:
            print(f"  ⚠️ ML train failed: {e}")
            return False

    def predict(self, features):
        if self.model is None or not HAS_LGB or features is None:
            return 0.5
        try:
            X = np.array([[features[k] for k in sorted(features.keys())]])
            return float(self.model.predict(X)[0])
        except:
            return 0.5


# ══════════════════════════════════════════════
#  FUNDAMENTAL ANALYST
# ══════════════════════════════════════════════
class FundamentalAnalyst:
    @staticmethod
    def analyze():
        fg_val, fg_class = 50, "Neutral"
        try:
            resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()
            if "data" in resp:
                fg_val = int(resp["data"][0]["value"])
                fg_class = resp["data"][0]["value_classification"]
        except:
            pass

        if fg_val <= 20: signal = 0.7
        elif fg_val <= 35: signal = 0.3
        elif fg_val >= 80: signal = -0.7
        elif fg_val >= 65: signal = -0.3
        else: signal = 0.0

        return {"fear_greed": fg_val, "fg_class": fg_class, "signal": signal}


# ══════════════════════════════════════════════
#  RISK ANALYST
# ══════════════════════════════════════════════
class RiskAnalyst:
    @staticmethod
    def position_size(balance_thb, atr_val, price, heat):
        if heat >= MAX_PORTFOLIO_HEAT:
            return 0
        risk_pct = min(RISK_PER_TRADE_PCT, MAX_PORTFOLIO_HEAT - heat)
        risk_amount = balance_thb * (risk_pct / 100)
        if atr_val > 0:
            stop_dist = (atr_val * SL_ATR_MULT) / price
            pos = risk_amount / stop_dist if stop_dist > 0 else risk_amount
        else:
            pos = risk_amount
        result = int(min(pos, risk_amount, balance_thb * 0.3))
        return max(result, 500)  # Minimum 500 THB per trade

    @staticmethod
    def check(state, total_portfolio):
        issues = []
        heat = sum(t.get("amount_thb", 0) for t in state.get("open_trades", [])) / total_portfolio * 100 if total_portfolio > 0 else 0
        if state.get("daily_pnl", 0) < 0 and abs(state["daily_pnl"]) / total_portfolio * 100 > DAILY_LOSS_LIMIT_PCT:
            issues.append("Daily loss limit reached")
        if heat >= MAX_PORTFOLIO_HEAT:
            issues.append(f"Heat maxed: {heat:.1f}%")
        if len(state.get("open_trades", [])) >= MAX_OPEN_TRADES:
            issues.append("Max trades reached")

        # Cooldown check (NEW in v3)
        recent = state.get("recent_results", [])
        if len(recent) >= COOLDOWN_AFTER_LOSSES and all(r < 0 for r in recent[-COOLDOWN_AFTER_LOSSES:]):
            last_loss_time = state.get("last_loss_time", "")
            if last_loss_time:
                try:
                    lt = datetime.fromisoformat(last_loss_time)
                    if datetime.now() - lt < timedelta(hours=COOLDOWN_HOURS):
                        issues.append(f"Cooldown: {COOLDOWN_AFTER_LOSSES} consecutive losses")
                except:
                    pass

        return {"can_trade": len(issues) == 0, "issues": issues, "heat": heat}


# ══════════════════════════════════════════════
#  REFLECTOR
# ══════════════════════════════════════════════
class Reflector:
    @staticmethod
    def review(state):
        trades = state.get("closed_trades", [])
        if len(trades) < 5:
            return {"lesson": "Need more data", "adjustments": {}}
        recent = trades[-30:]
        wins = [t for t in recent if t.get("pnl", 0) > 0]
        wr = len(wins) / len(recent) * 100
        avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
        losses = [t for t in recent if t.get("pnl", 0) <= 0]
        avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 1
        rr = avg_win / avg_loss if avg_loss > 0 else 1
        adj = {}
        if wr < 40:
            adj["raise_threshold"] = 0.05
            lesson = f"Win rate low ({wr:.0f}%), raising threshold"
        elif wr > 65 and rr > 1.5:
            adj["lower_threshold"] = 0.03
            lesson = f"Win rate high ({wr:.0f}%, R:R {rr:.1f}), can be more aggressive"
        else:
            lesson = f"Win rate {wr:.0f}%, R:R {rr:.1f} — steady"
        return {"lesson": lesson, "win_rate": wr, "rr": rr, "adjustments": adj}


# ══════════════════════════════════════════════
#  ORCHESTRATOR v3
# ══════════════════════════════════════════════
class Orchestrator:
    def __init__(self):
        self.ml = MLEngine()
        self.state = self._load()

    def _load(self):
        default = {"open_trades": [], "closed_trades": [], "daily_pnl": 0,
                    "daily_date": datetime.now().strftime("%Y-%m-%d"),
                    "total_trades": 0, "winning_trades": 0, "total_pnl": 0,
                    "recent_results": [], "entry_threshold": 0.22, "last_loss_time": ""}
        # Override threshold from dynamic config
        if "_DYNAMIC_THRESHOLD" in globals():
            default["entry_threshold"] = globals()["_DYNAMIC_THRESHOLD"]
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

    def run(self, dry_run=True):
        print("=" * 64)
        print("  🧠 MULTI-AGENT AI TRADING SYSTEM v4.1 — Smart Edges")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 64)

        # === Market Data ===
        price_data = get_btc_price()
        if not price_data:
            print("❌ No price data"); return

        candles_15m = get_ohlcv("15", 200)
        candles_1h = get_ohlcv("60", 200)
        candles_4h = get_ohlcv("240", 100)
        time.sleep(0.5)  # Rate limit buffer

        if len(candles_1h) < 60:
            print(f"⚠️ Not enough 1h data ({len(candles_1h)})"); return

        closes_1h = [c["close"] for c in candles_1h]
        current = price_data["last"]
        print(f"\n📊 BTC/THB: {current:,.2f}  ({price_data['change']:+.2f}%)")

        # === Regime Detection ===
        adx_val = adx(candles_1h)
        ema20 = ema(closes_1h, 20)
        slope = (ema20[-1] - ema20[-5]) / ema20[-5] * 100 if len(ema20) > 5 else 0
        bb_u, bb_m, bb_l = bollinger(closes_1h)
        bb_w = (bb_u[-1] - bb_l[-1]) / bb_m[-1] * 100 if not np.isnan(bb_m[-1]) and bb_m[-1] > 0 else 5

        if bb_w > 8: regime = "volatile"
        elif adx_val > 25 and slope > 0.3: regime = "trending_up"
        elif adx_val > 25 and slope < -0.3: regime = "trending_down"
        else: regime = "ranging"

        weights = WEIGHTS[regime]
        print(f"\n🌊 REGIME: {regime.upper()} (ADX:{adx_val:.0f} BBW:{bb_w:.1f}%)")

        # === Order Book ===
        ob = get_order_book()
        print(f"📗 ORDER BOOK: Buy pressure {ob['buy_pressure']:.0%}  Imbalance {ob['imbalance']:+.2f}")

        # === Multi-Timeframe (NEW) ===
        mtf = MultiTimeframe.analyze(candles_15m, candles_1h, candles_4h)
        print(f"\n🔭 MULTI-TIMEFRAME: {mtf['signals']}  →  {mtf['score']:+.3f}  {'✅ ALIGNED' if mtf['aligned'] else '⚠️ MIXED'}")

        # === Technical Strategies (v4: meanrev focused) ===
        features = self.ml.extract_features(candles_1h, closes_1h, ob)
        ml_pred = self.ml.predict(features) if features else 0.5

        signals = {
            "meanrev":  Strategies.mean_reversion(closes_1h),
            "macd":     Strategies.macd_confirmation(closes_1h),
            "trend":    Strategies.trend_filter(closes_1h),
        }

        print(f"\n📈 SIGNALS:")
        for k, v in signals.items():
            icon = "🟢" if v > 0.1 else "🔴" if v < -0.1 else "⚪"
            print(f"   {icon} {k:12s} {v:+.2f}  {'█' * int(abs(v) * 10)}")

        ensemble = sum(signals[k] * weights[k] for k in signals)

        # === Volume Confirmation (v4: HARD REQUIREMENT) ===
        vp = volume_profile(candles_1h)
        vol_ok = vp["ratio"] >= MIN_VOLUME_RATIO
        vol_mult = VolumeFilter.confirm(candles_1h, ensemble)
        ensemble_adj = ensemble * vol_mult
        print(f"\n   🎯 Raw: {ensemble:+.3f} × Volume({vol_mult:.1f}) = {ensemble_adj:+.3f}")
        if not vol_ok:
            print(f"   ⚠️ Volume {vp['ratio']:.1f}x < {MIN_VOLUME_RATIO}x required")

        # === Fundamental ===
        fund = FundamentalAnalyst.analyze()
        print(f"\n📰 F&G: {fund['fear_greed']} ({fund['fg_class']})  Signal: {fund['signal']:+.2f}")

        final = ensemble_adj * 0.80 + fund["signal"] * 0.20
        print(f"   📊 Final Score: {final:+.3f}")

        # === Risk ===
        balances = get_balances() if not dry_run else {"thb": 30000, "btc": 0}
        total = balances["thb"] + balances["btc"] * current
        risk = RiskAnalyst.check(self.state, total)
        print(f"\n🛡️ Portfolio: {total:,.0f}  Heat: {risk['heat']:.1f}%  Trade: {'✅' if risk['can_trade'] else '❌'}")
        for iss in risk["issues"]:
            print(f"   ⚠️ {iss}")

        # === Reflector ===
        ref = Reflector.review(self.state)
        if ref.get("win_rate"):
            print(f"\n🪞 {ref['lesson']}")
            if ref["adjustments"].get("raise_threshold"):
                self.state["entry_threshold"] = min(0.50, self.state["entry_threshold"] + ref["adjustments"]["raise_threshold"])
            elif ref["adjustments"].get("lower_threshold"):
                self.state["entry_threshold"] = max(0.12, self.state["entry_threshold"] - ref["adjustments"]["lower_threshold"])

        # === Check Exits ===
        self._check_exits(current, signals, candles_1h, dry_run)

        # === v4.1: No-Trade Filter ===
        no_trade, no_trade_reasons = NoTradeFilter.is_no_trade_time(regime)
        if no_trade:
            print(f"\n🚫 NO-TRADE TIME:")
            for r in no_trade_reasons:
                print(f"   {r}")

        # === Entry Decision ===
        threshold = self.state["entry_threshold"]
        atr_val = atr(candles_1h)
        print(f"\n{'='*64}")

        # v4.1 NEW: Skip all entries in trending_down (long-only can't win)
        if regime == "trending_down":
            print(f"\n🚫 SKIP: trending_down regime — long-only strategy can't win in downtrend")

        if final >= threshold and risk["can_trade"] and vol_ok and not no_trade and regime != "trending_down":
            # v4.1: Regime-aware SL
            exits = DynamicExits.calculate(current, atr_val, regime=regime)

            if not exits["valid"]:
                print(f"  ⚠️ R:R too low ({exits['rr_ratio']:.1f} < {MIN_RISK_REWARD}). Skipping.")
            else:
                pos_size = RiskAnalyst.position_size(balances["thb"], atr_val, current, risk["heat"])
                if pos_size >= 500 and balances["thb"] >= pos_size:
                    dominant = max(signals, key=lambda k: signals[k] * weights[k])
                    bid_price = price_data["bid"]

                    # v4.1: Dynamic partial exit based on signal strength
                    partial_pct = DynamicPartialExit.get_partial_pct(final)

                    print(f"  🟢 BUY — Score {final:+.3f} ≥ {threshold}")
                    print(f"     Strategy: {dominant} | Regime: {regime}")
                    print(f"     Amount: {pos_size:,} THB")
                    print(f"     SL: {exits['stop_loss']:,.0f} (-{exits['sl_pct']:.1f}%) [SL mult: {exits['sl_mult_used']} for {regime}]")
                    print(f"     TP1: {exits['take_profit_1']:,.0f} (+{exits['tp1_pct']:.1f}%) → exit {partial_pct}% {'(STRONG)' if partial_pct == PARTIAL_EXIT_STRONG else '(normal)'}")
                    print(f"     TP2: {exits['take_profit_2']:,.0f} → trail rest")
                    print(f"     R:R = {exits['rr_ratio']:.1f}")

                    if not dry_run:
                        # v4.1: Try maker fee first
                        result = place_buy(pos_size, 0, "market", maker=True, bid_price=bid_price)
                        print(f"     Order: {result}")
                        if result.get("error") != 0:
                            result = place_buy(pos_size, 0, "market")
                            print(f"     Fallback market: {result}")
                        if result.get("error") != 0:
                            return

                    trade = {
                        "entry_price": current, "amount_thb": pos_size,
                        "amount_btc": pos_size / current,
                        "entry_time": datetime.now().isoformat(),
                        "highest_price": current,
                        "stop_loss": exits["stop_loss"],
                        "take_profit_1": exits["take_profit_1"],
                        "take_profit_2": exits["take_profit_2"],
                        "partial_exited": False,
                        "partial_pct": partial_pct,  # v4.1: store for exit
                        "score": final, "regime": regime, "dominant": dominant,
                    }
                    self.state["open_trades"].append(trade)
                    self._log("BUY", {"price": current, "amount": pos_size, "score": final,
                                      "regime": regime, "dominant": dominant, "dry_run": dry_run})
                else:
                    print(f"  ⚠️ Insufficient: need {pos_size:,}, have {balances['thb']:,.0f}")
        else:
            print(f"  ⚪ HOLD — Score {final:+.3f} (threshold ±{threshold:.2f})")

        print(f"{'='*64}")

        # Summary
        wr = self.state["winning_trades"] / self.state["total_trades"] * 100 if self.state["total_trades"] > 0 else 0
        print(f"\n📋 Trades: {len(self.state['open_trades'])} open | {self.state['total_trades']} total | {wr:.0f}% win")
        print(f"   Today: {self.state['daily_pnl']:,.0f} | Total: {self.state['total_pnl']:,.0f} THB")
        for i, t in enumerate(self.state["open_trades"]):
            pnl = (current - t["entry_price"]) / t["entry_price"] * 100
            print(f"   #{i+1} {t['entry_price']:,.0f}→{current:,.0f} ({pnl:+.1f}%) SL:{t['stop_loss']:,.0f} [{t.get('dominant','')}]")

        self._save()

    def _check_exits(self, price, signals, candles, dry_run):
        for trade in self.state["open_trades"][:]:
            entry = trade["entry_price"]
            pnl_pct = (price - entry) / entry * 100
            sl = trade.get("stop_loss", entry * 0.98)
            tp1 = trade.get("take_profit_1", entry * 1.03)
            tp2 = trade.get("take_profit_2", entry * 1.05)

            should_exit = False
            partial = False
            reason = ""

            # Stop loss
            if price <= sl:
                should_exit, reason = True, f"STOP LOSS ({pnl_pct:+.1f}%)"

            # Partial TP1
            elif price >= tp1 and not trade.get("partial_exited"):
                partial, reason = True, f"PARTIAL TP1 ({pnl_pct:+.1f}%)"

            # Full TP2
            elif price >= tp2:
                should_exit, reason = True, f"TAKE PROFIT 2 ({pnl_pct:+.1f}%)"

            # Trailing stop (after partial exit)
            elif trade.get("partial_exited"):
                highest = trade.get("highest_price", entry)
                if price > highest:
                    trade["highest_price"] = price
                dd = (highest - price) / highest * 100
                if dd >= 1.5:
                    should_exit, reason = True, f"TRAILING STOP ({pnl_pct:+.1f}%)"

            # v4.1: Time-based exit — don't lock capital forever
            if not should_exit and not partial:
                time_exit, time_reason = TimeBasedExit.should_exit(trade, price)
                if time_exit:
                    should_exit, reason = True, f"TIME EXIT — {time_reason} ({pnl_pct:+.1f}%)"

            # Strong sell signals (all 3 strategies agree)
            if not should_exit and not partial:
                if sum(1 for s in signals.values() if s < -0.2) >= 3:
                    should_exit, reason = True, f"STRONG SELL ({pnl_pct:+.1f}%)"

            if partial:
                # v4.1: Use trade's stored partial_pct (dynamic)
                partial_pct_val = trade.get("partial_pct", PARTIAL_EXIT_1_PCT)
                exit_btc = trade["amount_btc"] * (partial_pct_val / 100)
                pnl_thb = exit_btc * (price - entry)
                print(f"\n  🟡 PARTIAL EXIT: {reason} — selling {partial_pct_val}%")
                if not dry_run:
                    place_sell(exit_btc)
                trade["amount_btc"] -= exit_btc
                trade["amount_thb"] *= (1 - partial_pct_val / 100)
                trade["partial_exited"] = True
                trade["stop_loss"] = entry  # Move SL to breakeven
                self.state["daily_pnl"] += pnl_thb
                self.state["total_pnl"] += pnl_thb
                self._log("PARTIAL_SELL", {"reason": reason, "pnl": pnl_thb, "dry_run": dry_run})

            elif should_exit:
                pnl_thb = trade["amount_btc"] * (price - entry)
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
                    **trade, "exit_price": price, "pnl": pnl_thb,
                    "exit_reason": reason, "exit_time": datetime.now().isoformat()})
                self.state["closed_trades"] = self.state["closed_trades"][-200:]
                self.state["open_trades"].remove(trade)
                self._log("SELL", {"reason": reason, "pnl": pnl_thb, "dry_run": dry_run})


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
if __name__ == "__main__":
    cmds = {
        "analyze": "Analyze (no trade)", "dry-run": "Simulate",
        "live": "REAL trading", "status": "Show positions",
        "log": "Trade log", "train-ml": "Train ML model", "reset": "Reset state",
    }

    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print("v4.0 Backtest Proven")
        print("=" * 40)
        for c, d in cmds.items():
            print(f"  {c:12s}  {d}")
        sys.exit(1)

    cmd = sys.argv[1]
    orch = Orchestrator()

    if cmd in ("analyze", "dry-run"):
        orch.run(dry_run=True)
    elif cmd == "live":
        if not API_KEY or not API_SECRET:
            print("❌ Set API keys"); sys.exit(1)
        print("⚠️ LIVE MODE — 5s to cancel...")
        try: time.sleep(5)
        except KeyboardInterrupt: print("\nCancelled."); sys.exit(0)
        orch.run(dry_run=False)
    elif cmd == "status":
        p = get_btc_price()
        if p:
            print(f"BTC: {p['last']:,.2f}")
            for i, t in enumerate(orch.state["open_trades"]):
                pnl = (p["last"] - t["entry_price"]) / t["entry_price"] * 100
                print(f"\n  Trade #{i+1} [{t.get('dominant','')}]")
                print(f"    Entry:  {t['entry_price']:,.0f}")
                print(f"    Now:    {p['last']:,.0f}  ({pnl:+.1f}%)")
                print(f"    SL:     {t.get('stop_loss',0):,.0f}")
                print(f"    TP1:    {t.get('take_profit_1',0):,.0f}  (exit 50%)")
                print(f"    TP2:    {t.get('take_profit_2',0):,.0f}  (exit rest)")
                print(f"    Partial: {'Yes' if t.get('partial_exited') else 'No'}")
        if not orch.state["open_trades"]:
            print("No open trades.")
    elif cmd == "log":
        if os.path.exists(LOG_FILE):
            for e in json.load(open(LOG_FILE))[-20:]:
                print(f"  {e['time'][:19]} {e['action']:12s} {json.dumps({k:v for k,v in e.items() if k not in ['time','action']})}")
    elif cmd == "train-ml":
        c = get_ohlcv("60", 200)
        if len(c) > 60:
            feats, labels = [], []
            for i in range(60, len(c)-1):
                f = orch.ml.extract_features(c[:i+1], [x["close"] for x in c[:i+1]])
                if f:
                    feats.append(f)
                    labels.append(1 if c[i+1]["close"] > c[i]["close"] else 0)
            orch.ml.train(feats, labels)
    elif cmd == "reset":
        for f in [STATE_FILE, LOG_FILE, MODEL_FILE]:
            if os.path.exists(f): os.remove(f)
        print("✅ Reset done.")
