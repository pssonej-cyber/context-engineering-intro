# BTC Trading Bot

Supply/Demand Zone strategy on Bitkub (BTC/THB spot). Long-only. Targets ≥ 65% win rate
using the One Pattern: 4H trend + 1H zone retest + rejection candle.

## Quick start (Ubuntu)

```bash
# 1. Install Python 3.11+ and clone
sudo apt update && sudo apt install -y python3.11 python3.11-venv git
git clone <your-repo> ~/btcbot && cd ~/btcbot

# 2. Virtual env + dependencies
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Configure
cp .env.example .env
nano .env       # paste BITKUB_API_KEY and BITKUB_API_SECRET

# 4. Validate before going live
.venv/bin/python -m btc_bot backtest --days 60     # check win rate ≥ 60%
.venv/bin/python -m btc_bot dry-run                # check signals + plumbing

# 5. Install systemd timer (replace USER with your username)
sudo cp systemd/btcbot.service /etc/systemd/system/btcbot@.service
sudo cp systemd/btcbot.timer /etc/systemd/system/btcbot@.timer
sudo systemctl daemon-reload
sudo systemctl enable --now btcbot@$USER.timer

# 6. Watch it run
journalctl -u btcbot@$USER.service -f
```

## Commands

| Command                     | Purpose                                      |
| --------------------------- | -------------------------------------------- |
| `python -m btc_bot dry-run` | One analysis cycle, no orders placed         |
| `python -m btc_bot live`    | One analysis cycle, places orders            |
| `python -m btc_bot status`  | Show open trades and PnL                     |
| `python -m btc_bot log`     | Last 20 log entries                          |
| `python -m btc_bot backtest --days 60` | Replay strategy on historical 1H candles |
| `python -m btc_bot reset`   | Wipe state + log                             |

## Architecture

```
btc_bot/
├── config.py              # pydantic-settings: load .env, validate params
├── api/bitkub.py          # Bitkub REST client (HMAC-SHA256 signing)
├── strategy/
│   ├── structure.py       # find_swings, analyze_structure, detect_bos
│   ├── zones.py           # find_zones, is_price_at_zone
│   └── signals.py         # generate_signals — THE ONE PATTERN
├── risk/manager.py        # position_size, can_trade, check_exits
├── state.py               # JSON state persistence (~/.btcbot/state.json)
├── bot.py                 # main orchestration loop
├── backtest.py            # historical replay using same strategy code
├── __main__.py            # CLI entry point
└── tests/                 # pytest unit tests (~30 tests)
```

## The Strategy

**The One Pattern** (achieves the win rate target):

1. **4H structure** — uptrend (HH+HL) or downtrend (LH+LL) confirmed via swing analysis
2. **1H zone retest** — current price is touching a fresh supply/demand zone matched to trend
3. **Rejection candle** — last closed 1H candle is a pin bar OR the last two form an engulfing pattern
4. **Stop loss** — placed 0.2% beyond the opposite side of the zone
5. **Partial exit** — 50% off at 2:1 R:R, SL moved to breakeven
6. **Trailing stop** — 1% trail on the runner

Validated parameters (from `examples/backtest_v8.py`):

| Parameter             | Value      | Why                                         |
| --------------------- | ---------- | ------------------------------------------- |
| `RETEST_PROXIMITY_PCT`| 1.0        | Loose enough to catch retests, tight enough to filter noise |
| `ZONE_LOOKBACK`       | 30         | Recent zones only — older zones decay       |
| `STRUCTURE_FILTER`    | trend_only | Skip ranging markets — they kill win rate   |
| `RISK_PER_TRADE_PCT`  | 2.0        | Standard prudent sizing                     |
| `TP1_RR_RATIO`        | 2.0        | High-probability exit, locks in winner      |
| `TRAILING_STOP_PCT`   | 1.0        | Tight enough to protect profits, loose enough to ride trends |

## Risk Controls

- **Daily loss limit**: bot halts new entries if cumulative loss > 3% of capital
- **Portfolio heat**: max 6% of capital deployed across all open trades
- **Max open trades**: 2 simultaneous positions
- **Cooldown**: pause for 6h after 3 consecutive losses
- **Long-only**: short signals are detected but not actionable on Bitkub spot

## Running Tests

```bash
cd ~/btcbot
.venv/bin/pytest tests/ -v
.venv/bin/ruff check btc_bot/
```

## Disclaimer

This bot trades real money. Past backtest results do not guarantee future performance.
Test on small capital first. The 70% win rate target depends on market conditions and
is most achievable in clearly trending periods — expect lower win rates in choppy markets.
