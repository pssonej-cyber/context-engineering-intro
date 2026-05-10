"""Unit tests for strategy.signals — rejection candle detection and signal gating."""

from __future__ import annotations

from btc_bot.config import BotConfig
from btc_bot.strategy.signals import (
    generate_signals,
    is_engulfing,
    is_rejection_candle,
)


def _candle(t: int, o: float, h: float, low: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": 1.0}


def _config() -> BotConfig:
    return BotConfig(bitkub_api_key="", bitkub_api_secret="")


def test_bullish_pin_bar_detected():
    # Long lower wick, small green body
    candle = _candle(0, 100, 101, 90, 100.5)  # body=0.5, lower_wick=10
    ok, kind = is_rejection_candle(candle, "bullish", _config())
    assert ok
    assert "pin_bar" in kind


def test_bearish_pin_bar_detected():
    candle = _candle(0, 100, 110, 99, 99.5)
    ok, kind = is_rejection_candle(candle, "bearish", _config())
    assert ok
    assert "pin_bar" in kind


def test_no_rejection_when_indecisive():
    # Doji with no clear wick or body
    candle = _candle(0, 100, 100.5, 99.5, 100)
    ok, _ = is_rejection_candle(candle, "bullish", _config())
    assert not ok


def test_bullish_engulfing_detected():
    # Engulfing requires: prev red, curr green, curr.open < prev.close, curr.close > prev.open
    prev = _candle(0, 102, 102.5, 100, 100)   # red body 102→100
    curr = _candle(1, 99.5, 104, 99, 103)     # green body 99.5→103, engulfs prev
    ok, kind = is_engulfing(prev, curr, "bullish")
    assert ok
    assert kind == "bullish_engulfing"


def test_engulfing_requires_proper_pattern():
    prev = _candle(0, 100, 102, 99, 101)  # green
    curr = _candle(1, 101, 102, 100, 100.5)  # also green — not engulfing
    ok, _ = is_engulfing(prev, curr, "bullish")
    assert not ok


def test_no_signal_when_4h_ranging():
    """trend_only filter should produce no signals in a sideways market."""
    config = _config()
    # 50 candles all in a tight range — no clear trend
    candles_1h = [_candle(i, 100, 101, 99, 100) for i in range(50)]
    candles_4h = [_candle(i, 100, 101, 99, 100) for i in range(15)]
    signals = generate_signals(candles_1h, candles_4h, config)
    assert signals == []


def test_no_signal_with_insufficient_data():
    config = _config()
    signals = generate_signals(
        [_candle(i, 100, 101, 99, 100) for i in range(5)],
        [_candle(i, 100, 101, 99, 100) for i in range(5)],
        config,
    )
    assert signals == []
