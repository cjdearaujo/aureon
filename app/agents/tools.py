"""Tools the agents can call. Each is a plain async function wrapped by LangChain's @tool;
the docstring is what the model reads to decide when and how to call it."""
from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Literal

import numpy as np
from langchain_core.tools import tool

from app import risk
from app.broker.paper import broker, estimate_charges
from app.config import get_settings
from app.finance_models import consult_fin_r1, fetch_news, finbert_sentiment
from app.market.data import COMMODITIES, get_provider, normalize
from app.market.hours import is_market_open, market_status
from app.market.indicators import atr, technical_summary

# Which user's paper account the current agent run acts on (set by the runner).
current_account: ContextVar[str] = ContextVar("current_account", default="demo")


def _j(x) -> str:
    return json.dumps(x, default=str)


# ---------------------------------------------------------------- market data
@tool
async def get_market_status() -> str:
    """Current NSE/MCX session status and time in IST."""
    return _j(market_status())


@tool
async def get_quote(symbol: str) -> str:
    """Latest price for an NSE stock (e.g. RELIANCE) or MCX commodity (GOLD, SILVER, CRUDEOIL, NATURALGAS, COPPER)."""
    return _j(await get_provider().quote(symbol))


@tool
async def get_price_history(symbol: str, period: Literal["5d", "1mo", "3mo", "6mo", "1y", "2y"] = "3mo",
                            interval: Literal["5m", "15m", "1h", "1d"] = "1d") -> str:
    """OHLCV candles for a symbol. Use 5m/15m with 5d for intraday, 1d with 3mo-1y for swing."""
    df = await get_provider().history(symbol, period, interval)
    tail = df.tail(30).round(2)
    return _j({"symbol": normalize(symbol), "bars": len(df), "first": str(df.index[0]), "last": str(df.index[-1]),
               "period_high": round(float(df["high"].max()), 2), "period_low": round(float(df["low"].min()), 2),
               "recent_30": [[str(i)[:16], *r] for i, r in zip(tail.index, tail.values.tolist())]})


@tool
async def technical_analysis(symbol: str, timeframe: Literal["intraday", "swing"] = "swing") -> str:
    """Indicator summary (SMA20/50, RSI14, MACD, ATR, Bollinger, volume, 52w range, rule-based score)."""
    period, interval = ("5d", "15m") if timeframe == "intraday" else ("1y", "1d")
    df = await get_provider().history(symbol, period, interval)
    return _j({"symbol": normalize(symbol), "timeframe": timeframe, **technical_summary(df)})


@tool
async def scan_watchlist() -> str:
    """Quick technical scan of the whole watchlist, sorted by rule score. Start here to find candidates."""
    out = []
    for sym in get_settings().watchlist:
        try:
            t = technical_summary(await get_provider().history(sym, "1y", "1d"))
            out.append({"symbol": sym, "close": t["last_close"], "chg_1d": t["change_1d_pct"], "rsi": t["rsi14"],
                        "score": t["rule_score"], "signal": t["rule_signal"]})
        except Exception as exc:
            out.append({"symbol": sym, "error": str(exc)[:100]})
    return _j(sorted(out, key=lambda r: -r.get("score", -99)))


# ---------------------------------------------------------------- research
@tool
async def news_sentiment(symbol: str) -> str:
    """Recent news headlines for a symbol scored by FinBERT (finance-tuned sentiment model)."""
    s = normalize(symbol)
    query = f"{s} commodity price" if s in COMMODITIES else f"{s} share NSE"
    news = await fetch_news(query, symbol=s)
    if not news:
        return _j({"symbol": s, "headlines": [], "note": "News feeds unavailable right now; judge on price data."})
    try:
        scores = await finbert_sentiment([n["title"] for n in news])
    except Exception as exc:  # never let the sentiment model break research
        return _j({"symbol": s, "headlines": news, "finbert_error": str(exc)[:200]})
    for n, sc in zip(news, scores):
        n["finbert"] = sc
    avg = round(float(np.mean([sc["net"] for sc in scores])), 3) if scores else 0.0
    return _j({"symbol": s, "avg_net_sentiment": avg, "headlines": news,
               "model": "ProsusAI/finbert", "scale": "-1 bearish .. +1 bullish"})


@tool
async def ask_fin_r1(question: str) -> str:
    """Consult Fin-R1, a 7B LLM fine-tuned on financial reasoning (Hugging Face). Use it for valuation logic,
    financial calculations, or a second opinion on a trade thesis. Include all relevant numbers in the question."""
    return _j(await consult_fin_r1(question))


# ---------------------------------------------------------------- quant
@tool
async def backtest_strategy(symbol: str, strategy: Literal["sma_crossover", "rsi_reversion", "breakout"] = "sma_crossover",
                            period: Literal["1y", "2y"] = "2y") -> str:
    """Vectorised daily backtest (long-only, after ~0.15% round-trip costs) vs buy-and-hold.
    sma_crossover: long when SMA20>SMA50. rsi_reversion: buy RSI<30, exit RSI>55. breakout: long above 20d high."""
    from app.market.indicators import rsi

    df = await get_provider().history(symbol, period, "1d")
    c = df["close"]
    if strategy == "sma_crossover":
        pos = (c.rolling(20).mean() > c.rolling(50).mean()).astype(float)
    elif strategy == "rsi_reversion":
        r, state, vals = rsi(c), 0.0, []
        for v in r:
            state = 1.0 if v < 30 else 0.0 if v > 55 else state
            vals.append(state)
        pos = np.array(vals)
        pos = c.copy() * 0 + pos
    else:
        pos = (c > c.rolling(20).max().shift()).astype(float).replace(0, np.nan).ffill(limit=10).fillna(0)
    pos = pos.shift().fillna(0)
    ret = c.pct_change().fillna(0)
    strat = pos * ret - pos.diff().abs().fillna(0) * 0.00075
    eq, bh = (1 + strat).cumprod(), (1 + ret).cumprod()
    years = max(len(c) / 252, 0.1)
    trades = int((pos.diff() > 0).sum())
    dd = float((eq / eq.cummax() - 1).min())
    sharpe = float(strat.mean() / strat.std() * np.sqrt(252)) if strat.std() else 0.0
    return _j({"symbol": normalize(symbol), "strategy": strategy, "period": period, "trades": trades,
               "total_return_pct": round((eq.iloc[-1] - 1) * 100, 2),
               "cagr_pct": round((eq.iloc[-1] ** (1 / years) - 1) * 100, 2),
               "buy_hold_return_pct": round((bh.iloc[-1] - 1) * 100, 2),
               "max_drawdown_pct": round(dd * 100, 2), "sharpe": round(sharpe, 2),
               "time_in_market_pct": round(float(pos.mean()) * 100, 1),
               "currently_long_signal": bool(pos.iloc[-1] > 0)})


@tool
async def position_size(symbol: str, risk_per_trade_pct: float = 1.0, stop_atr_multiple: float = 2.0) -> str:
    """ATR-based position sizing: risk `risk_per_trade_pct` of equity with a stop `stop_atr_multiple` x ATR away.
    Returns quantity, stop-loss and target (2R) levels, capped by the max position limit."""
    from app.settings_store import RISK_PROFILES, get_prefs

    s = get_settings()
    mult = RISK_PROFILES[(await get_prefs(current_account.get()))["risk_profile"]]
    df = await get_provider().history(symbol, "6mo", "1d")
    px, a = float(df["close"].iloc[-1]), float(atr(df).iloc[-1])
    equity = (await broker.portfolio(current_account.get()))["equity"]
    stop_dist = a * stop_atr_multiple
    qty = int(equity * risk_per_trade_pct / 100 / stop_dist) if stop_dist else 0
    cap_qty = int(min(s.max_position_pct * mult * equity, s.max_order_value * mult) / px)
    qty = max(min(qty, cap_qty), 0)
    return _j({"symbol": normalize(symbol), "price": round(px, 2), "atr": round(a, 2), "quantity": qty,
               "capped_by_limits": qty == cap_qty, "position_value": round(qty * px, 2),
               "stop_loss": round(px - stop_dist, 2), "target_2R": round(px + 2 * stop_dist, 2),
               "max_loss_at_stop": round(qty * stop_dist, 2)})


@tool
async def estimate_trade_charges(symbol: str, side: Literal["BUY", "SELL"], quantity: int, price: float,
                                 product: Literal["CNC", "MIS"] = "CNC") -> str:
    """Indian statutory charges (STT/CTT, exchange, stamp duty, SEBI, GST, brokerage) for a trade."""
    return _j(estimate_charges(normalize(symbol), side, quantity, price, product))


# ---------------------------------------------------------------- risk / portfolio
@tool
async def get_portfolio() -> str:
    """Cash, equity, P&L and open positions of the paper account."""
    return _j(await broker.portfolio(current_account.get()))


@tool
async def check_trade_risk(symbol: str, side: Literal["BUY", "SELL"], quantity: int,
                           product: Literal["CNC", "MIS"] = "CNC") -> str:
    """Run a proposed trade through the hard risk limits (position size, order value, cash, daily loss)."""
    return _j(await risk.check_trade(current_account.get(), symbol, side, quantity, None, product))


@tool
async def list_orders(limit: int = 20) -> str:
    """Recent orders (filled, open, rejected) with fill prices, fees and realized P&L."""
    return _j(await broker.orders(current_account.get(), limit))


# ---------------------------------------------------------------- execution (HITL-gated)
@tool
async def place_order(symbol: str, side: Literal["BUY", "SELL"], quantity: int, rationale: str,
                      product: Literal["CNC", "MIS"] = "CNC", order_type: Literal["MARKET", "LIMIT"] = "MARKET",
                      limit_price: float | None = None, stop_loss: float | None = None,
                      target: float | None = None) -> str:
    """Place a PAPER order. Execution pauses for human approval before this runs.
    CNC = delivery/swing, MIS = intraday. Always include a clear rationale plus stop_loss and target."""
    acct = current_account.get()
    s = get_settings()
    if s.trading_halted:
        return _j({"status": "NOT_PLACED", "reason": "trading halted by the Aureon admin"})
    if s.enforce_market_hours and not is_market_open():
        return _j({"status": "NOT_PLACED", "reason": "market closed", **market_status()})
    check = await risk.check_trade(acct, symbol, side, quantity, limit_price, product)
    full_rationale = f"{rationale} | SL={stop_loss} TGT={target}"
    if not check["approved_by_risk_engine"]:
        return _j(await broker.reject(acct, symbol, side, quantity, "; ".join(check["violations"]), full_rationale,
                                      order_type, limit_price, product))
    return _j(await broker.place_order(acct, symbol, side, quantity, order_type, limit_price, product, full_rationale))


@tool
async def cancel_order(order_id: str) -> str:
    """Cancel a resting LIMIT order by id."""
    return _j(await broker.cancel_order(current_account.get(), order_id))


from app.agents.specialist_tools import (  # noqa: E402
    company_fundamentals, compliance_check, macro_snapshot, performance_report)

MARKET_TOOLS = [get_market_status, get_quote, get_price_history, technical_analysis, scan_watchlist]
RESEARCH_TOOLS = [*MARKET_TOOLS, news_sentiment, ask_fin_r1]
QUANT_TOOLS = [get_quote, get_price_history, technical_analysis, backtest_strategy, position_size,
               estimate_trade_charges, ask_fin_r1]
RISK_TOOLS = [get_portfolio, check_trade_risk, get_quote, technical_analysis, list_orders]
EXECUTION_TOOLS = [get_market_status, get_quote, estimate_trade_charges, check_trade_risk, list_orders]
MANAGER_TOOLS = [get_portfolio, get_market_status, get_quote, list_orders, place_order, cancel_order]
MACRO_TOOLS = [macro_snapshot, get_market_status, get_quote, technical_analysis]
FUNDAMENTAL_TOOLS = [company_fundamentals, news_sentiment, get_quote, ask_fin_r1]
BEAR_TOOLS = [technical_analysis, news_sentiment, company_fundamentals, backtest_strategy, macro_snapshot, get_portfolio]
COMPLIANCE_TOOLS = [compliance_check, get_market_status, get_quote, list_orders]
PERFORMANCE_TOOLS = [performance_report, get_portfolio, list_orders]
