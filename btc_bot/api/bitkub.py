"""Bitkub REST API client (BTC/THB spot market).

The HMAC signing logic is copied verbatim from examples/btc_strategy_v5_zone.py
because Bitkub silently rejects mis-signed requests with error code 18.

API reference: https://github.com/bitkub/bitkub-official-api-docs
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import requests

from btc_bot.config import BotConfig


class BitkubClient:
    """Thin wrapper around the Bitkub v3 REST API.

    Methods that need signing (balances, orders) require API_KEY/SECRET.
    Read-only methods (price, ohlcv) work without credentials.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.host = config.bitkub_host

    # ─── Auth ────────────────────────────────────────────────────────
    def _gen_sign(self, payload: str) -> str:
        return hmac.new(
            self.config.bitkub_api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _server_time(self) -> str:
        # CRITICAL: must use Bitkub's server time, not local time
        try:
            return str(
                requests.get(f"{self.host}/api/v3/servertime", timeout=10).text.strip()
            )
        except requests.RequestException:
            return str(int(time.time() * 1000))

    def _make_headers(self, method: str, path: str, body_or_query: str = "") -> dict[str, str]:
        ts = self._server_time()
        sig = self._gen_sign(ts + method + path + body_or_query)
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-BTK-APIKEY": self.config.bitkub_api_key,
            "X-BTK-TIMESTAMP": ts,
            "X-BTK-SIGN": sig,
        }

    # ─── Public endpoints ────────────────────────────────────────────
    def get_price(self) -> dict[str, float] | None:
        """Return current BTC/THB ticker (last, bid, ask, high, low, volume, change)."""
        try:
            data = requests.get(
                f"{self.host}/api/v3/market/ticker", timeout=10
            ).json()
        except requests.RequestException as e:
            print(f"⚠️ Price error: {e}")
            return None

        if not isinstance(data, list):
            return None
        btc = next((t for t in data if t.get("symbol") == "BTC_THB"), None)
        if btc is None:
            return None
        return {
            "last": float(btc["last"]),
            "bid": float(btc["highest_bid"]),
            "ask": float(btc["lowest_ask"]),
            "high": float(btc["high_24_hr"]),
            "low": float(btc["low_24_hr"]),
            "volume": float(btc["quote_volume"]),
            "change": float(btc["percent_change"]),
        }

    def get_ohlcv(self, timeframe: str = "60", limit: int = 200) -> list[dict[str, Any]]:
        """Fetch OHLCV candles. timeframe in minutes ("60"=1h, "240"=4h)."""
        now = int(time.time())
        frm = now - (limit * int(timeframe) * 60)
        # Two known endpoint variants — try both; Bitkub has rotated them historically
        urls = [
            f"{self.host}/tradingview/history?symbol=BTC_THB&resolution={timeframe}&from={frm}&to={now}",
            f"{self.host}/api/market/tradingview?sym=BTC_THB&int={timeframe}&frm={frm}&to={now}",
        ]
        for url in urls:
            try:
                data = requests.get(url, timeout=15).json()
            except requests.RequestException:
                continue
            if data.get("s") != "ok":
                continue
            return [
                {
                    "time": data["t"][i],
                    "open": float(data["o"][i]),
                    "high": float(data["h"][i]),
                    "low": float(data["l"][i]),
                    "close": float(data["c"][i]),
                    "volume": float(data["v"][i]) if "v" in data else 0.0,
                }
                for i in range(len(data["t"]))
            ]
        return []

    # ─── Private endpoints (require API key) ─────────────────────────
    def get_balances(self) -> dict[str, float]:
        path = "/api/v3/market/balances"
        body = json.dumps({})
        try:
            data = requests.post(
                f"{self.host}{path}",
                headers=self._make_headers("POST", path, body),
                data=body,
                timeout=10,
            ).json()
        except requests.RequestException:
            return {"thb": 0.0, "btc": 0.0}

        if data.get("error") != 0:
            return {"thb": 0.0, "btc": 0.0}

        result = data.get("result", {})
        return {
            "thb": float(result.get("THB", {}).get("available", 0)),
            "btc": float(result.get("BTC", {}).get("available", 0)),
        }

    def place_buy(self, amount_thb: float, rate: float = 0, typ: str = "market") -> dict[str, Any]:
        """Place a buy order. amount_thb is in THB (not BTC). Returns Bitkub response."""
        path = "/api/v3/market/place-bid"
        body_dict = {"sym": "btc_thb", "amt": int(amount_thb), "rat": rate, "typ": typ}
        body = json.dumps(body_dict)
        try:
            return requests.post(
                f"{self.host}{path}",
                headers=self._make_headers("POST", path, body),
                data=body,
                timeout=10,
            ).json()
        except requests.RequestException as e:
            return {"error": -1, "message": str(e)}

    def place_sell(self, amount_btc: float, rate: float = 0, typ: str = "market") -> dict[str, Any]:
        """Place a sell order. amount_btc is in BTC."""
        path = "/api/v3/market/place-ask"
        body_dict = {"sym": "btc_thb", "amt": amount_btc, "rat": rate, "typ": typ}
        body = json.dumps(body_dict)
        try:
            return requests.post(
                f"{self.host}{path}",
                headers=self._make_headers("POST", path, body),
                data=body,
                timeout=10,
            ).json()
        except requests.RequestException as e:
            return {"error": -1, "message": str(e)}
