"""MCP end-to-end: Aureon's MCP server speaks real MCP (Streamable HTTP), is protected by a bearer token,
and the Bear-Case Analyst sub-agent calls its tools through langchain-mcp-adapters."""
import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.agents import graph, mcp_client
from app.agents.runner import HedgeFundRunner
from app.config import get_settings
from app.db import init_db
from app.events import EventBus
from app.mcp_server import asgi_app, running
from tests.test_agents import ScriptedModel

ASGI = asgi_app()


def _factory(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ASGI), base_url="http://aureon",
                             headers=headers, timeout=timeout, auth=auth)


@pytest.fixture(scope="module")
async def mcp_running():
    await init_db()
    st = get_settings()
    old = (st.mcp_enabled, st.app_passcode)
    st.mcp_enabled, st.app_passcode = True, "client-code"
    conns = {"aureon": {"transport": "streamable_http", "url": "http://aureon/", "httpx_client_factory": _factory,
                        "headers": {"Authorization": "Bearer client-code"}}}
    orig = mcp_client.connections
    mcp_client.connections = lambda: conns
    mcp_client.reset_cache()
    import asyncio

    started, stop = asyncio.Event(), asyncio.Event()

    async def serve():  # enter and exit the MCP session manager in one task (anyio requirement)
        async with running():
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    await started.wait()
    yield
    stop.set()
    await task
    mcp_client.connections = orig
    mcp_client.reset_cache()
    st.mcp_enabled, st.app_passcode = old


async def test_mcp_server_lists_and_runs_tools(mcp_running):
    tools = await mcp_client.load_mcp_tools(force=True)
    names = {t.name for t in tools}
    assert {"aureon_market_quote", "aureon_technical_snapshot", "aureon_fundamentals",
            "aureon_news_sentiment_finbert", "aureon_market_regime"} <= names
    quote = next(t for t in tools if t.name == "aureon_market_quote")
    out = await quote.ainvoke({"symbol": "RELIANCE"})
    text = out if isinstance(out, str) else json.dumps(out)
    assert "RELIANCE" in text and "ltp" in text


async def test_mcp_requires_token(mcp_running):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ASGI), base_url="http://aureon") as c:
        r = await c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401


async def test_bear_case_analyst_uses_mcp(mcp_running, monkeypatch):
    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{"id": "b1", "name": "task", "args": {
            "subagent_type": "bear-case-analyst", "description": "Argue against buying TCS."}}]),
        AIMessage(content="", tool_calls=[{"id": "m1", "name": "aureon_technical_snapshot", "args": {"symbol": "TCS"}}]),
        AIMessage(content="1. RSI stretched. PROCEED WITH CAUTION"),
        AIMessage(content="Bear case noted: proceed with caution."),
    ])
    monkeypatch.setattr(graph, "make_model", lambda spec: model)
    r = HedgeFundRunner(EventBus())
    await r.start()
    res = await r.chat("gina", "gina:main", "What's the bear case on TCS?")
    assert res["status"] == "done", res
    from app.db import AgentRun, SessionLocal
    async with SessionLocal() as s:
        steps = (await s.get(AgentRun, res["run_id"])).steps
    mcp_calls = [x for x in steps if x["agent"] == "bear-case-analyst" and x["kind"] == "tool_call"]
    assert mcp_calls and "aureon_technical_snapshot" in mcp_calls[0]["text"]
    results = [x for x in steps if x.get("tool") == "aureon_technical_snapshot"]
    assert results and "rsi14" in results[0]["text"]
