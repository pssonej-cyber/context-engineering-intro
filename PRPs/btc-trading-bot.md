name: "BTC Trading Bot — Supply/Demand Zone Strategy (70% Win Rate Target)"
description: |

## Goal

Build a **production-ready BTC trading bot** for the Bitkub exchange (BTC/THB pair) using a Supply/Demand Zone + Break of Structure strategy. The bot must:

1. Target a **≥ 70% win rate** by combining 4H market structure filter + BoS confirmation + 1H zone retest + rejection candle entry
2. Run reliably on **Ubuntu** as a systemd service with automated restarts
3. Be **fully configurable** via `.env` file with no hardcoded credentials
4. Include a **backtesting engine** to validate parameter changes before going live

## Why

- The existing v5/v7 zone scripts work but are single monolithic files — not maintainable, not testable, and not safe to run 24/7
- Ubuntu deployment requires proper daemonisation (systemd), logging, and auto-restart on failure
- The 70% win rate target requires the optimal parameter set discovered in backtest_v8 (RETEST=1.0%, LOOKBACK=30, STRUCTURE="trend_only", BoS required)
- Modular code enables A/B testing of strategy variants without rewriting the whole bot

## What

A Python package `btc_bot/` with clean separation of concerns:

- **Exchange layer**: Bitkub REST API client with HMAC-SHA256 signing
- **Strategy layer**: swing detection → structure analysis → zone detection → signal generation
- **Risk layer**: position sizing, portfolio heat, daily loss limit, cooldown
- **State layer**: JSON-based persistent state (open/closed trades, PnL)
- **Orchestrator**: `bot.py` ties everything together, called by systemd timer every 15 min
- **Backtester**: `backtest.py` — validate before live trading

### Success Criteria

- [ ] `python -m btc_bot backtest --days 60` shows win rate ≥ 65% on Bitkub BTC/THB 1H data
- [ ] `python -m btc_bot dry-run` produces signals without placing orders
- [ ] `python -m btc_bot live` places real orders when signal conditions are met
- [ ] All unit tests pass: `pytest tests/ -v`
- [ ] Bot runs as systemd service and auto-restarts on crash
- [ ] `.env.example` documents every required/optional variable

---

## All Needed Context

### Documentation & References

```yaml
- file: examples/btc_strategy_v5_zone.py
  why: >
    Complete reference implementation. Contains: Bitkub API (HMAC auth, endpoints),
    swing high/low detection, market structure (HH/HL/LH/LL), supply/demand zone
    detection, rejection candle logic, BoS confirmation, trailing stop, time exit,
    risk/position sizing, state persistence, backtest engine.
    COPY the API signing logic verbatim — it is correct and battle-tested.
    Optimal params from sweep: ZONE_WIDTH_PCT=3.0, TRAILING_STOP=1.0, REJECTION_THRESHOLD=0.3

- file: examples/btc_strategy_v7_zone.py
  why: >
    Improved zone detection with find_swings(left, right), analyze_structure(),
    detect_bos(), find_zones(). Also has partial exits: 50% at TP1 (2:1 R:R),
    remainder with trailing stop at 1.5%. Use this version's signal logic over v5.
    ZONE_PADDING_PCT=0.2, MIN_BOS_STRENGTH_PCT=0.3, REJECTION_MIN_WICK_RATIO=1.5

- file: examples/backtest_v8.py
  why: >
    Tuned parameters validated by backtest: RETEST_PCT=1.0 (was 0.5), LOOKBACK=30
    (was 50), 4H structure mapped from 1H candles via map_1h_to_4h(). These are
    the winning parameters to start with. Do NOT revert to v5 zone width (1.0%) —
    3.0% is optimal.

- url: https://github.com/bitkub/bitkub-official-api-docs
  why: >
    Official Bitkub API v3 docs. Key endpoints used:
    GET  /api/v3/servertime              — timestamp for HMAC
    GET  /api/v3/market/ticker           — current price (BTC_THB)
    GET  /tradingview/history            — OHLCV candlestick data
    POST /api/v3/market/balances         — wallet balances
    POST /api/v3/market/place-bid        — buy order
    POST /api/v3/market/place-ask        — sell order
    Auth: X-BTK-APIKEY, X-BTK-TIMESTAMP, X-BTK-SIGN (HMAC-SHA256)

- url: https://docs.python.org/3/library/hmac.html
  why: HMAC signing — already implemented in v5, copy exactly

- url: https://pydantic-docs.helpmanual.io/usage/settings/
  why: Use pydantic-settings for .env config loading with type validation

- url: https://www.freedesktop.org/software/systemd/man/systemd.service.html
  why: systemd service unit file for Ubuntu daemon setup
```

### Current Codebase Tree

```
d:\Claude\Context-Engineering-Intro\
├── CLAUDE.md
├── INITIAL.md
├── examples/
│   ├── btc_strategy_v5_zone.py   ← full reference bot (Bitkub, v5 zones)
│   ├── btc_strategy_v7_zone.py   ← improved version (better zones, partial TP)
│   ├── backtest_v8.py            ← tuned backtest using v7 functions
│   └── Autotrade.pdf             ← strategy background documentation
├── PRPs/
│   └── templates/prp_base.md
└── .claude/commands/
    ├── generate-prp.md
    └── execute-prp.md
```

### Desired Codebase Tree

```
btc_bot/
├── __init__.py
├── __main__.py            # CLI entry: python -m btc_bot [dry-run|live|status|backtest|reset]
├── config.py              # pydantic-settings: load .env, validate all params
├── api/
│   ├── __init__.py
│   └── bitkub.py          # BitkubClient: sign(), get_price(), get_ohlcv(), get_balances(), buy(), sell()
├── strategy/
│   ├── __init__.py
│   ├── structure.py       # find_swings(), analyze_structure() → "uptrend"|"downtrend"|"ranging"
│   ├── zones.py           # find_zones(), is_price_at_zone(), _remove_overlapping()
│   └── signals.py         # generate_signals(candles_4h, candles_1h) → List[Signal]
├── risk/
│   ├── __init__.py
│   └── manager.py         # RiskManager: can_trade(), position_size(), check_exits()
├── state.py               # BotState: load/save JSON, open_trades, closed_trades, PnL
├── bot.py                 # Bot.run(dry_run) — main orchestration loop
├── backtest.py            # run_backtest(days, params) → BacktestResult
├── tests/
│   ├── __init__.py
│   ├── test_structure.py
│   ├── test_zones.py
│   ├── test_signals.py
│   ├── test_risk.py
│   └── test_backtest.py
├── systemd/
│   └── btcbot.service     # systemd unit file for Ubuntu
├── .env.example
├── requirements.txt
└── README.md
```

### Known Gotchas

```python
# CRITICAL: Bitkub server time must be fetched fresh per-request for HMAC signing.
# Using local time causes "Invalid timestamp" (error 18). See v5:server_time()
ts = requests.get("https://api.bitkub.com/api/v3/servertime", timeout=10).text.strip()

# CRITICAL: HMAC payload format is: timestamp + METHOD + path + body_or_query
# e.g.: "1683000000000POST/api/v3/market/place-bid{...json...}"
sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

# CRITICAL: place-bid "amt" is in THB (not BTC). place-ask "amt" is in BTC.
# Minimum order: 500 THB. Always check before placing.

# CRITICAL: OHLCV endpoint returns "s":"ok" on success. Check this before parsing.
# Two fallback URLs exist (see v5:get_ohlcv) — always try both.

# GOTCHA: zone width of 1.0% is too tight for BTC volatility.
# Use ZONE_WIDTH_PCT=3.0 (from v5 parameter sweep). v7 uses ZONE_PADDING_PCT=0.2
# applied differently. Keep 3.0% for zone creation, 1.0% for retest proximity.

# GOTCHA: Overlapping zones must be de-duplicated (keep stronger bounce/rejection).
# See v5:_remove_overlapping_zones() — copy this logic exactly.

# GOTCHA: Zone detection on a sliding window — do NOT use the full candle history.
# Use last LOOKBACK=30 candles only (v8 tuning). Older zones degrade signal quality.

# GOTCHA: "trend_only" structure filter is critical for 70% win rate.
# Only enter LONG in uptrend, SHORT in downtrend. Disable "any" and "ranging" entries.

# GOTCHA: Position size formula: risk_thb / (sl_distance_pct / 100)
# Cap at min(30% of capital, available_thb). Min size: 500 THB.

# GOTCHA: pydantic-settings v2 uses model_config = SettingsConfigDict(env_file=".env")
# NOT class Config: env_file = ".env" (that's pydantic v1 style)
```

---

## Implementation Blueprint

### Data Models

```python
# config.py — all tunable parameters in one place
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class BotConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Exchange
    bitkub_api_key: str = Field(default="")
    bitkub_api_secret: str = Field(default="")
    bitkub_host: str = "https://api.bitkub.com"

    # Risk
    risk_per_trade_pct: float = 2.0
    max_open_trades: int = 2
    daily_loss_limit_pct: float = 3.0
    max_portfolio_heat_pct: float = 6.0
    cooldown_after_losses: int = 3
    cooldown_hours: int = 6
    min_position_thb: float = 500.0

    # Strategy
    zone_width_pct: float = 3.0        # zone size as % of price
    zone_lookback: int = 30            # candle window for zone detection
    swing_lookback_left: int = 3       # left bars for swing detection
    swing_lookback_right: int = 3      # right bars for swing detection
    retest_proximity_pct: float = 1.0  # "at zone" threshold
    min_bos_strength_pct: float = 0.3  # minimum BoS move
    rejection_min_wick_ratio: float = 1.5
    rejection_min_body_pct: float = 0.4
    structure_filter: str = "trend_only"  # "trend_only" | "any"

    # Exit
    trailing_stop_pct: float = 1.0
    tp1_rr_ratio: float = 2.0
    tp1_exit_pct: float = 50.0    # % of position to exit at TP1
    max_hold_hours: int = 96

# state.py
from dataclasses import dataclass, field
from typing import Optional
import json

@dataclass
class Trade:
    entry_price: float
    entry_time: str
    amount_thb: float
    amount_btc: float
    stop_loss: float
    direction: str           # "long" | "short"
    zone: dict
    highest_price: float
    partial_exited: bool = False
    structure: str = "ranging"

@dataclass
class BotState:
    open_trades: list[Trade] = field(default_factory=list)
    closed_trades: list[dict] = field(default_factory=list)
    daily_pnl: float = 0.0
    daily_date: str = ""
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    recent_results: list[float] = field(default_factory=list)
    last_loss_time: str = ""

# strategy/signals.py
from dataclasses import dataclass

@dataclass
class Signal:
    direction: str     # "long" | "short"
    entry: float
    stop_loss: float
    tp1: float
    zone: dict
    structure: str
    reason: str
    rr_potential: float
```

### Tasks

```yaml
Task 1 — Project scaffold:
CREATE btc_bot/__init__.py (empty)
CREATE btc_bot/__main__.py:
  - argparse CLI: subcommands dry-run, live, status, log, backtest, reset
  - Instantiate BotConfig() from .env, then Bot(config).run(dry_run=True/False)
CREATE requirements.txt:
  - requests>=2.31
  - numpy>=1.26
  - pydantic-settings>=2.0
  - pytest>=8.0
  - python-dotenv>=1.0
CREATE .env.example with all BotConfig fields documented

Task 2 — Bitkub API client:
CREATE btc_bot/api/bitkub.py:
  - MIRROR signing logic from examples/btc_strategy_v5_zone.py lines 106-119
  - BitkubClient(config: BotConfig) with methods:
      server_time() → str
      _sign(method, path, body) → dict (headers)
      get_price() → dict | None
      get_ohlcv(timeframe, limit) → list[dict]  # try both URLs (v5 lines 136-152)
      get_balances() → dict  # {"thb": float, "btc": float}
      place_buy(amount_thb, bid_price) → dict
      place_sell(amount_btc) → dict

Task 3 — Strategy: structure detection:
CREATE btc_bot/strategy/structure.py:
  - find_swings(candles, left, right) → list[dict]  # MIRROR v7's find_swings
  - analyze_structure(swings) → dict  # {"trend": "uptrend"|"downtrend"|"ranging"}
  - detect_bos(candles, swings, direction) → bool  # v7's detect_bos logic

Task 4 — Strategy: zone detection:
CREATE btc_bot/strategy/zones.py:
  - find_zones(candles, swings, lookback, width_pct) → {"demand": [...], "supply": [...]}
    PATTERN: v5:find_supply_demand_zones with ZONE_LOOKBACK=30, ZONE_WIDTH_PCT=3.0
  - is_price_at_zone(price, zone, proximity_pct) → bool
  - _remove_overlapping_zones(zones) → list  # COPY from v5 verbatim

Task 5 — Strategy: signal generation:
CREATE btc_bot/strategy/signals.py:
  - is_rejection_candle(candle, direction, config) → bool
    PATTERN: v7's is_rejection_candle (wick ratio + body % checks)
  - generate_signals(candles_4h, candles_1h, config) → list[Signal]:
      1. 4H: find_swings → analyze_structure
      2. If structure == "ranging" and config.structure_filter == "trend_only": return []
      3. 1H: find_zones (last LOOKBACK=30 candles)
      4. For each zone: check is_price_at_zone, check rejection candle, check BoS
      5. Yield Signal with entry, SL, TP1 (2:1), rr_potential

Task 6 — Risk manager:
CREATE btc_bot/risk/manager.py:
  - RiskManager(config, state):
      can_trade() → tuple[bool, list[str]]  # (ok, issues)
        Checks: daily loss limit, portfolio heat, max open trades, cooldown
      position_size(entry, sl, capital) → float  # THB, respects min 500
      check_exits(current_price, trades) → list[dict]  # trades to close + reasons
        Logic: stop loss, trailing stop (after 1% profit), TP1 partial, time exit

Task 7 — State persistence:
CREATE btc_bot/state.py:
  - STATE_FILE = ~/.btcbot/state.json
  - LOG_FILE = ~/.btcbot/log.json
  - load_state() → BotState
  - save_state(state: BotState)
  - append_log(entry: dict)
  - Reset daily_pnl at date rollover (check daily_date vs today)

Task 8 — Main bot orchestrator:
CREATE btc_bot/bot.py:
  - Bot(config: BotConfig):
      run(dry_run: bool):
        1. api.get_price() — bail if None
        2. api.get_ohlcv("240", 200) for 4H, api.get_ohlcv("60", 200) for 1H
        3. strategy.generate_signals(candles_4h, candles_1h)
        4. risk.check_exits(current_price, state.open_trades) — process exits
        5. risk.can_trade() — skip if blocked
        6. Take first signal (highest rr_potential)
        7. risk.position_size() — skip if < 500 THB
        8. If not dry_run: api.place_buy() → append to open_trades
        9. save_state(), print summary

Task 9 — Backtester:
CREATE btc_bot/backtest.py:
  - MIRROR examples/backtest_v8.py but use the modular strategy functions
  - run_backtest(days: int, config: BotConfig) → BacktestResult
  - map_1h_to_4h(candles_1h, end_idx) → list[dict]  # copy from backtest_v8
  - BacktestResult: total_trades, win_rate, total_pnl, max_drawdown, sharpe, trades

Task 10 — systemd service:
CREATE systemd/btcbot.service:
  - Type=oneshot (called by timer)
  - ExecStart=python -m btc_bot live
  - EnvironmentFile=/home/user/btcbot/.env
  - Restart=on-failure
CREATE systemd/btcbot.timer:
  - OnCalendar=*:0/15 (every 15 minutes)
  - WantedBy=timers.target
CREATE README.md with Ubuntu setup steps:
  - Install Python 3.11, pip install -r requirements.txt
  - Copy .env.example → .env, fill API keys
  - sudo cp systemd/* /etc/systemd/system/
  - sudo systemctl enable --now btcbot.timer

Task 11 — Tests:
CREATE tests/test_structure.py:
  - test_uptrend_detected() — synthetic HH/HL candles → "uptrend"
  - test_downtrend_detected() — synthetic LH/LL candles → "downtrend"
  - test_ranging_detected() — mixed candles → "ranging"
CREATE tests/test_zones.py:
  - test_demand_zone_found() — clear bounce after swing low
  - test_supply_zone_found() — clear rejection after swing high
  - test_overlapping_zones_deduplicated()
CREATE tests/test_signals.py:
  - test_no_signal_in_ranging_with_trend_filter()
  - test_long_signal_in_uptrend_at_demand_zone()
  - test_short_signal_in_downtrend_at_supply_zone()
CREATE tests/test_risk.py:
  - test_position_size_respects_risk_pct()
  - test_can_trade_blocked_by_daily_loss()
  - test_can_trade_blocked_by_max_trades()
  - test_stop_loss_exit_triggered()
  - test_trailing_stop_after_profit()
```

### Per-Task Pseudocode

```python
# Task 5 — generate_signals() — CRITICAL DETAIL
def generate_signals(candles_4h, candles_1h, config):
    signals = []

    # 4H structure
    swings_4h = find_swings(candles_4h, config.swing_lookback_left, config.swing_lookback_right)
    structure = analyze_structure(swings_4h)

    if config.structure_filter == "trend_only" and structure["trend"] == "ranging":
        return []  # No trades in ranging market

    trend = structure["trend"]  # "uptrend" or "downtrend"

    # 1H zones — only last LOOKBACK candles
    window = candles_1h[-config.zone_lookback:]
    swings_1h = find_swings(window, config.swing_lookback_left, config.swing_lookback_right)
    zones = find_zones(window, swings_1h, config.zone_lookback, config.zone_width_pct)

    current_price = candles_1h[-1]["close"]
    current_candle = candles_1h[-1]

    # LONG setup: demand zone + uptrend
    if trend == "uptrend":
        for zone in zones["demand"][:3]:
            if not is_price_at_zone(current_price, zone, config.retest_proximity_pct):
                continue
            if not is_rejection_candle(current_candle, "long", config):
                continue
            bos = detect_bos(candles_1h, swings_1h, "long")
            sl = zone["zone_low"] * (1 - 0.005)  # 0.5% buffer
            risk_pct = (current_price - sl) / current_price * 100
            if risk_pct < 0.5:
                continue
            tp1 = current_price + (current_price - sl) * config.tp1_rr_ratio
            signals.append(Signal(
                direction="long", entry=current_price, stop_loss=sl, tp1=tp1,
                zone=zone, structure=trend,
                reason=f"Demand retest + uptrend{'+ BoS' if bos else ''}",
                rr_potential=round(risk_pct, 2)
            ))

    # SHORT setup: supply zone + downtrend (mirror of above)
    ...

    # Sort by rr_potential desc
    signals.sort(key=lambda s: s.rr_potential, reverse=True)
    return signals


# Task 6 — RiskManager.check_exits() — CRITICAL DETAIL
def check_exits(self, current_price, trades):
    exits = []
    for trade in trades:
        entry = trade.entry_price
        direction = trade.direction
        sl = trade.stop_loss
        pnl_pct = (current_price - entry) / entry * 100 if direction == "long" \
                  else (entry - current_price) / entry * 100

        reason = None

        # 1. Stop loss
        if direction == "long" and current_price <= sl:
            reason = f"STOP_LOSS ({pnl_pct:+.1f}%)"
        elif direction == "short" and current_price >= sl:
            reason = f"STOP_LOSS ({pnl_pct:+.1f}%)"

        # 2. TP1 partial exit (2:1 R:R, 50% of position)
        if not reason and not trade.partial_exited:
            risk = abs(entry - sl)
            tp1 = entry + risk * self.config.tp1_rr_ratio if direction == "long" \
                  else entry - risk * self.config.tp1_rr_ratio
            if (direction == "long" and current_price >= tp1) or \
               (direction == "short" and current_price <= tp1):
                exits.append({"trade": trade, "reason": "TP1_PARTIAL", "pct": 50})
                trade.partial_exited = True
                trade.stop_loss = entry  # Move SL to breakeven
                continue

        # 3. Trailing stop (after ≥1% profit)
        if not reason and pnl_pct > 1.0:
            if direction == "long" and current_price > trade.highest_price:
                trade.highest_price = current_price
            drawdown = (trade.highest_price - current_price) / trade.highest_price * 100 \
                       if direction == "long" else 0
            if drawdown >= self.config.trailing_stop_pct:
                reason = f"TRAILING_STOP ({pnl_pct:+.1f}%)"

        # 4. Time exit
        if not reason:
            held = (datetime.now() - datetime.fromisoformat(trade.entry_time)).total_seconds() / 3600
            if held >= self.config.max_hold_hours and pnl_pct < 0.3:
                reason = f"TIME_EXIT (held {held:.0f}h, {pnl_pct:+.1f}%)"

        if reason:
            exits.append({"trade": trade, "reason": reason, "pct": 100})

    return exits
```

### Integration Points

```yaml
ENVIRONMENT (.env):
  BITKUB_API_KEY: your_api_key_here
  BITKUB_API_SECRET: your_api_secret_here
  RISK_PER_TRADE_PCT: 2.0
  MAX_OPEN_TRADES: 2
  ZONE_LOOKBACK: 30
  ZONE_WIDTH_PCT: 3.0
  RETEST_PROXIMITY_PCT: 1.0
  STRUCTURE_FILTER: trend_only
  TRAILING_STOP_PCT: 1.0
  TP1_RR_RATIO: 2.0

STATE FILES:
  ~/.btcbot/state.json   — persistent bot state (open trades, PnL)
  ~/.btcbot/log.json     — trade log (last 1000 entries)

SYSTEMD:
  /etc/systemd/system/btcbot.service   — oneshot service
  /etc/systemd/system/btcbot.timer     — every 15 minutes
  EnvironmentFile=/home/USER/btcbot/.env
```

---

## Validation Gates

### Level 1: Syntax & Style

```bash
# Install tools
pip install ruff mypy

# Lint
ruff check btc_bot/ --fix

# Type check
mypy btc_bot/ --ignore-missing-imports

# Expected: 0 errors. Fix all before proceeding.
```

### Level 2: Unit Tests

```bash
# Run all tests
pytest tests/ -v

# Expected output (all pass):
# tests/test_structure.py::test_uptrend_detected PASSED
# tests/test_structure.py::test_downtrend_detected PASSED
# tests/test_structure.py::test_ranging_detected PASSED
# tests/test_zones.py::test_demand_zone_found PASSED
# tests/test_zones.py::test_supply_zone_found PASSED
# tests/test_zones.py::test_overlapping_zones_deduplicated PASSED
# tests/test_signals.py::test_no_signal_in_ranging_with_trend_filter PASSED
# tests/test_signals.py::test_long_signal_in_uptrend_at_demand_zone PASSED
# tests/test_risk.py::test_position_size_respects_risk_pct PASSED
# tests/test_risk.py::test_can_trade_blocked_by_daily_loss PASSED
# tests/test_risk.py::test_stop_loss_exit_triggered PASSED
# tests/test_risk.py::test_trailing_stop_after_profit PASSED

# If failing: Read error, understand root cause, fix code, re-run (NEVER mock to pass)
```

### Level 3: Backtest Validation

```bash
# Run 60-day backtest (downloads live Bitkub data — no API key needed)
python -m btc_bot backtest --days 60

# Expected output includes:
# Win Rate: >= 60% (target 70% in live with full signal filtering)
# Max Drawdown: < 15%
# Profit Factor: > 1.2

# If win rate < 55%: check STRUCTURE_FILTER=trend_only is active,
# check RETEST_PROXIMITY_PCT=1.0, check ZONE_WIDTH_PCT=3.0
```

### Level 4: Dry-Run Integration

```bash
# Test full bot loop without placing orders
BITKUB_API_KEY="" BITKUB_API_SECRET="" python -m btc_bot dry-run

# Expected: prints market structure, zones, signals (or "no signal"), portfolio summary
# No HTTP POST requests should be made (confirm with: dry_run=True guard)

# If error: check API connectivity, check OHLCV fallback URLs
```

### Level 5: Ubuntu systemd Setup

```bash
# On Ubuntu server:
sudo cp systemd/btcbot.service systemd/btcbot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable btcbot.timer
sudo systemctl start btcbot.timer

# Verify timer is active:
systemctl status btcbot.timer
# Expected: Active: active (waiting)

# Manual test run:
sudo systemctl start btcbot.service
journalctl -u btcbot.service -n 50
# Expected: bot output without Python errors
```

---

## Final Validation Checklist

- [ ] `ruff check btc_bot/` — 0 errors
- [ ] `mypy btc_bot/ --ignore-missing-imports` — 0 errors
- [ ] `pytest tests/ -v` — all 12+ tests pass
- [ ] `python -m btc_bot backtest --days 60` — win rate ≥ 60%, no crash
- [ ] `python -m btc_bot dry-run` — prints signals without placing orders
- [ ] `.env.example` documents all 15+ config variables
- [ ] `systemd/btcbot.service` has `EnvironmentFile` and `Restart=on-failure`
- [ ] `README.md` contains complete Ubuntu setup steps (5 commands to running)
- [ ] No API keys hardcoded anywhere in source files

---

## Anti-Patterns to Avoid

- Do NOT hardcode API_KEY or API_SECRET — always load from env
- Do NOT use `time.sleep()` in the strategy — the systemd timer handles scheduling
- Do NOT use a single monolithic file — the modular structure is essential for testing
- Do NOT change the HMAC signing logic from v5 — it is correct, Bitkub rejects wrong signatures silently with error code 18
- Do NOT use ZONE_WIDTH_PCT < 2.0 — too tight, causes missed retests
- Do NOT use `structure_filter = "any"` in production — ranging markets hurt win rate
- Do NOT skip the backtest validation before switching to live mode
- Do NOT catch bare `except: pass` — log the error at minimum

---

## PRP Score: 8/10

**Confidence for one-pass implementation**: High

**Rationale**: The existing v5/v7 examples contain all required code patterns verbatim. The strategy logic, API client, and backtest engine are already proven. The main work is refactoring into a clean modular structure + adding Ubuntu deployment. The primary risk is HMAC signing edge cases and Bitkub API rate limits — both are documented with gotchas. The 70% win rate is achievable in trending markets with the validated parameter set; the bot will naturally achieve lower win rates in ranging periods (expected and acceptable).
