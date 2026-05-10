"""Supply/Demand zone detection (Breaker Blocks).

A zone is the 1-3 candle "base" immediately preceding a strong impulse candle.
- Demand zone: base before strong up-impulse → future support
- Supply zone: base before strong down-impulse → future resistance

A zone is invalidated once price closes through it.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from btc_bot.config import BotConfig


def find_zones(
    candles: list[dict[str, Any]],
    config: BotConfig,
) -> list[dict[str, Any]]:
    """Identify valid (untested) supply/demand zones in the lookback window.

    Returns zones sorted by recency (newest first).
    """
    if len(candles) < 30:
        return []

    zones: list[dict[str, Any]] = []
    start_idx = max(0, len(candles) - config.zone_lookback)

    for i in range(start_idx + 10, len(candles) - 2):
        # Average true range of previous 10 candles
        ranges = [candles[j]["high"] - candles[j]["low"] for j in range(i - 10, i)]
        avg_range = float(np.mean(ranges))
        if avg_range == 0:
            continue

        current = candles[i]
        curr_range = current["high"] - current["low"]
        curr_body = abs(current["close"] - current["open"])

        # Strong impulse: range > 2x avg, body > 60% of range
        if not (curr_range > 2 * avg_range and curr_body > 0.6 * curr_range):
            continue

        is_up = current["close"] > current["open"]

        # Base = 1-3 candles immediately before the impulse
        base_candles = candles[max(0, i - 3) : i]
        if not base_candles:
            continue

        base_high = max(c["high"] for c in base_candles)
        base_low = min(c["low"] for c in base_candles)

        # Skip if zone is too small to be meaningful
        zone_size_pct = (base_high - base_low) / current["close"] * 100
        if zone_size_pct < config.min_zone_size_pct:
            continue

        # Pad zone edges by configured %
        padding = (base_high - base_low) * (config.zone_padding_pct / 100)
        zone_high = base_high + padding
        zone_low = base_low - padding

        zones.append(
            {
                "type": "demand" if is_up else "supply",
                "high": zone_high,
                "low": zone_low,
                "created_idx": i,
                "created_time": current["time"],
                "age_bars": len(candles) - 1 - i,
                "impulse_strength": curr_range / avg_range,
            }
        )

    # Drop stale zones
    zones = [z for z in zones if z["age_bars"] <= config.max_zone_age_bars]

    # Drop invalidated zones (price has closed through them)
    valid_zones: list[dict[str, Any]] = []
    for z in zones:
        created = z["created_idx"]
        if z["type"] == "demand":
            broken = any(c["close"] < z["low"] for c in candles[created + 1 :])
        else:
            broken = any(c["close"] > z["high"] for c in candles[created + 1 :])
        if not broken:
            valid_zones.append(z)

    # Newest first — recent zones have higher trade probability
    valid_zones.sort(key=lambda z: z["created_idx"], reverse=True)
    return valid_zones


def is_price_at_zone(
    price: float,
    zone: dict[str, Any],
    proximity_pct: float,
) -> bool:
    """True if price is within the zone (with proximity buffer on each side)."""
    buffer = price * proximity_pct / 100
    return (zone["low"] - buffer) <= price <= (zone["high"] + buffer)
