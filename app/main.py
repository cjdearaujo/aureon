"""FastAPI app: REST + WebSocket API for the dashboard, voice endpoint, and the scheduler tick."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import distinct, select

from app.agents.runner import HedgeFundRunner
from app.broker.paper import broker
from app.config import get_settings
from app.db import Account, AgentRun, EquitySnapshot, Order, SessionLocal, init_db
from app.events import EventBus
from app.settings_store import RISK_PROFILES, get_prefs, global_settings, load_global, save_global, save_prefs
from app.market.data import COMMODITIES, get_provider, normalize
from app.market.hours import is_market_open, market_status
from app.market.indicators import technical_summary

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await load_global()
    from app.llm_budget import guard

    await guard.load()
    from contextlib import AsyncExitStack

    from app.mcp_server import running

    stack = AsyncExitStack()
    await stack.enter_async_context(running())   # MCP Streamable HTTP session manager (serves /mcp/)
    app.state.bus = EventBus(settings.redis_url)
    await app.state.bus.start()
    app.state.runner = HedgeFundRunner(app.state.bus)
    await app.state.runner.start()
    async def _warmup():  # load FinBERT + Whisper in the background so the first user doesn't wait ~45 s
        import asyncio

        from app.finance_models import _finbert
        from app.voice import _whisper

        for name, loader in (("FinBERT", _finbert), ("Whisper", _whisper)):
            try:
                if name == "FinBERT" and not settings.finbert_enabled:
                    continue
                await asyncio.to_thread(loader)
                log.info("%s preloaded", name)
            except Exception as exc:
                log.warning("%s preload failed: %s", name, exc)

    if settings.app_env == "prod":
        import asyncio

        app.state.warmup = asyncio.create_task(_warmup())
    log.info("Agentic Trader ready (models: manager=%s, market data=%s)", settings.manager_model,
             settings.market_data_provider)
    yield
    await app.state.runner.stop()
    await app.state.bus.stop()
    await stack.aclose()


app = FastAPI(title="Aureon · Agentic Hedge Fund", version="1.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception):
    """Never a bare 'Internal Server Error': log the traceback, return a readable reason."""
    from fastapi.responses import JSONResponse

    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={
        "detail": f"{type(exc).__name__}: {str(exc)[:240]} (full traceback in Cloud Run logs)"})


# ------------------------------------------------------------------ auth: one login, role by account
# Master accounts (ADMIN_USERS) signing in with ADMIN_PASSCODE get the admin/broker console.
# Everyone else signs in with APP_PASSCODE and gets the client view. Tokens are HMAC-signed.
TOKEN_TTL = 7 * 24 * 3600


def _sign(payload: str) -> str:
    return hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(name: str, role: str) -> str:
    payload = f"{name}|{role}|{int(time.time()) + TOKEN_TTL}"
    return base64.urlsafe_b64encode(f"{payload}|{_sign(payload)}".encode()).decode()


def read_token(token: str | None) -> tuple[str, str]:
    try:
        name, role, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
        payload = f"{name}|{role}|{exp}"
        if hmac.compare_digest(sig, _sign(payload)) and int(exp) > time.time():
            return name, role
    except Exception:
        pass
    raise HTTPException(401, "please sign in again")


class Session(BaseModel):
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def session(authorization: str | None = Header(default=None)) -> Session:
    token = (authorization or "").removeprefix("Bearer ").strip()
    name, role = read_token(token)
    return Session(name=name, role=role)


def user(sess: Session = Depends(session)) -> str:
    return sess.name


def admin(sess: Session = Depends(session)) -> Session:
    if not sess.is_admin:
        raise HTTPException(403, "admin only")
    return sess


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    code: str = ""


@app.post("/api/login")
async def login(body: LoginIn):
    name = re.sub(r"[^a-z0-9_-]", "", body.username.lower())[:32]
    if not name:
        raise HTTPException(400, "use letters, numbers, - or _")
    if name in settings.admin_users:
        if not settings.admin_passcode or not hmac.compare_digest(body.code, settings.admin_passcode):
            raise HTTPException(401, "invalid admin access code")
        role = "admin"
    else:
        if settings.app_passcode and not hmac.compare_digest(body.code, settings.app_passcode):
            raise HTTPException(401, "invalid access code")
        role = "user"
    return {"token": make_token(name, role), "name": name, "role": role}


def runner() -> HedgeFundRunner:
    return app.state.runner


# ------------------------------------------------------------------ pages / health
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _mount_mcp():
    from app.mcp_server import asgi_app

    app.mount("/mcp", asgi_app(), name="mcp")   # MCP Streamable HTTP endpoint: POST /mcp/


_mount_mcp()


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def config(sess: Session = Depends(session)):
    s, u = settings, sess.name
    return {"user": u, "role": sess.role, "prefs": await get_prefs(u), "risk_profiles": RISK_PROFILES,
            "trading_halted": s.trading_halted, "starting_capital": s.starting_capital, "watchlist": s.watchlist, "commodities": list(COMMODITIES), "market": market_status(),
            "data_provider": s.market_data_provider,
            "models": {"Hedge Fund Manager": s.manager_model, "Macro Strategist": s.macro_model,
                       "Research Analyst": s.research_model, "Fundamental Analyst": s.fundamental_model,
                       "Quant Strategist": s.quant_model, "Bear-Case Analyst": s.bear_model,
                       "Risk Manager": s.risk_model, "Compliance Officer": s.compliance_model,
                       "Execution Trader": s.execution_model, "Performance Analyst": s.performance_model, "Fin-R1 (finance LLM)": s.finr1_model,
                       "FinBERT (sentiment)": s.finbert_model, "Whisper (voice)": f"faster-whisper {s.whisper_model}"},
            "mcp": {"endpoint": "/mcp/", "server": "aureon-market-intelligence", "enabled": s.mcp_enabled,
                    "tools": ["market_quote", "technical_snapshot", "fundamentals", "news_sentiment_finbert", "market_regime"]},
            "limits": {"max_position_pct": s.max_position_pct, "max_order_value": s.max_order_value,
                       "max_daily_loss_pct": s.max_daily_loss_pct, "max_open_positions": s.max_open_positions}}


# ------------------------------------------------------------------ market + portfolio
@app.get("/api/quote/{symbol}")
async def quote(symbol: str, u: str = Depends(user)):
    try:
        return await get_provider().quote(symbol)
    except Exception as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/candles/{symbol}")
async def candles(symbol: str, period: str = "6mo", interval: str = "1d", u: str = Depends(user)):
    try:
        df = await get_provider().history(symbol, period, interval)
    except Exception as exc:
        raise HTTPException(404, str(exc))
    intraday = interval.endswith(("m", "h"))
    bars = [{"time": int(ts.timestamp()) if intraday else ts.strftime("%Y-%m-%d"),
             "open": round(r.open, 2), "high": round(r.high, 2), "low": round(r.low, 2), "close": round(r.close, 2),
             "volume": float(r.volume)} for ts, r in df.iterrows()]
    async with SessionLocal() as s:
        fills = (await s.scalars(select(Order).where(Order.account_id == u, Order.symbol == normalize(symbol),
                                                     Order.status == "FILLED"))).all()
    markers = [{"time": int(o.filled_at.timestamp()) if intraday else o.filled_at.strftime("%Y-%m-%d"),
                "side": o.side, "qty": o.quantity, "price": o.fill_price} for o in fills if o.filled_at]
    tech = technical_summary(df) if len(df) >= 30 else {}
    return {"symbol": normalize(symbol), "bars": bars, "markers": markers, "technicals": tech}


@app.get("/api/portfolio")
async def portfolio(u: str = Depends(user)):
    return await broker.portfolio(u)


@app.get("/api/orders")
async def orders(u: str = Depends(user)):
    return await broker.orders(u, 100)


@app.get("/api/equity")
async def equity(u: str = Depends(user)):
    async with SessionLocal() as s:
        rows = (await s.scalars(select(EquitySnapshot).where(EquitySnapshot.account_id == u)
                                .order_by(EquitySnapshot.taken_at))).all()
    return [{"time": int(r.taken_at.timestamp()), "value": round(r.equity, 2)} for r in rows]


@app.get("/api/memory")
async def memory(u: str = Depends(user), r: HedgeFundRunner = Depends(runner)):
    await r.ensure_playbook(u)
    return await r.list_memories(u)


# ------------------------------------------------------------------ agents
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None


@app.post("/api/chat")
async def chat(body: ChatIn, u: str = Depends(user), r: HedgeFundRunner = Depends(runner)):
    return await r.chat(u, body.thread_id or f"{u}:main", body.message, "text")


@app.get("/api/approvals")
async def approvals(u: str = Depends(user), r: HedgeFundRunner = Depends(runner)):
    return await r.pending(u)


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionIn, u: str = Depends(user), r: HedgeFundRunner = Depends(runner)):
    return await r.decide(u, approval_id, body.approve, body.note)


@app.post("/api/voice")
async def voice(audio: UploadFile = File(...), thread_id: str | None = Form(None), u: str = Depends(user),
                r: HedgeFundRunner = Depends(runner)):
    from app.voice import parse_intent, transcribe

    data = await audio.read()
    if len(data) > 10_000_000:
        raise HTTPException(413, "audio too long")
    suffix = Path(audio.filename or "a.webm").suffix or ".webm"
    try:
        text = await transcribe(data, suffix)
    except Exception as exc:
        log.exception("transcription failed")
        return {"transcript": "", "reply": f"Voice transcription failed ({type(exc).__name__}). Please type your request."}
    if not text:
        return {"transcript": "", "reply": "I didn't catch that. Please try again."}
    await app.state.bus.publish("transcript", account=u, text=text)
    pending = await r.pending(u)
    intent = await parse_intent(text, bool(pending))
    if intent.intent in ("approve", "reject") and pending:
        result = await r.decide(u, pending[0]["id"], intent.intent == "approve", intent.reason, "voice")
    else:
        result = await r.chat(u, thread_id or f"{u}:main", text, "voice")
    return {"transcript": text, "intent": intent.intent, **result}


# ------------------------------------------------------------------ client settings
@app.get("/api/me/settings")
async def my_settings(u: str = Depends(user)):
    return await get_prefs(u)


@app.put("/api/me/settings")
async def update_my_settings(body: dict, u: str = Depends(user)):
    return await save_prefs(u, body)


@app.post("/api/me/reset")
async def reset_me(u: str = Depends(user)):
    await broker.reset(u)
    return {"ok": True}


# ------------------------------------------------------------------ admin (broker) console
@app.get("/api/admin/overview")
async def admin_overview(_: Session = Depends(admin), r: HedgeFundRunner = Depends(runner)):
    async with SessionLocal() as s:
        accounts = (await s.scalars(select(Account))).all()
        runs = (await s.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(500))).all()
        n_orders = len((await s.scalars(select(Order.id))).all())
    last_run = {}
    for run in runs:
        last_run.setdefault(run.account_id, run.started_at.isoformat())
    clients = []
    for a in accounts:
        pf = await broker.portfolio(a.id)
        prefs = await get_prefs(a.id)
        clients.append({"user": a.id, "name": prefs["display_name"] or a.id, "risk_profile": prefs["risk_profile"],
                        "equity": pf["equity"], "pnl": pf["total_pnl"], "return_pct": pf["total_return_pct"],
                        "positions": len(pf["positions"]), "fees": pf["fees_paid"],
                        "pending": len(await r.pending(a.id)), "last_active": last_run.get(a.id),
                        "is_admin": a.id in settings.admin_users})
    aum = sum(c["equity"] for c in clients)
    return {"clients": sorted(clients, key=lambda c: -c["equity"]), "aum": round(aum, 2),
            "total_pnl": round(sum(c["pnl"] for c in clients), 2), "orders": n_orders,
            "runs": len(runs), "errors": sum(1 for x in runs if x.status == "error"),
            "pending": sum(c["pending"] for c in clients), "settings": global_settings(),
            "llm": __import__("app.llm_budget", fromlist=["guard"]).guard.snapshot(),
            "market": market_status(), "data_provider": settings.market_data_provider}


@app.get("/api/admin/orders")
async def admin_orders(_: Session = Depends(admin)):
    return await broker.orders(None, 300)


@app.get("/api/admin/runs")
async def admin_runs(_: Session = Depends(admin)):
    async with SessionLocal() as s:
        runs = (await s.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(100))).all()
    return [{"id": x.id, "user": x.account_id, "trigger": x.trigger, "input": x.input[:200], "status": x.status,
             "output": (x.output or "")[:400], "steps": len(x.steps or []), "started_at": x.started_at.isoformat(),
             "seconds": round((x.finished_at - x.started_at).total_seconds(), 1) if x.finished_at else None}
            for x in runs]


@app.get("/api/admin/runs/{run_id}")
async def admin_run(run_id: str, _: Session = Depends(admin)):
    async with SessionLocal() as s:
        x = await s.get(AgentRun, run_id)
    if not x:
        raise HTTPException(404, "not found")
    return {"id": x.id, "user": x.account_id, "input": x.input, "output": x.output, "steps": x.steps}


@app.get("/api/admin/settings")
async def admin_get_settings(_: Session = Depends(admin)):
    return global_settings()


@app.put("/api/admin/settings")
async def admin_put_settings(body: dict, sess: Session = Depends(admin)):
    out = await save_global(body)
    await app.state.bus.publish("settings_changed", by=sess.name, trading_halted=out["trading_halted"])
    return out


@app.get("/api/admin/llm-check")
async def admin_llm_check(_: Session = Depends(admin)):
    return await llm_check()


async def llm_check() -> dict:
    """Send a 1-word prompt to every configured model: proves IDs, credentials and quota (costs ~nothing)."""
    from app.agents.models import make_model

    specs = sorted({getattr(settings, f) for f in type(settings).model_fields if f.endswith("_model") and f not in
                    ("finr1_model", "finbert_model", "whisper_model")})
    out = {}
    for spec in specs:
        try:
            msg = await make_model(spec).ainvoke("Reply with the single word OK.")
            out[spec] = "ok: " + str(msg.content)[:20]
        except Exception as exc:
            out[spec] = f"FAILED: {type(exc).__name__}: {str(exc)[:160]}"
    return {"all_ok": all(v.startswith("ok") for v in out.values()), "models": out}


@app.get("/api/admin/diagnostics")
async def admin_diagnostics(_: Session = Depends(admin)):
    from app.diagnostics import run_diagnostics

    return await run_diagnostics()


@app.post("/api/admin/users/{name}/reset")
async def admin_reset_user(name: str, _: Session = Depends(admin)):
    await broker.reset(name)
    return {"ok": True}


# ------------------------------------------------------------------ autonomous scan (Cloud Scheduler)
@app.post("/api/autonomous/tick")
async def tick(mode: str = "scan", x_tick_token: str | None = Header(default=None), r: HedgeFundRunner = Depends(runner)):
    if not settings.tick_token or x_tick_token != settings.tick_token:
        raise HTTPException(401, "bad tick token")
    if mode == "llmcheck":
        return await llm_check()
    if mode == "diag":  # used by deploy.sh right after deployment: every live dependency, one report
        from app.diagnostics import run_diagnostics

        return await run_diagnostics()
    if mode == "review":  # end-of-day Performance Analyst review for every client
        async with SessionLocal() as s:
            accounts = (await s.scalars(select(distinct(Account.id)))).all()
        return {"reviewed": {a: (await r.performance_review(a)).get("status") for a in accounts}}
    filled = await broker.check_open_orders()
    if settings.enforce_market_hours and not is_market_open():
        return {"skipped": "market closed", "limit_orders_filled": filled}
    async with SessionLocal() as s:
        accounts = (await s.scalars(select(distinct(Account.id)))).all()
    # Awaited inside the request: Cloud Run only guarantees CPU while a request is in flight.
    # Users are scanned one at a time to stay within free-tier LLM rate limits.
    results = {}
    for acct in accounts:
        if await r.pending(acct):  # don't stack proposals while one awaits a human
            results[acct] = "skipped: approval pending"
            continue
        res = await r.autonomous_tick(acct)
        results[acct] = res.get("status")
    return {"results": results, "limit_orders_filled": filled}


# ------------------------------------------------------------------ live events
@app.websocket("/ws")
async def ws(websocket: WebSocket, token: str = Query("")):
    try:
        user_name, role = read_token(token)
    except HTTPException:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    q = app.state.bus.subscribe()
    try:
        while True:
            event = await q.get()
            if role == "admin" or event.get("account") in (None, user_name):
                await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        app.state.bus.unsubscribe(q)
