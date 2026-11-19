"""End-to-end test of the Deep Agents hedge fund with a scripted model (no API calls):
Manager proposes an order -> HITL pause -> human approves -> paper order fills -> memory exists."""
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents import graph
from app.agents.runner import HedgeFundRunner
from app.db import init_db
from app.events import EventBus


class ScriptedModel(BaseChatModel):
    script: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        msg = self.script.pop(0) if self.script else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.fixture(autouse=True, scope="module")
async def _db():
    await init_db()


async def test_hitl_order_flow(monkeypatch):
    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "place_order", "args": {
            "symbol": "INFY", "side": "BUY", "quantity": 5, "rationale": "test", "stop_loss": 1, "target": 2}}]),
        AIMessage(content="Bought 5 INFY. Journal updated."),
    ])
    monkeypatch.setattr(graph, "make_model", lambda spec: model)
    bus = EventBus()
    events = bus.subscribe()
    r = HedgeFundRunner(bus)
    await r.start()

    res = await r.chat("carol", "carol:main", "buy 5 infosys")
    assert res["status"] == "awaiting_approval", res
    pending = await r.pending("carol")
    assert pending and pending[0]["actions"][0]["args"]["symbol"] == "INFY"

    done = await r.decide("carol", pending[0]["id"], approve=True)
    assert done["status"] == "done", done
    assert "Bought" in done["reply"]

    from app.broker.paper import broker
    orders = await broker.orders("carol")
    assert orders[0]["status"] == "FILLED" and orders[0]["quantity"] == 5

    mems = await r.list_memories("carol")
    assert any(m["path"] == "/memories/AGENTS.md" and "Playbook" in m["content"] for m in mems)
    kinds = set()
    while not events.empty():
        kinds.add((await events.get())["type"])
    assert {"run_started", "approval_required", "approval_decided", "run_finished"} <= kinds


async def test_rejection_does_not_trade(monkeypatch):
    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{"id": "c2", "name": "place_order", "args": {
            "symbol": "TCS", "side": "BUY", "quantity": 1, "rationale": "x"}}]),
        AIMessage(content="Understood, not trading TCS."),
    ])
    monkeypatch.setattr(graph, "make_model", lambda spec: model)
    r = HedgeFundRunner(EventBus())
    await r.start()
    res = await r.chat("dave", "dave:main", "buy tcs")
    pid = (await r.pending("dave"))[0]["id"]
    out = await r.decide("dave", pid, approve=False, note="too expensive")
    from app.broker.paper import broker
    assert await broker.orders("dave") == []
    assert "not trading" in out["reply"]


async def test_manager_delegates_to_subagent_and_writes_memory(monkeypatch):
    """Manager -> task(risk-manager) -> sub-agent calls a tool -> Manager writes a journal entry to /memories."""
    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{"id": "t1", "name": "task", "args": {
            "subagent_type": "risk-manager", "description": "Check BUY 5 INFY CNC against limits."}}]),
        AIMessage(content="", tool_calls=[{"id": "r1", "name": "check_trade_risk", "args": {
            "symbol": "INFY", "side": "BUY", "quantity": 5}}]),            # inside the risk-manager sub-agent
        AIMessage(content="APPROVE: 5 INFY is well within limits."),        # sub-agent's answer
        AIMessage(content="", tool_calls=[{"id": "w1", "name": "write_file", "args": {
            "file_path": "/memories/journal/2026-11-19.md", "content": "INFY: risk approved 5 shares."}}]),
        AIMessage(content="Risk Manager approved; journal updated."),
    ])
    monkeypatch.setattr(graph, "make_model", lambda spec: model)
    r = HedgeFundRunner(EventBus())
    await r.start()
    res = await r.chat("erin", "erin:main", "Is 5 INFY ok risk-wise?")
    assert res["status"] == "done" and "approved" in res["reply"], res
    from app.db import AgentRun, SessionLocal
    async with SessionLocal() as s:
        run = await s.get(AgentRun, res["run_id"])
    agents = {st["agent"] for st in run.steps}
    assert "risk-manager" in agents, run.steps          # sub-agent activity was streamed
    assert any("check_trade_risk" in st["text"] for st in run.steps if st["kind"] == "tool_call")
    # long-term memory persisted in the store, in a fresh runner too
    r2 = HedgeFundRunner(EventBus())
    r2.checkpointer, r2.store = r.checkpointer, r.store
    paths = {m["path"]: m["content"] for m in await r2.list_memories("erin")}
    assert "INFY: risk approved" in paths.get("/memories/journal/2026-11-19.md", ""), paths
    await r.stop()


async def test_budget_guardrails(monkeypatch):
    """Spend is tracked per model/user; the daily cap and the per-request call cap both stop the agents."""
    from app.config import get_settings
    from app.llm_budget import guard

    st = get_settings()
    old = (st.llm_daily_budget_usd, st.llm_max_calls_per_run)
    await guard.load()
    usage = {"input_tokens": 200_000, "output_tokens": 50_000, "total_tokens": 250_000}
    model = ScriptedModel(callbacks=[guard], script=[AIMessage(content="Portfolio is flat.", usage_metadata=usage)])
    monkeypatch.setattr(graph, "make_model", lambda spec: model)
    r = HedgeFundRunner(EventBus())
    await r.start()
    try:
        before = guard.spent_today()
        res = await r.chat("frank", "frank:main", "how am I doing?")
        assert res["status"] == "done"
        assert guard.spent_today() > before and guard.usage["by_user"]["frank"] > 0

        st.llm_daily_budget_usd = guard.spent_today()           # cap reached -> next run refused up front
        res = await r.chat("frank", "frank:main", "again")
        assert res["status"] == "limited" and "budget" in res["reply"]

        st.llm_daily_budget_usd, st.llm_max_calls_per_run = 1000.0, 2  # runaway loop -> stopped mid-run
        loop = [AIMessage(content="", tool_calls=[{"id": f"q{i}", "name": "get_market_status", "args": {}}])
                for i in range(5)]
        model.script[:] = loop
        res = await r.chat("frank", "frank:loop", "loop forever")
        assert res["status"] == "limited" and "limit of 2 AI calls" in res["reply"], res
    finally:
        st.llm_daily_budget_usd, st.llm_max_calls_per_run = old


async def test_failing_tool_does_not_crash_run(monkeypatch):
    """A data source outage (e.g. Google News 503) is reported to the agent, and the run completes."""
    import httpx

    from app.market import data

    class Down(data.SyntheticProvider):
        async def quote(self, symbol):
            raise httpx.HTTPStatusError("503 Service Unavailable", request=httpx.Request("GET", "http://x"),
                                        response=httpx.Response(503))

    data.set_provider(Down())
    model = ScriptedModel(script=[
        AIMessage(content="", tool_calls=[{"id": "t1", "name": "task", "args": {
            "subagent_type": "research-analyst", "description": "Quote GOLD."}}]),
        AIMessage(content="", tool_calls=[{"id": "q1", "name": "get_quote", "args": {"symbol": "GOLD"}}]),
        AIMessage(content="Quote feed is down; no view on GOLD."),
        AIMessage(content="", tool_calls=[{"id": "q2", "name": "get_quote", "args": {"symbol": "GOLD"}}]),
        AIMessage(content="Data is temporarily unavailable; I'll retry later."),
    ])
    monkeypatch.setattr(graph, "make_model", lambda spec: model)
    r = HedgeFundRunner(EventBus())
    await r.start()
    try:
        res = await r.chat("hari", "hari:main", "Analyse GOLD")
    finally:
        data.set_provider(data.SyntheticProvider())
    assert res["status"] == "done", res
    assert "unavailable" in res["reply"]


async def test_news_falls_back_to_yahoo(monkeypatch):
    from app import finance_models as fm

    async def boom(*a, **k):
        raise RuntimeError("503")

    async def yahoo(symbol, limit):
        return [{"title": f"{symbol} rallies", "published": "", "source": "Yahoo Finance"}]

    monkeypatch.setattr(fm, "_google_news", boom)
    monkeypatch.setattr(fm, "_yahoo_news", yahoo)
    news = await fm.fetch_news("GOLD commodity price", symbol="GOLD")
    assert news and news[0]["title"] == "GOLD rallies"
