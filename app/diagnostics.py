"""Live diagnostics: exercises every external dependency once and reports pass/fail with timings.
Run from the Broker console ("Run diagnostics") or automatically at the end of deploy.sh."""
from __future__ import annotations

import asyncio
import io
import json
import time
import wave

from app.config import get_settings


async def _check(name: str, coro, timeout: float = 90) -> dict:
    t = time.perf_counter()
    try:
        detail = await asyncio.wait_for(coro, timeout)
        return {"check": name, "ok": True, "seconds": round(time.perf_counter() - t, 1), "detail": str(detail)[:160]}
    except Exception as exc:
        return {"check": name, "ok": False, "seconds": round(time.perf_counter() - t, 1),
                "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


async def _db():
    from sqlalchemy import text

    from app.db import SessionLocal

    async with SessionLocal() as s:
        return (await s.execute(text("select 1"))).scalar()


async def _quote(sym):
    from app.market.data import get_provider

    q = await get_provider().quote(sym)
    return f"{q['symbol']} ₹{q['ltp']} ({q['source']})"


async def _candles(sym, period, interval):
    from app.market.data import get_provider

    df = await get_provider().history(sym, period, interval)
    return f"{len(df)} bars, last {df.index[-1]}"


async def _tool(tool, args):
    out = json.loads(await tool.ainvoke(args))
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(out["error"])
    return json.dumps(out)[:160]


async def _news(source):
    from app import finance_models as fm

    items = await (fm._google_news("RELIANCE share NSE", 3) if source == "google" else fm._yahoo_news("RELIANCE", 3))
    if not items:
        raise RuntimeError("no headlines returned")
    return items[0]["title"]


async def _finbert():
    from app.finance_models import finbert_sentiment

    r = await finbert_sentiment(["Reliance shares surge after record quarterly profit"])
    return r[0]


async def _whisper():
    from app.voice import transcribe

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:  # 1 s of silence: proves model load + audio decoding
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000), w.writeframes(b"\x00\x00" * 16000)
    text = await transcribe(buf.getvalue(), ".wav")
    return f"model loaded, transcript={text!r}"


async def _mcp():
    from app.agents.mcp_client import load_mcp_tools

    tools = await load_mcp_tools(force=True)
    if not tools:
        raise RuntimeError("no MCP tools loaded")
    return [t.name for t in tools]


async def _llm():
    from app.main import llm_check

    r = await llm_check()
    if not r["all_ok"]:
        raise RuntimeError(json.dumps(r["models"]))
    return r["models"]


async def run_diagnostics() -> dict:
    from app.agents import specialist_tools as st
    from app.agents import tools as T

    s = get_settings()
    checks = [
        ("Database (Neon)", _db()),
        ("Quote: NSE stock", _quote("RELIANCE")),
        ("Quote: MCX commodity (proxy)", _quote("GOLD")),
        ("Daily candles", _candles("RELIANCE", "6mo", "1d")),
        ("Intraday candles", _candles("TCS", "5d", "15m")),
        ("Technical analysis", _tool(T.technical_analysis, {"symbol": "INFY"})),
        ("Macro regime", _tool(st.macro_snapshot, {})),
        ("Fundamentals", _tool(st.company_fundamentals, {"symbol": "INFY"})),
        ("News: Google News", _news("google")),
        ("News: Yahoo fallback", _news("yahoo")),
        ("FinBERT model", _finbert()),
        ("Whisper model", _whisper()),
        ("MCP server + client", _mcp()),
        ("Gemini models", _llm()),
    ]
    results = []
    for name, coro in checks:  # sequential: gentle on free data sources
        results.append(await _check(name, coro))
    critical = {"Database (Neon)", "Quote: NSE stock", "Daily candles", "Gemini models"}
    news_ok = any(r["ok"] for r in results if r["check"].startswith("News"))
    return {"all_ok": all(r["ok"] for r in results),
            "critical_ok": all(r["ok"] for r in results if r["check"] in critical) and news_ok,
            "data_provider": s.market_data_provider, "results": results}
