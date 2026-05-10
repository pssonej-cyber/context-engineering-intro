"""Unit tests for strategy.zones."""

from __future__ import annotations

from btc_bot.config import BotConfig
from btc_bot.strategy.zones import find_zones, is_price_at_zone


def _candle(t: int, o: float, h: float, low: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": 1.0}


def _config() -> BotConfig:
    return BotConfig(
        bitkub_api_key="",
        bitkub_api_secret="",
        zone_lookback=30,
        min_zone_size_pct=0.3,
        zone_padding_pct=0.2,
        max_zone_age_bars=100,
        retest_proximity_pct=1.0,
    )


def test_demand_zone_found_after_strong_up_impulse():
    # 10 small candles, then small base, then huge bullish impulse
    candles = [_candle(i, 100, 101, 99, 100) for i in range(10)]
    # Base: 3 small candles around 100
    candles += [_candle(10, 100, 101, 99, 100)]
    candles += [_candle(11, 100, 101, 99, 100)]
    # Strong bullish impulse: large green body
    candles += [_candle(12, 100, 110, 100, 109)]
    # Pad to 30+ candles for find_zones to engage
    candles += [_candle(i, 109, 110, 108, 109) for i in range(13, 32)]

    zones = find_zones(candles, _config())
    demands = [z for z in zones if z["type"] == "demand"]
    assert len(demands) >= 1
    assert demands[0]["high"] >= 99  # zone covers the base low


def test_supply_zone_found_after_strong_down_impulse():
    candles = [_candle(i, 100, 101, 99, 100) for i in range(10)]
    candles += [_candle(10, 100, 101, 99, 100)]
    candles += [_candle(11, 100, 101, 99, 100)]
    # Strong bearish impulse
    candles += [_candle(12, 100, 100, 90, 91)]
    candles += [_candle(i, 91, 92, 90, 91) for i in range(13, 32)]

    zones = find_zones(candles, _config())
    supplies = [z for z in zones if z["type"] == "supply"]
    assert len(supplies) >= 1


def test_is_price_at_zone_within_buffer():
    zone = {"low": 100.0, "high": 102.0, "type": "demand"}
    # Just inside the zone
    assert is_price_at_zone(101.0, zone, proximity_pct=1.0)
    # 0.5% below low → within buffer
    assert is_price_at_zone(99.5, zone, proximity_pct=1.0)
    # 5% below low → outside buffer
    assert not is_price_at_zone(95.0, zone, proximity_pct=1.0)


def test_invalidated_zones_excluded():
    """A demand zone is dropped once price closes below its low."""
    candles = [_candle(i, 100, 101, 99, 100) for i in range(10)]
    candles += [_candle(10, 100, 101, 99, 100)]
    candles += [_candle(11, 100, 101, 99, 100)]
    candles += [_candle(12, 100, 110, 100, 109)]  # demand zone forms here
    # Then price closes back BELOW the base low (98 < base_low ~99)
    candles += [_candle(13, 109, 109, 95, 96)]  # invalidates demand
    candles += [_candle(i, 96, 97, 95, 96) for i in range(14, 32)]

    zones = find_zones(candles, _config())
    demands = [z for z in zones if z["type"] == "demand"]
    # Either the zone is filtered out, or its low has been closed below
    for z in demands:
        # No closes below z["low"] should remain after creation
        post = candles[z["created_idx"] + 1 :]
        assert not any(c["close"] < z["low"] for c in post)


def test_no_zones_when_too_few_candles():
    candles = [_candle(i, 100, 101, 99, 100) for i in range(10)]
    zones = find_zones(candles, _config())
    assert zones == []
