"""Market data providers for NSE equities and MCX commodities.

- YFinanceProvider: free, ~15 min delayed. Equities via `<SYMBOL>.NS`. MCX commodities are
  proxied with international futures (GC=F, CL=F, ...) converted to INR with USDINR=X; these are
  approximations, labelled as such in every response.
- AngelOneProvider: Angel One SmartAPI (free with an Angel One account), real NSE + MCX data.
- SyntheticProvider: deterministic random walk for tests / offline demos.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.config import get_settings

log = logging.getLogger(__name__)

# MCX commodity -> (yfinance proxy ticker, unit note)
COMMODITIES: dict[str, tuple[str, str]] = {
    "GOLD": ("GC=F", "proxy: COMEX gold USD/oz -> INR"),
    "SILVER": ("SI=F", "proxy: COMEX silver USD/oz -> INR"),
    "CRUDEOIL": ("CL=F", "proxy: NYMEX WTI USD/bbl -> INR"),
    "NATURALGAS": ("NG=F", "proxy: NYMEX natgas USD/mmBtu -> INR"),
    "COPPER": ("HG=F", "proxy: COMEX copper USD/lb -> INR"),
}


def is_commodity(symbol: str) -> bool:
    return symbol.upper() in COMMODITIES


def normalize(symbol: str) -> str:
    s = symbol.upper().strip()
    for suffix in (".NS", ".BO", "-EQ"):
        s = s.removesuffix(suffix)
    return s.removeprefix("NSE:").removeprefix("MCX:")


class MarketDataProvider(ABC):
    name = "base"

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, ttl: float):
        hit = self._cache.get(key)
        return hit[1] if hit and time.time() - hit[0] < ttl else None

    def _store(self, key: str, value):
        self._cache[key] = (time.time(), value)
        return value

    @abstractmethod
    async def quote(self, symbol: str) -> dict: ...

    @abstractmethod
    async def history(self, symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
        """OHLCV, oldest first, columns open/high/low/close/volume, DatetimeIndex."""


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"

    def _ticker(self, symbol: str) -> str:
        s = normalize(symbol)
        return COMMODITIES[s][0] if s in COMMODITIES else f"{s}.NS"

    async def _usdinr(self) -> float:
        cached = self._cached("USDINR", 600)
        if cached:
            return cached
        df = await self._download("USDINR=X", "5d", "1d")
        return self._store("USDINR", float(df["close"].iloc[-1]) if len(df) else 88.0)

    async def _download(self, ticker: str, period: str, interval: str) -> pd.DataFrame:
        import yfinance as yf

        def _get():
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
            if df is None or df.empty:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            return df.dropna(subset=["close"])

        return await asyncio.to_thread(_get)

    async def history(self, symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
        s = normalize(symbol)
        key = f"h:{s}:{period}:{interval}"
        ttl = 60 if interval.endswith("m") else 900
        if (c := self._cached(key, ttl)) is not None:
            return c
        df = await self._download(self._ticker(s), period, interval)
        if s in COMMODITIES and len(df):
            fx = await self._usdinr()
            df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] * fx
        if df.empty:
            raise ValueError(f"No data for {s}. Use NSE symbols like RELIANCE or MCX names like GOLD.")
        return self._store(key, df)

    async def quote(self, symbol: str) -> dict:
        s = normalize(symbol)
        key = f"q:{s}"
        if (c := self._cached(key, get_settings().quote_cache_seconds)) is not None:
            return c
        df = await self.history(s, "5d", "5m")
        daily = await self.history(s, "1mo", "1d")
        last = float(df["close"].iloc[-1])
        prev_close = float(daily["close"].iloc[-2]) if len(daily) > 1 else last
        q = {
            "symbol": s,
            "segment": "MCX" if s in COMMODITIES else "NSE",
            "ltp": round(last, 2),
            "prev_close": round(prev_close, 2),
            "change_pct": round((last / prev_close - 1) * 100, 2),
            "day_high": round(float(daily["high"].iloc[-1]), 2),
            "day_low": round(float(daily["low"].iloc[-1]), 2),
            "as_of": str(df.index[-1]),
            "source": "yfinance (delayed)" + (f"; {COMMODITIES[s][1]}" if s in COMMODITIES else ""),
        }
        return self._store(key, q)


class AngelOneProvider(MarketDataProvider):
    """Angel One SmartAPI. Needs ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET."""

    name = "angelone"
    SCRIP_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    INTERVALS = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE", "1h": "ONE_HOUR", "1d": "ONE_DAY"}

    def __init__(self) -> None:
        super().__init__()
        self._api = None
        self._tokens: dict[str, tuple[str, str]] = {}  # symbol -> (exchange, token)
        self._lock = asyncio.Lock()

    async def _client(self):
        async with self._lock:
            if self._api is None:
                import pyotp
                from SmartApi import SmartConnect

                s = get_settings()
                api = SmartConnect(api_key=s.angel_api_key)
                await asyncio.to_thread(api.generateSession, s.angel_client_code, s.angel_pin,
                                        pyotp.TOTP(s.angel_totp_secret).now())
                self._api = api
                await self._load_tokens()
        return self._api

    async def _load_tokens(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=60) as c:
            rows = (await c.get(self.SCRIP_MASTER)).json()
        today = datetime.now().date()
        nearest: dict[str, tuple[datetime, str]] = {}
        for r in rows:
            if r.get("exch_seg") == "NSE" and r.get("symbol", "").endswith("-EQ"):
                self._tokens[r["name"]] = ("NSE", r["token"])
            elif r.get("exch_seg") == "MCX" and r.get("instrumenttype") == "FUTCOM" and r.get("name") in COMMODITIES:
                try:
                    exp = datetime.strptime(r["expiry"], "%d%b%Y")
                except (KeyError, ValueError):
                    continue
                if exp.date() >= today and (r["name"] not in nearest or exp < nearest[r["name"]][0]):
                    nearest[r["name"]] = (exp, r["token"])
        for name, (_, token) in nearest.items():  # front-month MCX future
            self._tokens[name] = ("MCX", token)

    async def _lookup(self, symbol: str) -> tuple[str, str]:
        await self._client()
        s = normalize(symbol)
        if s not in self._tokens:
            raise ValueError(f"Unknown symbol {s}")
        return self._tokens[s]

    async def quote(self, symbol: str) -> dict:
        s = normalize(symbol)
        if (c := self._cached(f"q:{s}", get_settings().quote_cache_seconds)) is not None:
            return c
        exch, token = await self._lookup(s)
        api = await self._client()
        data = await asyncio.to_thread(api.getMarketData, "FULL", {exch: [token]})
        d = data["data"]["fetched"][0]
        q = {
            "symbol": s, "segment": exch, "ltp": d["ltp"], "prev_close": d["close"],
            "change_pct": round(d.get("percentChange", 0.0), 2),
            "day_high": d["high"], "day_low": d["low"],
            "as_of": d.get("exchFeedTime"), "source": "Angel One SmartAPI (live)",
        }
        return self._store(f"q:{s}", q)

    async def history(self, symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
        s = normalize(symbol)
        key = f"h:{s}:{period}:{interval}"
        if (c := self._cached(key, 60 if interval.endswith("m") else 900)) is not None:
            return c
        exch, token = await self._lookup(s)
        api = await self._client()
        days = {"5d": 5, "1mo": 31, "3mo": 92, "6mo": 183, "1y": 365, "2y": 730}.get(period, 183)
        end = datetime.now()
        params = {
            "exchange": exch, "symboltoken": token, "interval": self.INTERVALS.get(interval, "ONE_DAY"),
            "fromdate": (end - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M"),
        }
        res = await asyncio.to_thread(api.getCandleData, params)
        df = pd.DataFrame(res["data"], columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("ts"))
        return self._store(key, df)


class SyntheticProvider(MarketDataProvider):
    """Deterministic prices per symbol, for tests and offline demos."""

    name = "synthetic"

    def _seed(self, s: str) -> int:
        return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)

    async def history(self, symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
        s = normalize(symbol)
        rng = np.random.default_rng(self._seed(s))
        n = 200
        base = 500 + self._seed(s) % 3000
        close = base * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
        idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="B")
        open_ = np.r_[close[0], close[:-1]]
        high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
        low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
        vol = rng.integers(100_000, 2_000_000, n)
        return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)

    async def quote(self, symbol: str) -> dict:
        s = normalize(symbol)
        df = await self.history(s)
        last, prev = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        return {"symbol": s, "segment": "MCX" if is_commodity(s) else "NSE", "ltp": round(last, 2),
                "prev_close": round(prev, 2), "change_pct": round((last / prev - 1) * 100, 2),
                "day_high": round(float(df["high"].iloc[-1]), 2), "day_low": round(float(df["low"].iloc[-1]), 2),
                "as_of": str(df.index[-1]), "source": "synthetic"}


_provider: MarketDataProvider | None = None


def get_provider() -> MarketDataProvider:
    global _provider
    if _provider is None:
        kind = get_settings().market_data_provider
        _provider = {"yfinance": YFinanceProvider, "angelone": AngelOneProvider, "synthetic": SyntheticProvider}[kind]()
    return _provider


def set_provider(p: MarketDataProvider) -> None:
    global _provider
    _provider = p
