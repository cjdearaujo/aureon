"""Builds the multi-agent hedge fund with LangChain Deep Agents (create_deep_agent on LangGraph).

Hedge Fund Manager (main deep agent)
 ├── macro-strategist     market regime
 ├── research-analyst     technicals + news/FinBERT
 ├── fundamental-analyst  valuation + quality
 ├── quant-strategist     backtests, sizing, levels
 ├── bear-case-analyst    devil's advocate
 ├── risk-manager         exposure, veto
 ├── compliance-officer   pre-trade rules, can BLOCK
 ├── execution-trader     order ticket
 └── performance-analyst  post-trade review -> lessons

Memory:   /memories/* is routed to a LangGraph Store (Postgres on Neon) -> persists across sessions.
State:    LangGraph checkpointer (Postgres) -> conversations survive restarts; required for HITL.
MCP:      bear-case-analyst uses tools from MCP servers (Aureon's own at /mcp/ + any in MCP_SERVERS).
HITL:     interrupt_on={"place_order"} -> every order pauses until a human approves or rejects.
"""
from __future__ import annotations

import logging

from deepagents import create_deep_agent
from langchain.agents.middleware import ToolErrorMiddleware
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend

from app.agents import prompts
from app.agents import tools as T
from app.agents.models import make_model
from app.config import get_settings
from app.market.hours import is_market_open, now_ist

log = logging.getLogger(__name__)


def _memory_namespace(_runtime) -> tuple[str, ...]:
    return ("memories", T.current_account.get())   # each user has their own long-term memory


def _tool_error(exc: Exception, request) -> str | None:
    """A failing data source (news feed down, symbol not found, timeout) becomes a message the agent can
    reason about instead of crashing the whole run. Budget stops still propagate."""
    from app.llm_budget import BudgetExceeded

    if isinstance(exc, BudgetExceeded):
        return None
    name = request.tool_call.get("name", "tool") if hasattr(request, "tool_call") else "tool"
    log.warning("tool %s failed: %s: %s", name, type(exc).__name__, exc)
    return (f"Tool '{name}' failed ({type(exc).__name__}: {str(exc)[:160]}). This data source is unavailable right now; "
            "continue with the other evidence you have and mention the gap.")


def _guarded() -> list:
    return [ToolErrorMiddleware(on_error=_tool_error, aon_error=_atool_error)]


async def _atool_error(exc: Exception, request) -> str | None:
    return _tool_error(exc, request)


def build_backend(store):
    return CompositeBackend(
        default=StateBackend(),                     # scratch files live in the thread's state
        routes={"/memories/": StoreBackend(namespace=_memory_namespace, store=store)},
    )


def build_hedge_fund(checkpointer, store, profile: str = "No profile set.", mcp_tools: list | None = None):
    s = get_settings()
    # Bear-Case Analyst gathers its evidence over MCP when the MCP server is reachable (built-in tools otherwise).
    bear_tools = [*mcp_tools, T.backtest_strategy, T.get_portfolio] if mcp_tools else T.BEAR_TOOLS
    subagents = [
        {"name": "research-analyst", "description": "Technical + news/FinBERT research on NSE stocks or MCX commodities.",
         "system_prompt": prompts.RESEARCH, "tools": T.RESEARCH_TOOLS, "model": make_model(s.research_model), "middleware": _guarded()},
        {"name": "quant-strategist", "description": "Backtests, position sizing (ATR), entry/stop/target levels, charges.",
         "system_prompt": prompts.QUANT, "tools": T.QUANT_TOOLS, "model": make_model(s.quant_model), "middleware": _guarded()},
        {"name": "risk-manager", "description": "Portfolio risk review and hard-limit check. Can veto any trade.",
         "system_prompt": prompts.RISK, "tools": T.RISK_TOOLS, "model": make_model(s.risk_model), "middleware": _guarded()},
        {"name": "execution-trader", "description": "Prepares the exact order ticket (product, type, price, charges).",
         "system_prompt": prompts.EXECUTION, "tools": T.EXECUTION_TOOLS, "model": make_model(s.execution_model), "middleware": _guarded()},
        {"name": "macro-strategist", "description": "Market regime: NIFTY, Bank Nifty, India VIX, USD/INR, crude, S&P 500.",
         "system_prompt": prompts.MACRO, "tools": T.MACRO_TOOLS, "model": make_model(s.macro_model), "middleware": _guarded()},
        {"name": "fundamental-analyst", "description": "Valuation and quality: P/E, P/B, ROE, debt, growth, results dates.",
         "system_prompt": prompts.FUNDAMENTAL, "tools": T.FUNDAMENTAL_TOOLS, "model": make_model(s.fundamental_model), "middleware": _guarded()},
        {"name": "bear-case-analyst", "description": "Devil's advocate: the strongest evidence-based case against a trade.",
         "system_prompt": prompts.BEAR, "tools": bear_tools, "model": make_model(s.bear_model), "middleware": _guarded()},
        {"name": "compliance-officer", "description": "Exchange/SEBI-style pre-trade checks and audit note. Can BLOCK.",
         "system_prompt": prompts.COMPLIANCE, "tools": T.COMPLIANCE_TOOLS, "model": make_model(s.compliance_model), "middleware": _guarded()},
        {"name": "performance-analyst", "description": "Reviews past trades and results; proposes lessons for the playbook.",
         "system_prompt": prompts.PERFORMANCE, "tools": T.PERFORMANCE_TOOLS, "model": make_model(s.performance_model), "middleware": _guarded()},
    ]
    system_prompt = prompts.MANAGER.format(now=now_ist().strftime("%Y-%m-%d %H:%M"), market_open=is_market_open(),
                                           profile=profile)
    return create_deep_agent(
        name="hedge-fund-manager",
        model=make_model(s.manager_model),
        tools=T.MANAGER_TOOLS,
        middleware=_guarded(),
        system_prompt=system_prompt,
        subagents=subagents,
        backend=build_backend(store),
        memory=["/memories/AGENTS.md"],
        interrupt_on={"place_order": {"allowed_decisions": ["approve", "reject"]}},
        checkpointer=checkpointer,
        store=store,
    )
