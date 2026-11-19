"""Tools for the specialist sub-agents: Macro, Fundamental, Compliance and Performance.
(The Bear-Case Analyst reuses research tools.)"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
from langchain_core.tools import tool

from app.broker.paper import broker
from app.config import get_settings
from app.market.data import COMMODITIES, get_provider, is_commodity, normalize
from app.market.hours import IST, is_market_open, now_ist

MACRO = {"NIFTY 50": "^NSEI", "BANK NIFTY": "^NSEBANK", "INDIA VIX": "^INDIAVIX", "USD/INR": "USDINR=X",
         "BRENT CRUDE": "BZ=F", "S&P 500": "^GSPC"}


def _j(x) -> str:
    return json.dumps(x, default=str)


def _num(x, n=2):
    try:
        x = float(x)
        return None if math.isnan(x) else round(x, n)
    except (TypeError, ValueError):
        return None


async def _series(name: str, ticker: str):
    """Daily closes for an index / FX / commodity benchmark."""
    if get_settings().market_data_provider == "synthetic":
        return (await get_provider().history(name.replace(" ", ""), "1y", "1d"))["close"]
    from app.market.data import YFinanceProvider

    p = get_provider() if isinstance(get_provider(), YFinanceProvider) else YFinanceProvider()
    return (await p._download(ticker, "1y", "1d"))["close"]


# ---------------------------------------------------------------- Macro Strategist
@tool
async def macro_snapshot() -> str:
    """Market regime dashboard: NIFTY 50, Bank Nifty, India VIX, USD/INR, Brent crude, S&P 500 with
    1d/5d/20d moves and trend vs 50-day average, plus a rule-based risk-on / risk-off read."""
    out, score = {}, 0
    for name, tk in MACRO.items():
        try:
            c = await _series(name, tk)
            last, sma50 = float(c.iloc[-1]), float(c.rolling(50).mean().iloc[-1])
            chg = lambda n: _num((last / float(c.iloc[-n - 1]) - 1) * 100) if len(c) > n else None
            out[name] = {"last": _num(last), "chg_1d_pct": chg(1), "chg_5d_pct": chg(5), "chg_20d_pct": chg(20),
                         "above_50dma": last > sma50}
        except Exception as exc:
            out[name] = {"error": str(exc)[:80]}
    n, vix = out.get("NIFTY 50", {}), out.get("INDIA VIX", {})
    if n.get("above_50dma"):
        score += 1
    if (n.get("chg_20d_pct") or 0) > 0:
        score += 1
    if vix.get("last") is not None:
        score += 1 if vix["last"] < 16 else -1 if vix["last"] > 22 else 0
    if (out.get("S&P 500", {}).get("chg_5d_pct") or 0) < -3:
        score -= 1
    regime = "risk-on" if score >= 2 else "risk-off" if score <= 0 else "neutral"
    return _j({"regime": regime, "regime_score": score, "markets": out, "as_of_ist": now_ist().strftime("%Y-%m-%d %H:%M")})


# ---------------------------------------------------------------- Fundamental Analyst
async def _fundamentals(symbol: str) -> dict:
    s = normalize(symbol)
    if is_commodity(s):
        return {"symbol": s, "note": "Commodity: no company fundamentals. Use macro (USD/INR, global prices)."}
    if get_settings().market_data_provider == "synthetic":
        h = int(hashlib.md5(s.encode()).hexdigest()[:6], 16)
        return {"symbol": s, "pe": 12 + h % 40, "pb": round(1 + (h % 70) / 10, 1), "roe_pct": 8 + h % 22,
                "debt_to_equity": round((h % 150) / 100, 2), "profit_margin_pct": 5 + h % 25,
                "revenue_growth_pct": -5 + h % 30, "next_results": None, "source": "synthetic"}
    import yfinance as yf

    def _get():
        t = yf.Ticker(f"{s}.NS")
        info = t.info or {}
        try:
            cal = t.calendar or {}
            ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
            next_results = str(ed[0]) if ed else None
        except Exception:
            next_results = None
        pct = lambda k: _num((info.get(k) or 0) * 100) if info.get(k) is not None else None
        return {"symbol": s, "name": info.get("longName"), "sector": info.get("sector"), "industry": info.get("industry"),
                "market_cap_cr": _num((info.get("marketCap") or 0) / 1e7, 0), "pe": _num(info.get("trailingPE")),
                "forward_pe": _num(info.get("forwardPE")), "pb": _num(info.get("priceToBook")),
                "roe_pct": pct("returnOnEquity"), "debt_to_equity": _num((info.get("debtToEquity") or 0) / 100),
                "profit_margin_pct": pct("profitMargins"), "revenue_growth_pct": pct("revenueGrowth"),
                "earnings_growth_pct": pct("earningsGrowth"), "dividend_yield_pct": _num(info.get("dividendYield")),
                "beta": _num(info.get("beta")), "next_results": next_results, "source": "yfinance"}

    return await asyncio.to_thread(_get)


@tool
async def company_fundamentals(symbol: str) -> str:
    """Valuation and quality for an NSE stock: P/E, P/B, ROE, debt/equity, margins, growth, beta,
    sector and the next results date."""
    return _j(await _fundamentals(symbol))


# ---------------------------------------------------------------- Compliance Officer
@tool
async def compliance_check(symbol: str, side: str, quantity: int, product: str = "CNC") -> str:
    """Exchange / SEBI-style pre-trade checks: market session, MIS square-off cutoff (15:20 IST),
    price-band (circuit) proximity, liquidity vs 20-day traded value, results-day blackout."""
    s, product = normalize(symbol), product.upper()
    flags, blocks = [], []
    now = now_ist()
    if get_settings().enforce_market_hours and not is_market_open(now):
        blocks.append("market is closed")
    if product == "MIS" and now.time() >= datetime.strptime("15:20", "%H:%M").time():
        blocks.append("past 15:20 IST: new MIS (intraday) positions are not allowed; brokers auto square-off")
    q = await get_provider().quote(s)
    move = abs(q.get("change_pct") or 0)
    if not is_commodity(s) and move >= 9:
        flags.append(f"price moved {move:.1f}% today: close to a 10%/20% price band (upper/lower circuit)")
    df = await get_provider().history(s, "3mo", "1d")
    adv = float((df["close"] * df["volume"]).tail(20).mean())
    value = quantity * float(q["ltp"])
    if adv and value > 0.01 * adv:
        flags.append(f"order is {value / adv:.2%} of 20-day avg traded value: may move the price")
    if not is_commodity(s):
        nr = (await _fundamentals(s)).get("next_results")
        if nr:
            try:
                d = datetime.fromisoformat(str(nr)[:10]).date()
                if abs((d - now.date()).days) <= 2:
                    flags.append(f"results due {d}: event risk (results-day blackout)")
            except ValueError:
                pass
    status = "BLOCK" if blocks else "PASS WITH FLAGS" if flags else "PASS"
    return _j({"symbol": s, "side": side, "quantity": quantity, "product": product, "status": status,
               "blocks": blocks, "flags": flags, "order_value": round(value, 2), "avg_traded_value_20d": round(adv, 0),
               "checked_at_ist": now.strftime("%Y-%m-%d %H:%M"),
               "audit_note": f"{status}: {side} {quantity} {s} {product} at ~₹{q['ltp']}"})


# ---------------------------------------------------------------- Performance Analyst
@tool
async def performance_report(days: int = 30) -> str:
    """Post-trade analytics for this account over the last N days: closed-trade win rate, average win/loss,
    profit factor, charges, equity return, max drawdown, best/worst symbols, plus open positions."""
    from app.agents.tools import current_account
    from sqlalchemy import select

    from app.db import EquitySnapshot, Order, SessionLocal

    acct = current_account.get()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with SessionLocal() as s:
        orders = (await s.scalars(select(Order).where(Order.account_id == acct, Order.created_at >= since))).all()
        snaps = (await s.scalars(select(EquitySnapshot).where(EquitySnapshot.account_id == acct)
                                 .order_by(EquitySnapshot.taken_at))).all()
    filled = [o for o in orders if o.status == "FILLED"]
    closes = [o for o in filled if o.realized_pnl]
    wins = [o.realized_pnl for o in closes if o.realized_pnl > 0]
    losses = [o.realized_pnl for o in closes if o.realized_pnl < 0]
    by_sym: dict[str, float] = {}
    for o in closes:
        by_sym[o.symbol] = by_sym.get(o.symbol, 0) + o.realized_pnl
    eq = np.array([x.equity for x in snaps]) if snaps else np.array([])
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min() * 100) if len(eq) else 0.0
    pf = await broker.portfolio(acct)
    return _j({
        "window_days": days, "orders": len(orders), "filled": len(filled),
        "rejected": sum(o.status == "REJECTED" for o in orders), "closed_trades": len(closes),
        "win_rate_pct": round(len(wins) / len(closes) * 100, 1) if closes else None,
        "avg_win": round(np.mean(wins), 2) if wins else None, "avg_loss": round(np.mean(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
        "charges_paid": round(sum(o.fees for o in filled), 2), "total_return_pct": pf["total_return_pct"],
        "max_drawdown_pct": round(mdd, 2), "best_symbols": sorted(by_sym.items(), key=lambda x: -x[1])[:3],
        "worst_symbols": sorted(by_sym.items(), key=lambda x: x[1])[:3], "open_positions": pf["positions"],
        "rejection_reasons": [o.reject_reason for o in orders if o.reject_reason][:5]})
