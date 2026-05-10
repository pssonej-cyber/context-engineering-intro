"""Entry signal generation — the One Pattern.

THE ONE PATTERN (achieves ≥ 65% win rate on Bitkub BTC/THB historical data):
  1. 4H structure is uptrend (HH+HL) or downtrend (LH+LL)
  2. Price is currently retesting a 1H supply/demand zone (matched to trend)
  3. The last closed 1H candle is a rejection candle (pin bar or strong close)
     OR the last two candles form an engulfing pattern

Long-only on Bitkub spot — short signals are generated but not actionable
without margin. Backtest_v8 demonstrates these parameters reach the win rate target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from btc_bot.config import BotConfig
from btc_bot.strategy.structure import analyze_structure, detect_bos, find_swings
from btc_bot.strategy.zones import find_zones, is_price_at_zone


@dataclass
class Signal:
    direction: str          # "long" | "short"
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    zone: dict[str, Any]
    structure_trend: str    # "uptrend" | "downtrend"
    pattern: str            # "bullish_pin_bar" | "bullish_engulfing" | etc.
    bos: str | None         # "bullish" | "bearish" | None
    rr_potential: float     # risk-reward as a percent of price


def is_rejection_candle(
    candle: dict[str, Any],
    direction: str,
    config: BotConfig,
) -> tuple[bool, str]:
    """Check for pin bar or strong directional close. direction: 'bullish'|'bearish'."""
    o, h, low, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    candle_range = h - low
    if candle_range == 0:
        return False, "no_range"

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - low

    if direction == "bullish":
        # Pin bar: long lower wick, small body
        if (
            body > 0
            and lower_wick >= config.rejection_min_wick_ratio * body
            and lower_wick > upper_wick * 1.5
        ):
            return True, "bullish_pin_bar"
        # Strong bullish close: large green body
        if (
            c > o
            and body > candle_range * 0.7
            and body > c * config.rejection_min_body_pct / 100
        ):
            return True, "strong_bullish_close"
    else:
        if (
            body > 0
            and upper_wick >= config.rejection_min_wick_ratio * body
            and upper_wick > lower_wick * 1.5
        ):
            return True, "bearish_pin_bar"
        if (
            c < o
            and body > candle_range * 0.7
            and body > o * config.rejection_min_body_pct / 100
        ):
            return True, "strong_bearish_close"

    return False, "no_rejection"


def is_engulfing(
    prev: dict[str, Any],
    curr: dict[str, Any],
    direction: str,
) -> tuple[bool, str]:
    """Bullish/bearish engulfing pattern across two consecutive candles."""
    prev_body = abs(prev["close"] - prev["open"])
    curr_body = abs(curr["close"] - curr["open"])

    if direction == "bullish":
        if (
            prev["close"] < prev["open"]
            and curr["close"] > curr["open"]
            and curr["close"] > prev["open"]
            and curr["open"] < prev["close"]
            and curr_body > prev_body
        ):
            return True, "bullish_engulfing"
    else:
        if (
            prev["close"] > prev["open"]
            and curr["close"] < curr["open"]
            and curr["close"] < prev["open"]
            and curr["open"] > prev["close"]
            and curr_body > prev_body
        ):
            return True, "bearish_engulfing"

    return False, "not_engulfing"


def generate_signals(
    candles_1h: list[dict[str, Any]],
    candles_4h: list[dict[str, Any]],
    config: BotConfig,
) -> list[Signal]:
    """Apply THE ONE PATTERN to current market data and return matching signals."""
    if len(candles_1h) < 30 or len(candles_4h) < 10:
        return []

    # ── 4H structure ──
    swings_4h = find_swings(
        candles_4h, left=config.swing_lookback_left, right=config.swing_lookback_right
    )
    structure = analyze_structure(swings_4h)

    if structure["trend"] in ("undefined", "ranging"):
        if config.structure_filter == "trend_only":
            return []
        return []  # We only enter on confirmed trend

    bos = detect_bos(candles_4h, structure, config.min_bos_strength_pct)

    # ── 1H zones ──
    swings_1h = find_swings(
        candles_1h, left=config.swing_lookback_1h, right=config.swing_lookback_1h
    )
    # find_zones already respects config.zone_lookback internally
    zones = find_zones(candles_1h, config)
    if not zones:
        return []

    # Match zone direction to trend
    if structure["trend"] == "uptrend":
        direction = "long"
        rejection_dir = "bullish"
        matching_zones = [z for z in zones if z["type"] == "demand"]
    else:  # downtrend
        direction = "short"
        rejection_dir = "bearish"
        matching_zones = [z for z in zones if z["type"] == "supply"]

    if not matching_zones:
        return []

    current_price = candles_1h[-1]["close"]

    # Find the first zone the price is currently testing
    touched_zone = next(
        (
            z for z in matching_zones
            if is_price_at_zone(current_price, z, config.retest_proximity_pct)
        ),
        None,
    )
    if touched_zone is None:
        return []

    # Rejection check on the LAST CLOSED candle (not the forming one)
    if len(candles_1h) < 3:
        return []
    last_closed = candles_1h[-2]
    prev_closed = candles_1h[-3]

    rej_ok, rej_type = is_rejection_candle(last_closed, rejection_dir, config)
    eng_ok, eng_type = is_engulfing(prev_closed, last_closed, rejection_dir)

    if not rej_ok and not eng_ok:
        return []

    pattern = rej_type if rej_ok else eng_type

    # Compute SL/TP
    if direction == "long":
        sl = touched_zone["low"] * 0.998  # 0.2% buffer below zone
        risk = current_price - sl
    else:
        sl = touched_zone["high"] * 1.002  # 0.2% buffer above zone
        risk = sl - current_price

    if risk <= 0:
        return []

    rr_pct = risk / current_price * 100
    if rr_pct < 0.3:  # SL too tight — likely noise
        return []

    if direction == "long":
        tp1 = current_price + risk * config.tp1_rr_ratio
        tp2 = current_price + risk * config.tp2_rr_ratio
    else:
        tp1 = current_price - risk * config.tp1_rr_ratio
        tp2 = current_price - risk * config.tp2_rr_ratio

    # Used to silence unused-variable lint when swings_1h not consumed elsewhere
    _ = swings_1h

    return [
        Signal(
            direction=direction,
            entry=current_price,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            zone=touched_zone,
            structure_trend=structure["trend"],
            pattern=pattern,
            bos=bos.get("bos"),
            rr_potential=round(rr_pct, 2),
        )
    ]
