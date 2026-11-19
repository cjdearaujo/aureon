import pytest

from app.broker.paper import broker, estimate_charges
from app.db import init_db
from app import risk


@pytest.fixture(autouse=True, scope="module")
async def _db():
    await init_db()


async def test_buy_sell_roundtrip_with_charges():
    o = await broker.place_order("alice", "RELIANCE", "BUY", 10)
    assert o["status"] == "FILLED" and o["fees"] > 0
    pf = await broker.portfolio("alice")
    assert pf["positions"][0]["quantity"] == 10
    s = await broker.place_order("alice", "RELIANCE", "SELL", 10)
    assert s["status"] == "FILLED"
    pf = await broker.portfolio("alice")
    assert pf["positions"] == []
    assert pf["fees_paid"] == pytest.approx(o["fees"] + s["fees"], abs=0.02)
    assert pf["realized_pnl"] < 0  # slippage + charges on a flat round trip


async def test_risk_blocks_oversized_and_naked_short():
    big = await risk.check_trade("bob", "TCS", "BUY", 100000)
    assert not big["approved_by_risk_engine"]
    short = await risk.check_trade("bob", "TCS", "SELL", 1, product="CNC")
    assert any("cannot short" in v for v in short["violations"])
    ok = await risk.check_trade("bob", "TCS", "BUY", 1)
    assert ok["approved_by_risk_engine"], ok


def test_delivery_charges_include_stt():
    c = estimate_charges("INFY", "BUY", 100, 1500, "CNC")
    assert c["stt_ctt"] == pytest.approx(150.0)
    assert c["brokerage"] == 0


async def test_specialist_tools_and_team():
    import json

    from app.agents import specialist_tools as st
    from app.agents.tools import current_account
    current_account.set("alice")
    macro = json.loads(await st.macro_snapshot.ainvoke({}))
    assert macro["regime"] in ("risk-on", "neutral", "risk-off") and "NIFTY 50" in macro["markets"]
    f = json.loads(await st.company_fundamentals.ainvoke({"symbol": "INFY"}))
    assert f["pe"] and json.loads(await st.company_fundamentals.ainvoke({"symbol": "GOLD"}))["note"]
    c = json.loads(await st.compliance_check.ainvoke({"symbol": "TCS", "side": "BUY", "quantity": 5}))
    assert c["status"] in ("PASS", "PASS WITH FLAGS", "BLOCK") and c["audit_note"]
    p = json.loads(await st.performance_report.ainvoke({"days": 30}))
    assert p["filled"] >= 2 and p["closed_trades"] >= 1   # alice's round trip from the first test


def test_team_has_ten_agents():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore

    from app.agents import graph
    names = []
    real = graph.create_deep_agent
    graph.create_deep_agent = lambda **kw: names.extend(a["name"] for a in kw["subagents"]) or real(**kw)
    try:
        import os
        os.environ.setdefault("GOOGLE_API_KEY", "x")
        graph.build_hedge_fund(InMemorySaver(), InMemoryStore())
    finally:
        graph.create_deep_agent = real
    assert len(names) == 9 and {"macro-strategist", "bear-case-analyst", "compliance-officer"} <= set(names)


async def test_voice_decodes_browser_audio_with_ffmpeg(monkeypatch, tmp_path):
    """Browser-style WebM/Opus audio -> 16 kHz mono samples -> Whisper (model stubbed)."""
    import shutil
    import subprocess

    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    from app import voice

    webm = tmp_path / "v.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
                    "-c:a", "libopus", str(webm)], check=True)
    samples = voice.decode_audio(webm.read_bytes())
    assert 20000 < samples.size < 28000 and samples.dtype.name == "float32"

    class Seg:
        text = " buy five reliance "

    class FakeWhisper:
        def transcribe(self, audio, **kw):
            assert hasattr(audio, "shape")          # gets decoded samples, not a file path
            return [Seg()], None

    monkeypatch.setattr(voice, "_whisper", lambda: FakeWhisper())
    assert await voice.transcribe(webm.read_bytes()) == "buy five reliance"
    with pytest.raises(RuntimeError):
        voice.decode_audio(b"not audio at all")
