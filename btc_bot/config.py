"""Centralised configuration loaded from .env via pydantic-settings.

All tunable parameters live here. The bot, strategy, risk, and backtester
modules accept a `BotConfig` instance — no module reads env vars directly.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Exchange ─────────────────────────────────────
    bitkub_api_key: str = ""
    bitkub_api_secret: str = ""
    bitkub_host: str = "https://api.bitkub.com"

    # ─── Risk Management ──────────────────────────────
    risk_per_trade_pct: float = Field(default=2.0, gt=0, le=10)
    max_open_trades: int = Field(default=2, ge=1, le=10)
    daily_loss_limit_pct: float = Field(default=3.0, gt=0, le=20)
    max_portfolio_heat_pct: float = Field(default=6.0, gt=0, le=50)
    cooldown_after_losses: int = Field(default=3, ge=1)
    cooldown_hours: int = Field(default=6, ge=0)
    min_position_thb: float = Field(default=500.0, ge=10)

    # ─── Strategy ─────────────────────────────────────
    zone_width_pct: float = Field(default=3.0, gt=0)
    zone_lookback: int = Field(default=30, ge=10)
    swing_lookback_left: int = Field(default=3, ge=1)
    swing_lookback_right: int = Field(default=3, ge=1)
    swing_lookback_1h: int = Field(default=2, ge=1)
    retest_proximity_pct: float = Field(default=1.0, gt=0)
    min_bos_strength_pct: float = Field(default=0.3, ge=0)
    min_zone_size_pct: float = Field(default=0.3, gt=0)
    max_zone_age_bars: int = Field(default=100, ge=10)
    zone_padding_pct: float = Field(default=0.2, ge=0)
    rejection_min_wick_ratio: float = Field(default=1.5, gt=0)
    rejection_min_body_pct: float = Field(default=0.4, ge=0)
    structure_filter: str = Field(default="trend_only")

    # ─── Exits ────────────────────────────────────────
    trailing_stop_pct: float = Field(default=1.0, gt=0)
    tp1_rr_ratio: float = Field(default=2.0, gt=0)
    tp2_rr_ratio: float = Field(default=4.0, gt=0)
    partial_exit_pct: float = Field(default=50.0, gt=0, le=100)
    max_hold_hours: int = Field(default=96, ge=1)

    # ─── Storage ──────────────────────────────────────
    state_dir: str = "~/.btcbot"

    @field_validator("structure_filter")
    @classmethod
    def _validate_filter(cls, v: str) -> str:
        if v not in ("trend_only", "any"):
            raise ValueError("structure_filter must be 'trend_only' or 'any'")
        return v

    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "state.json"

    def log_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "log.json"
