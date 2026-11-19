"""Aureon Market Intelligence: an MCP (Model Context Protocol) server.

Read-only market tools published over MCP's Streamable HTTP transport at /mcp/ on the Aureon website:
  • Aureon's own Bear-Case Analyst consumes them through MCP (see app/agents/mcp_client.py).
  • Any external MCP client (MCP Inspector, Claude Desktop, other agents) can connect to the same endpoint
    with the header  Authorization: Bearer <client access code>.
No trading tools are exposed: orders stay inside Aureon, behind human approval and the risk engine.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import get_settings

mcp = FastMCP(
    "aureon-market-intelligence",
    instructions="Read-only Indian market intelligence (NSE equities, MCX commodities): quotes, technicals, "
                 "fundamentals, FinBERT news sentiment and the macro regime. Paper-trading research only.",
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    # Our own bearer-token check (below) protects the endpoint; Cloud Run hostnames vary, so no host allow-list.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def market_quote(symbol: str) -> str:
    """Latest price for an NSE stock (e.g. RELIANCE) or MCX commodity (GOLD, SILVER, CRUDEOIL, NATURALGAS, COPPER)."""
    from app.market.data import get_provider

    return json.dumps(await get_provider().quote(symbol), default=str)


@mcp.tool()
async def technical_snapshot(symbol: str, timeframe: str = "swing") -> str:
    """Indicators for a symbol: SMA20/50, RSI14, MACD, ATR, Bollinger bands, 52-week range and a rule-based score.
    timeframe: 'swing' (daily, 1y) or 'intraday' (15m, 5d)."""
    from app.agents.tools import technical_analysis

    tf = "intraday" if timeframe == "intraday" else "swing"
    return await technical_analysis.ainvoke({"symbol": symbol, "timeframe": tf})


@mcp.tool()
async def fundamentals(symbol: str) -> str:
    """Valuation and quality for an NSE company: P/E, P/B, ROE, debt/equity, margins, growth, next results date."""
    from app.agents.specialist_tools import company_fundamentals

    return await company_fundamentals.ainvoke({"symbol": symbol})


@mcp.tool()
async def news_sentiment_finbert(symbol: str) -> str:
    """Recent headlines for a symbol, each scored by FinBERT (a finance-tuned BERT): -1 bearish .. +1 bullish."""
    from app.agents.tools import news_sentiment

    return await news_sentiment.ainvoke({"symbol": symbol})


@mcp.tool()
async def market_regime() -> str:
    """Risk-on / neutral / risk-off read from NIFTY 50, Bank Nifty, India VIX, USD/INR, Brent crude and the S&P 500."""
    from app.agents.specialist_tools import macro_snapshot

    return await macro_snapshot.ainvoke({})


class BearerAuth:
    """ASGI wrapper: require 'Authorization: Bearer <APP_PASSCODE or ADMIN_PASSCODE>' when codes are configured."""

    def __init__(self):
        self.app = None
        self.rebuild()

    def rebuild(self):
        """Fresh Streamable HTTP app + session manager (the MCP SDK allows one run() per manager)."""
        mcp._session_manager = None
        self.app = mcp.streamable_http_app()

    async def __call__(self, scope, receive, send):
        st = get_settings()
        allowed = {c for c in (st.app_passcode, st.admin_passcode) if c}
        if scope["type"] == "http" and allowed:
            headers = dict(scope.get("headers") or [])
            token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            if token not in allowed:
                body = b'{"error":"unauthorized: send Authorization: Bearer <access code>"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


_asgi = BearerAuth()


def asgi_app():
    return _asgi


def running():
    """Async context that serves MCP sessions; enter it once per app start (FastAPI lifespan)."""
    _asgi.rebuild()
    return mcp.session_manager.run()
