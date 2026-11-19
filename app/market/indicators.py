"""Technical indicators computed with pandas, plus a simple rule-based signal summary.

The agents use these as evidence; the LLM interprets them, it does not compute them.
"""
from __future__ import annotations

import math

import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100)


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def technical_summary(df: pd.DataFrame) -> dict:
    """df needs columns open, high, low, close, volume (oldest first)."""
    if len(df) < 30:
        return {"error": f"need >=30 bars, got {len(df)}"}
    c = df["close"]
    last = float(c.iloc[-1])
    sma20, sma50 = c.rolling(20).mean(), c.rolling(50).mean()
    r = rsi(c)
    m_line, m_sig, m_hist = macd(c)
    a = atr(df)
    bb_mid, bb_std = sma20, c.rolling(20).std()
    vol_avg = df["volume"].rolling(20).mean()

    score, reasons = 0, []
    if not math.isnan(sma50.iloc[-1]):
        if sma20.iloc[-1] > sma50.iloc[-1]:
            score += 1; reasons.append("SMA20 above SMA50 (uptrend)")
        else:
            score -= 1; reasons.append("SMA20 below SMA50 (downtrend)")
    if last > sma20.iloc[-1]:
        score += 1; reasons.append("price above SMA20")
    else:
        score -= 1; reasons.append("price below SMA20")
    rv = float(r.iloc[-1])
    if rv < 30:
        score += 1; reasons.append(f"RSI {rv:.0f} oversold")
    elif rv > 70:
        score -= 1; reasons.append(f"RSI {rv:.0f} overbought")
    if m_hist.iloc[-1] > 0 and m_hist.iloc[-2] <= 0:
        score += 2; reasons.append("MACD bullish crossover")
    elif m_hist.iloc[-1] < 0 and m_hist.iloc[-2] >= 0:
        score -= 2; reasons.append("MACD bearish crossover")
    elif m_hist.iloc[-1] > 0:
        score += 1; reasons.append("MACD histogram positive")
    else:
        score -= 1; reasons.append("MACD histogram negative")

    signal = "BUY" if score >= 3 else "SELL" if score <= -3 else "HOLD"
    ret = lambda n: _r((last / float(c.iloc[-n - 1]) - 1) * 100) if len(c) > n else None
    return {
        "last_close": _r(last),
        "change_1d_pct": ret(1),
        "change_5d_pct": ret(5),
        "change_20d_pct": ret(20),
        "sma20": _r(sma20.iloc[-1]),
        "sma50": _r(sma50.iloc[-1]),
        "rsi14": _r(rv, 1),
        "macd": _r(m_line.iloc[-1], 3),
        "macd_signal": _r(m_sig.iloc[-1], 3),
        "atr14": _r(a.iloc[-1]),
        "atr_pct": _r(a.iloc[-1] / last * 100),
        "bollinger_upper": _r(bb_mid.iloc[-1] + 2 * bb_std.iloc[-1]),
        "bollinger_lower": _r(bb_mid.iloc[-1] - 2 * bb_std.iloc[-1]),
        "volume_vs_20d_avg": _r(df["volume"].iloc[-1] / vol_avg.iloc[-1]) if vol_avg.iloc[-1] else None,
        "high_52w": _r(df["high"].tail(252).max()),
        "low_52w": _r(df["low"].tail(252).min()),
        "rule_score": score,
        "rule_signal": signal,
        "reasons": reasons,
    }
