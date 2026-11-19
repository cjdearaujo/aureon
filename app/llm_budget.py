"""LLM spending guardrails.

Every chat-model call made by any agent (Manager, sub-agents, voice-intent, Fin-R1 fallback) passes through
`BudgetGuard`, a LangChain callback attached to each model in `make_model`. It:
  1. blocks a call before it is sent if today's estimated spend has reached the daily cap,
  2. stops a single run that makes too many model calls (runaway agent loops),
  3. records tokens and estimated cost per model / per user after each call.
The runner also refuses to start a run when the cap is hit or a user has used up their daily runs.

Costs are ESTIMATES from list prices (USD per 1M tokens, overridable with LLM_PRICES). GCP billing is the
source of truth; the cloud budget alert is the second safety net.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from langchain_core.callbacks import AsyncCallbackHandler

from app.config import get_settings
from app.market.hours import now_ist

log = logging.getLogger(__name__)

# (input $/1M, output $/1M). Unknown models fall back to the most expensive entry (overestimate = safe).
DEFAULT_PRICES = {
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}

# Per-run call counter, set by the runner for each agent run.
run_calls: ContextVar[dict | None] = ContextVar("run_calls", default=None)
current_user: ContextVar[str] = ContextVar("llm_user", default="system")


class BudgetExceeded(RuntimeError):
    """Raised to stop a run when a spending guardrail trips."""


def price_for(model: str) -> tuple[float, float]:
    prices = {**DEFAULT_PRICES, **get_settings().llm_prices}
    for name, p in prices.items():
        if model and model.endswith(name):
            return tuple(p)
    return max(prices.values(), key=lambda p: p[1])


class BudgetGuard(AsyncCallbackHandler):
    raise_error = True   # make BudgetExceeded actually stop the agent
    run_inline = True

    def __init__(self) -> None:
        self.day = ""
        self.usage: dict = {}
        self._models: dict = {}

    # ---------------------------------------------------------------- state (persisted in the kv table)
    def _today(self) -> str:
        return now_ist().strftime("%Y-%m-%d")

    async def load(self) -> None:
        from app.db import KV, SessionLocal

        self.day = self._today()
        async with SessionLocal() as s:
            row = await s.get(KV, f"llm:{self.day}")
        self.usage = dict(row.value) if row else {"cost_usd": 0.0, "calls": 0, "input_tokens": 0,
                                                  "output_tokens": 0, "by_model": {}, "by_user": {}}

    async def _save(self) -> None:
        from app.db import KV, SessionLocal

        async with SessionLocal() as s:
            row = await s.get(KV, f"llm:{self.day}")
            if row:
                row.value = dict(self.usage)
            else:
                s.add(KV(key=f"llm:{self.day}", value=dict(self.usage)))
            await s.commit()

    async def ensure_today(self) -> None:
        if self.day != self._today():
            await self.load()

    def spent_today(self) -> float:
        return float(self.usage.get("cost_usd", 0.0)) if self.day == self._today() else 0.0

    def snapshot(self) -> dict:
        st = get_settings()
        return {"date_ist": self.day, "budget_usd": st.llm_daily_budget_usd, **self.usage,
                "remaining_usd": round(max(st.llm_daily_budget_usd - self.spent_today(), 0), 4)}

    # ---------------------------------------------------------------- callbacks
    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        await self.ensure_today()
        st = get_settings()
        if self.spent_today() >= st.llm_daily_budget_usd:
            raise BudgetExceeded(f"daily AI budget of ${st.llm_daily_budget_usd:.2f} reached")
        counter = run_calls.get()
        if counter is not None:
            counter["n"] += 1
            if counter["n"] > st.llm_max_calls_per_run:
                raise BudgetExceeded(f"this request hit the limit of {st.llm_max_calls_per_run} AI calls")
        params = kwargs.get("invocation_params") or {}
        self._models[run_id] = (params.get("model") or params.get("model_name") or (metadata or {}).get("ls_model_name")
                                or "unknown")

    async def on_llm_end(self, response, *, run_id, **kwargs):
        model = str(self._models.pop(run_id, "unknown")).removeprefix("models/")
        tin = tout = 0
        for gens in response.generations:
            for g in gens:
                um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                i, o = int(um.get("input_tokens", 0)), int(um.get("output_tokens", 0))
                # bill thinking tokens as output even if a provider reports them separately
                o = max(o, int(um.get("total_tokens", 0)) - i)
                tin, tout = tin + i, tout + o
        pin, pout = price_for(model)
        cost = tin / 1e6 * pin + tout / 1e6 * pout
        u = self.usage
        u["cost_usd"] = round(u.get("cost_usd", 0) + cost, 6)
        u["calls"] = u.get("calls", 0) + 1
        u["input_tokens"] = u.get("input_tokens", 0) + tin
        u["output_tokens"] = u.get("output_tokens", 0) + tout
        m = u.setdefault("by_model", {}).setdefault(model, {"calls": 0, "cost_usd": 0.0})
        m["calls"] += 1
        m["cost_usd"] = round(m["cost_usd"] + cost, 6)
        user = current_user.get()
        u.setdefault("by_user", {})[user] = round(u["by_user"].get(user, 0) + cost, 6)
        await self._save()

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self._models.pop(run_id, None)


guard = BudgetGuard()


async def runs_today(account: str) -> int:
    """User-initiated agent runs since midnight IST (scheduled scans/reviews don't count)."""
    from sqlalchemy import func, select

    from app.db import AgentRun, SessionLocal

    from datetime import timezone

    start = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    async with SessionLocal() as s:
        return await s.scalar(select(func.count()).select_from(AgentRun).where(
            AgentRun.account_id == account, AgentRun.started_at >= start,
            AgentRun.trigger.in_(["text", "voice"]))) or 0


async def preflight(account: str, trigger: str) -> str | None:
    """Return a user-facing reason if a new run must not start, else None."""
    st = get_settings()
    await guard.ensure_today()
    if guard.spent_today() >= st.llm_daily_budget_usd:
        return (f"Aureon's daily AI budget (${st.llm_daily_budget_usd:.2f}) has been used up. "
                "The agents will be back after midnight IST, or an admin can raise the limit in House rules.")
    if trigger in ("text", "voice") and account not in st.admin_users:
        n = await runs_today(account)
        if n >= st.llm_user_daily_runs:
            return f"You've used all {st.llm_user_daily_runs} AI requests for today. Your limit resets at midnight IST."
    return None
