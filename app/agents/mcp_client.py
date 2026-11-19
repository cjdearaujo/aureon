"""MCP client side: loads tools from MCP servers (via langchain-mcp-adapters) for the agents.

By default it connects to Aureon's own MCP server (app/mcp_server.py) over Streamable HTTP on localhost.
Add more servers with MCP_SERVERS (JSON), e.g. a hosted news-search MCP:
  MCP_SERVERS='{"news": {"transport": "streamable_http", "url": "https://.../mcp", "headers": {"Authorization": "Bearer ..."}}}'
If a server can't be reached, agents fall back to their built-in tools, so MCP is never a single point of failure.
"""
from __future__ import annotations

import logging
import os

from app.config import get_settings

log = logging.getLogger(__name__)
_tools: list | None = None


def connections() -> dict:
    s = get_settings()
    port = os.environ.get("PORT", "8080")
    conns = {"aureon": {"transport": "streamable_http", "url": s.mcp_self_url or f"http://127.0.0.1:{port}/mcp/"}}
    token = s.app_passcode or s.admin_passcode
    if token:
        conns["aureon"]["headers"] = {"Authorization": f"Bearer {token}"}
    conns.update(s.mcp_servers)
    return conns


async def load_mcp_tools(force: bool = False) -> list:
    """Fetch tool definitions from every configured MCP server (cached after the first success)."""
    global _tools
    if _tools is not None and not force:
        return _tools
    if not get_settings().mcp_enabled:
        return []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    try:
        tools = await MultiServerMCPClient(connections(), tool_name_prefix=True).get_tools()
        _tools = tools
        log.info("MCP tools loaded: %s", [t.name for t in tools])
        return tools
    except Exception as exc:
        log.warning("MCP servers unreachable (%s); agents use built-in tools", exc)
        return []


def reset_cache() -> None:
    global _tools
    _tools = None
