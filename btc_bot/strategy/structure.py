"""Market structure detection: swing points, trend (HH/HL or LH/LL), Break of Structure.

Pure-functional — all functions take candle lists and return new data, no global state.
Logic mirrors examples/btc_strategy_v7_zone.py — see that file for reference output.
"""

from __future__ import annotations

from typing import Any


def find_swings(
    candles: list[dict[str, Any]],
    left: int = 2,
    right: int = 2,
) -> list[dict[str, Any]]:
    """Find swing highs and swing lows in chronological order.

    A swing high is a candle whose high is strictly higher than `left` candles
    before and `right` candles after. Swing low is the mirror.
    """
    swings: list[dict[str, Any]] = []
    for i in range(left, len(candles) - right):
        h = candles[i]["high"]
        low = candles[i]["low"]

        is_high = (
            all(candles[j]["high"] < h for j in range(i - left, i))
            and all(candles[j]["high"] < h for j in range(i + 1, i + right + 1))
        )
        is_low = (
            all(candles[j]["low"] > low for j in range(i - left, i))
            and all(candles[j]["low"] > low for j in range(i + 1, i + right + 1))
        )

        if is_high:
            swings.append(
                {"idx": i, "type": "high", "price": h, "time": candles[i]["time"]}
            )
        elif is_low:
            swings.append(
                {"idx": i, "type": "low", "price": low, "time": candles[i]["time"]}
            )
    return swings


def analyze_structure(swings: list[dict[str, Any]]) -> dict[str, Any]:
    """Determine trend from the last two highs and lows.

    Returns: dict with `trend` ∈ {"uptrend", "downtrend", "ranging", "undefined"}
    plus the swing references used.
    """
    if len(swings) < 4:
        return {
            "trend": "undefined",
            "last_high": None,
            "last_low": None,
            "prev_high": None,
            "prev_low": None,
        }

    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]

    if len(highs) < 2 or len(lows) < 2:
        return {
            "trend": "undefined",
            "last_high": highs[-1] if highs else None,
            "last_low": lows[-1] if lows else None,
            "prev_high": None,
            "prev_low": None,
        }

    last_high, prev_high = highs[-1], highs[-2]
    last_low, prev_low = lows[-1], lows[-2]

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


def detect_bos(
    candles: list[dict[str, Any]],
    structure: dict[str, Any],
    min_strength_pct: float = 0.3,
) -> dict[str, Any]:
    """Break of Structure: price has broken the last significant swing level.

    Uptrend BoS: recent high > last_high * (1 + strength%) — trend continuation
    Downtrend BoS: recent low < last_low * (1 - strength%) — trend continuation
    """
    if structure["trend"] in ("undefined", "ranging"):
        return {"bos": None, "broken_level": None}

    if structure["trend"] == "uptrend" and structure["last_high"]:
        level = structure["last_high"]["price"]
        recent_high = max(c["high"] for c in candles[-5:])
        if recent_high > level * (1 + min_strength_pct / 100):
            return {
                "bos": "bullish",
                "broken_level": level,
                "broken_swing": structure["last_high"],
            }

    if structure["trend"] == "downtrend" and structure["last_low"]:
        level = structure["last_low"]["price"]
        recent_low = min(c["low"] for c in candles[-5:])
        if recent_low < level * (1 - min_strength_pct / 100):
            return {
                "bos": "bearish",
                "broken_level": level,
                "broken_swing": structure["last_low"],
            }

    return {"bos": None, "broken_level": None}
