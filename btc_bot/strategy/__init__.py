"""Strategy: structure detection, zone detection, signal generation."""

from btc_bot.strategy.structure import find_swings, analyze_structure, detect_bos
from btc_bot.strategy.zones import find_zones, is_price_at_zone
from btc_bot.strategy.signals import generate_signals, Signal

__all__ = [
    "find_swings",
    "analyze_structure",
    "detect_bos",
    "find_zones",
    "is_price_at_zone",
    "generate_signals",
    "Signal",
]
