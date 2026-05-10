"""Unit tests for strategy.structure."""

from __future__ import annotations

from btc_bot.strategy.structure import analyze_structure, detect_bos, find_swings


def _candle(t: int, o: float, h: float, low: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": 1.0}


def test_find_swings_detects_high_and_low():
    # find_swings requires STRICTLY greater/less than ALL neighbors in the lookback window.
    # Index 3 must have the unique maximum high; index 6 must have the unique minimum low.
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 102, 98, 101),
        _candle(2, 101, 103, 100, 102),
        _candle(3, 102, 115, 101, 109),  # swing high at idx 3 (high=115 unique max)
        _candle(4, 109, 110, 102, 103),
        _candle(5, 103, 104, 97, 100),
        _candle(6, 100, 101, 90, 95),    # swing low at idx 6 (low=90 unique min)
        _candle(7, 95, 99, 96, 97),
        _candle(8, 97, 100, 95, 98),
    ]
    swings = find_swings(candles, left=2, right=2)
    types = [s["type"] for s in swings]
    assert "high" in types
    assert "low" in types


def test_uptrend_detected_with_hh_hl():
    # Synthetic swings: HH and HL → uptrend
    swings = [
        {"idx": 0, "type": "low", "price": 100, "time": 0},
        {"idx": 5, "type": "high", "price": 110, "time": 5},
        {"idx": 10, "type": "low", "price": 105, "time": 10},   # higher low
        {"idx": 15, "type": "high", "price": 115, "time": 15},  # higher high
    ]
    structure = analyze_structure(swings)
    assert structure["trend"] == "uptrend"
    assert structure["hh"] is True
    assert structure["hl"] is True


def test_downtrend_detected_with_lh_ll():
    swings = [
        {"idx": 0, "type": "high", "price": 110, "time": 0},
        {"idx": 5, "type": "low", "price": 100, "time": 5},
        {"idx": 10, "type": "high", "price": 105, "time": 10},  # lower high
        {"idx": 15, "type": "low", "price": 95, "time": 15},    # lower low
    ]
    structure = analyze_structure(swings)
    assert structure["trend"] == "downtrend"


def test_ranging_when_no_clear_trend():
    swings = [
        {"idx": 0, "type": "high", "price": 110, "time": 0},
        {"idx": 5, "type": "low", "price": 100, "time": 5},
        {"idx": 10, "type": "high", "price": 112, "time": 10},  # HH
        {"idx": 15, "type": "low", "price": 98, "time": 15},    # LL → mixed
    ]
    structure = analyze_structure(swings)
    assert structure["trend"] == "ranging"


def test_undefined_with_too_few_swings():
    structure = analyze_structure([])
    assert structure["trend"] == "undefined"


def test_detect_bos_uptrend_breaks_high():
    structure = {
        "trend": "uptrend",
        "last_high": {"price": 100.0, "idx": 5, "type": "high", "time": 5},
        "last_low": {"price": 90.0, "idx": 8, "type": "low", "time": 8},
    }
    # Recent candle whose high blows past 100 → bullish BoS
    candles = [_candle(i, 99, 102, 98, 101) for i in range(5)]
    result = detect_bos(candles, structure, min_strength_pct=0.3)
    assert result["bos"] == "bullish"


def test_detect_bos_no_break_when_below_threshold():
    structure = {
        "trend": "uptrend",
        "last_high": {"price": 100.0, "idx": 5, "type": "high", "time": 5},
        "last_low": {"price": 90.0, "idx": 8, "type": "low", "time": 8},
    }
    candles = [_candle(i, 99, 100.1, 98, 99) for i in range(5)]
    result = detect_bos(candles, structure, min_strength_pct=0.5)
    assert result["bos"] is None
