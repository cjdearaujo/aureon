"""Runs the hedge fund graph: streams progress to the dashboard, records runs, and handles the
human-in-the-loop pause/resume around place_order."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from sqlalchemy import select

from app.agents import prompts
from app.agents.graph import build_hedge_fund
from app.agents.tools import current_account
from app.config import get_settings
from app.db import AgentRun, PendingApproval, SessionLocal
from app.events import EventBus, current_run_id

log = logging.getLogger(__name__)


def text_of(content) -> str:
    """Gemini/Anthropic may return a list of content parts; flatten to text."""
    if isinstance(content, str):
        return content
    parts = []
    for p in content or []:
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "".join(parts)


class HedgeFundRunner:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._stack = AsyncExitStack()
        self.checkpointer = None
        self.store = None
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        url = get_settings().database_url
        if url.startswith(("postgres://", "postgresql")):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from langgraph.store.postgres.aio import AsyncPostgresStore
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            pool = AsyncConnectionPool(url.replace("postgresql+psycopg://", "postgresql://"), max_size=5, open=False,
                                       kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row})
            await pool.open()
            self._stack.push_async_callback(pool.close)
            self.checkpointer, self.store = AsyncPostgresSaver(pool), AsyncPostgresStore(pool)
            await self.checkpointer.setup()
            await self.store.setup()
            log.info("Agent memory: Postgres store + checkpointer")
        else:
            from langgraph.checkpoint.memory import InMemorySaver
            from langgraph.store.memory import InMemoryStore

            self.checkpointer, self.store = InMemorySaver(), InMemoryStore()
            log.warning("Agent memory: in-process (set DATABASE_URL to Postgres to persist)")

    async def stop(self) -> None:
        await self._stack.aclose()

    # ------------------------------------------------------------ memory helpers
    async def ensure_playbook(self, account: str) -> None:
        if await self.store.aget(("memories", account), "/AGENTS.md") is None:
            from deepagents.backends import StoreBackend

            backend = StoreBackend(namespace=lambda _rt: ("memories", account), store=self.store)
            await backend.awrite("/AGENTS.md", prompts.DEFAULT_PLAYBOOK)

    async def list_memories(self, account: str) -> list[dict]:
        items = await self.store.asearch(("memories", account), limit=100)
        out = []
        for it in items:
            content = it.value.get("content", "")
            out.append({"path": "/memories" + it.key,
                        "content": "\n".join(content) if isinstance(content, list) else str(content),
                        "updated_at": it.updated_at.isoformat() if it.updated_at else None})
        return sorted(out, key=lambda m: m["path"])

    # ------------------------------------------------------------ running
    async def chat(self, account: str, thread_id: str, message: str, trigger: str = "text") -> dict:
        return await self._run(account, thread_id, {"messages": [{"role": "user", "content": message}]}, message, trigger)

    async def autonomous_tick(self, account: str) -> dict:
        from app.market.hours import now_ist

        msg = prompts.AUTONOMOUS_TICK.format(now=now_ist().strftime("%Y-%m-%d %H:%M"))
        thread = f"{account}:auto:{now_ist():%Y%m%d}"
        return await self._run(account, thread, {"messages": [{"role": "user", "content": msg}]}, msg, "autonomous")

    async def performance_review(self, account: str) -> dict:
        from app.market.hours import now_ist

        msg = prompts.PERFORMANCE_REVIEW.format(now=now_ist().strftime("%Y-%m-%d %H:%M"))
        thread = f"{account}:review:{now_ist():%Y%m%d}"
        return await self._run(account, thread, {"messages": [{"role": "user", "content": msg}]}, msg, "review")

    async def decide(self, account: str, approval_id: str, approve: bool, note: str = "", trigger: str = "click") -> dict:
        from app.llm_budget import preflight

        if reason := await preflight(account, "click"):   # keep the trade pending rather than half-resume it
            return {"status": "limited", "reply": reason + " Your pending trade is kept for later.", "pending_approval": None}
        async with SessionLocal() as s:
            pa = await s.get(PendingApproval, approval_id)
            if not pa or pa.account_id != account or pa.status != "pending":
                return {"error": "approval not found or already decided"}
            pa.status = "approved" if approve else "rejected"
            pa.decision_note, pa.decided_at = note, datetime.now(timezone.utc)
            await s.commit()
            thread_id, n = pa.thread_id, len(pa.actions)
        decision = {"type": "approve"} if approve else \
            {"type": "reject", "message": f"Human rejected this order. Reason: {note or 'not given'}"}
        await self.bus.publish("approval_decided", approval_id=approval_id, approved=approve, note=note, account=account)
        label = f"{'APPROVE' if approve else 'REJECT'} {approval_id}" + (f": {note}" if note else "")
        return await self._run(account, thread_id, Command(resume={"decisions": [decision] * n}), label, trigger)

    async def pending(self, account: str) -> list[dict]:
        async with SessionLocal() as s:
            rows = (await s.scalars(select(PendingApproval).where(
                PendingApproval.account_id == account, PendingApproval.status == "pending")
                .order_by(PendingApproval.created_at.desc()))).all()
        return [{"id": r.id, "thread_id": r.thread_id, "actions": r.actions, "created_at": r.created_at.isoformat()}
                for r in rows]

    async def _run(self, account: str, thread_id: str, payload, label: str, trigger: str) -> dict:
        """Safety net: whatever goes wrong inside a run, the user gets a readable reply, never a 500."""
        try:
            return await self._run_inner(account, thread_id, payload, label, trigger)
        except Exception as exc:
            log.exception("agent run crashed outside the graph")
            await self.bus.publish("run_finished", account=account, status="error", reply=str(exc)[:200])
            return {"status": "error", "pending_approval": None,
                    "reply": f"Sorry, something went wrong ({type(exc).__name__}: {str(exc)[:200]}). "
                             "Please try again; if it repeats, an admin can run Diagnostics in the Broker console."}

    async def _run_inner(self, account: str, thread_id: str, payload, label: str, trigger: str) -> dict:
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            return {"error": "The fund manager is still working on your previous request. Please wait."}
        from app.llm_budget import BudgetExceeded, current_user, preflight, run_calls

        if not isinstance(payload, Command) and (reason := await preflight(account, trigger)):
            return {"status": "limited", "reply": reason, "pending_approval": None}
        async with lock:
            current_account.set(account)
            current_user.set(account)
            run_calls.set({"n": 0})
            await self.ensure_playbook(account)
            async with SessionLocal() as s:
                run = AgentRun(account_id=account, trigger=trigger, thread_id=thread_id, input=label, steps=[])
                s.add(run)
                await s.commit()
            current_run_id.set(run.id)
            await self.bus.publish("run_started", account=account, trigger=trigger, input=label[:300])

            from app.settings_store import RISK_PROFILES, get_prefs

            p = await get_prefs(account)
            profile = (f"Name: {p['display_name'] or account}. Risk profile: {p['risk_profile']} "
                       f"({RISK_PROFILES[p['risk_profile']]:.0%} of house limits). Preferred style: {p['trading_style']}, "
                       f"default product {p['default_product']}. Client notes: {p['notes_for_manager'] or 'none'}.")
            from app.agents.mcp_client import load_mcp_tools

            agent = build_hedge_fund(self.checkpointer, self.store, profile, await load_mcp_tools())
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": get_settings().agent_recursion_limit,
                      "metadata": {"account": account, "trigger": trigger, "run_id": run.id},
                      "tags": ["hedge-fund", trigger]}
            steps, status, error = [], "done", None
            try:
                names: dict[str, str] = {}  # sub-graph namespace -> agent name (from message metadata)
                async for ns, mode, chunk in agent.astream(payload, config, stream_mode=["updates", "messages"],
                                                           subgraphs=True):
                    key = ns[0] if ns else ""
                    if mode == "messages":
                        names.setdefault(key, (chunk[1] or {}).get("lc_agent_name") or "hedge-fund-manager")
                        continue
                    who = names.get(key, "hedge-fund-manager" if not ns else "sub-agent")
                    for node, update in (chunk or {}).items():
                        if not isinstance(update, dict):
                            continue
                        for m in update.get("messages", []) or []:
                            step = self._describe(who, m)
                            if step:
                                steps.append(step)
                                await self.bus.publish("step", account=account, **step)
            except BudgetExceeded as exc:
                status, error = "limited", f"Stopped to protect the budget: {exc}."
            except Exception as exc:
                if isinstance(exc.__cause__, BudgetExceeded) or "BudgetExceeded" in repr(exc):
                    status, error = "limited", f"Stopped to protect the budget: {exc}."
                else:
                    log.exception("agent run failed")
                    status, error = "error", f"{type(exc).__name__}: {exc}"

            state = await agent.aget_state(config)
            reply = ""
            for m in reversed(state.values.get("messages", [])):
                if isinstance(m, AIMessage) and text_of(m.content).strip():
                    reply = text_of(m.content).strip()
                    break
            approval = None
            if state.interrupts:
                actions = []
                for intr in state.interrupts:
                    for a in (intr.value or {}).get("action_requests", []):
                        actions.append({"name": a["name"], "args": a["args"]})
                async with SessionLocal() as s:
                    pa = PendingApproval(account_id=account, thread_id=thread_id, actions=actions)
                    s.add(pa)
                    await s.commit()
                approval = {"id": pa.id, "actions": actions}
                status = "awaiting_approval"
                await self.bus.publish("approval_required", account=account, approval=approval)
            if status == "limited":
                reply = error + " Ask an admin to raise the AI limits in House rules if needed."
            elif error:
                reply = (f"Sorry, the run failed: {error[:300]}. If this is a rate limit (429), "
                         "wait a minute and try again.")

            async with SessionLocal() as s:
                r = await s.get(AgentRun, run.id)
                r.output, r.status, r.steps, r.finished_at = reply, status, steps[-200:], datetime.now(timezone.utc)
                await s.commit()
            await self.bus.publish("run_finished", account=account, status=status, reply=reply)
            return {"run_id": run.id, "thread_id": thread_id, "status": status, "reply": reply,
                    "pending_approval": approval, "steps": len(steps)}

    @staticmethod
    def _describe(who: str, m) -> dict | None:
        if isinstance(m, AIMessage):
            if m.tool_calls:
                calls = []
                for tc in m.tool_calls:
                    args = tc.get("args", {})
                    if tc["name"] == "task":
                        calls.append(f"→ {args.get('subagent_type', '?')}: {str(args.get('description', ''))[:160]}")
                    else:
                        calls.append(f"{tc['name']}({json.dumps(args, default=str)[:120]})")
                return {"agent": who, "kind": "tool_call", "text": " | ".join(calls)}
            txt = text_of(m.content).strip()
            return {"agent": who, "kind": "thought", "text": txt[:600]} if txt else None
        if isinstance(m, ToolMessage):
            return {"agent": who, "kind": "tool_result", "tool": m.name, "text": text_of(m.content)[:400]}
        return None
