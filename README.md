# ◆ AUREON: an agentic AI hedge fund desk

An autonomous, **human-in-the-loop** AI trading desk for Indian markets (NSE intraday + swing equities, MCX commodities).
A **Hedge Fund Manager** agent runs a team of specialist agents built with **LangChain Deep Agents** (LangGraph).
It consults **finance-tuned models from Hugging Face**, takes **voice commands** (Whisper), remembers past trades and
lessons, and **never places an order without your approval**. Paper trading only.

![architecture](https://img.shields.io/badge/LangGraph-Deep%20Agents-blue) ![LangSmith](https://img.shields.io/badge/monitoring-LangSmith-green) ![GCP](https://img.shields.io/badge/deploy-Cloud%20Run-orange)

## Architecture

```
 Browser (dashboard, charts, mic, TTS) ──REST/WebSocket──► Cloud Run: hedge-fund (FastAPI)
                                                              │
        ┌─────────────────────── Deep Agents graph (LangGraph) ┴──────────────────────┐
        │  🎩 Hedge Fund Manager  (Gemini 3.1 Pro: planning, delegation, final call)   │
        │     ├─ 🌐 macro-strategist    (Flash)      ─ regime: NIFTY, VIX, INR, crude   │
        │     ├─ 🔎 research-analyst    (Flash)      ─ technicals, news + FinBERT       │
        │     ├─ 📊 fundamental-analyst (Flash)      ─ P/E, ROE, debt, results dates    │
        │     ├─ 📐 quant-strategist    (Flash)      ─ backtests, ATR sizing, levels    │
        │     ├─ 🐻 bear-case-analyst   (Flash-Lite) ─ devil's advocate                 │
        │     ├─ 🛡️ risk-manager        (Flash)      ─ exposure review, VETO power      │
        │     ├─ ⚖️ compliance-officer  (Flash-Lite) ─ pre-trade rules, can BLOCK       │
        │     ├─ ⚡ execution-trader    (Flash-Lite) ─ order ticket                     │
        │     └─ 📈 performance-analyst (Flash-Lite) ─ post-trade review → lessons      │
        │  place_order ──► ⏸ interrupt_on (HITL) ──► you approve/reject (click or voice)│
        │  ──► hard risk engine (code) ──► paper broker (Indian charges, slippage)     │
        └──────────────────────────────────────────────────────────────────────────────┘
   Finance specialists:  FinBERT (in-image, CPU) · Fin-R1 7B (Cloud Run L4 GPU, scales to zero)
   Memory:   /memories/AGENTS.md playbook + /memories/journal/*  → LangGraph Store (Neon Postgres)
   State:    LangGraph checkpointer (Neon Postgres): conversations + paused approvals survive restarts
   Ops:      LangSmith traces · Cloud Scheduler market scans + end-of-day performance review · uptime check · budget alerts
```

| Agent / model | Model | Why |
|---|---|---|
| Hedge Fund Manager | `gemini-3.1-pro-preview` | Strongest reasoning and tool calling in the free tier |
| Macro / Research / Fundamental / Quant / Risk | `gemini-3.5-flash` | Fast, cheap, good tool calling |
| Bear-Case / Compliance / Execution / Performance, voice intent | `gemini-3.1-flash-lite` | Focused, structured tasks |
| **Fin-R1** (SUFE-AIFLM-Lab) | Qwen2.5-7B fine-tuned on 60k financial reasoning chains | Finance-specialist second opinion (`ask_fin_r1` tool) |
| **FinBERT** (ProsusAI) | BERT fine-tuned on financial news | Headline sentiment (`news_sentiment` tool) |
| **Whisper** small | faster-whisper, int8 on CPU | Voice commands |

Finance-tuned LLMs aren't trained for tool calling, so the agents **consult** them as expert tools while Gemini
runs the agent loops. Every model is one line in `.env`; for example, `MANAGER_MODEL=ollama:qwen3:8b` runs fully local.

## Website: one login, two views

| Who | How they sign in | What they see |
|---|---|---|
| **Client** (retail trader) | any username + client code (`APP_PASSCODE`) | Dashboard · Markets · Portfolio · Orders · AI Desk · Memory · Settings |
| **Master / broker (admin)** | a username in `ADMIN_USERS` + admin code (`ADMIN_PASSCODE`) | Everything above **plus** the Broker console: all clients and AUM, every order, agent-run traces, house risk rules and a **kill switch** |

Each client has their own paper account, memory and preferences. Their risk profile (conservative, balanced or aggressive)
scales the house limits, and their notes are given to the Fund Manager before every conversation.
Logins issue HMAC-signed tokens, and admin APIs reject non-admin tokens.

## Code map (start reading here)

| File | What it does |
|---|---|
| `app/agents/graph.py` | **The core**: `create_deep_agent` with sub-agents, memory backend and HITL |
| `app/agents/prompts.py` | Each agent's role and the hedge-fund workflow |
| `app/agents/tools.py` | Core tools (market data, backtests, sizing, risk, orders) and each agent's tool list |
| `app/agents/specialist_tools.py` | Macro regime, fundamentals, compliance checks, performance analytics |
| `app/agents/runner.py` | Runs the graph, streams steps to the UI, pause/resume for approvals |
| `app/risk.py` | Hard risk limits enforced in code, even if a model errs |
| `app/broker/paper.py` | Paper broker: fills, slippage, STT/GST/stamp duty, P&L |
| `app/market/` | Market data (yfinance / Angel One SmartAPI / synthetic), indicators, NSE hours |
| `app/finance_models.py` | FinBERT + Fin-R1 + free Google News feed |
| `app/voice.py` | Whisper speech-to-text + approve/reject intent parsing |
| `app/main.py` | FastAPI routes: login/roles, client APIs, admin APIs, voice, WebSocket |
| `app/mcp_server.py` / `app/agents/mcp_client.py` | MCP server at `/mcp/` + MCP client for the agents |
| `app/settings_store.py` | Admin house rules (kill switch, limits) + per-client preferences |
| `app/static/` | Aureon web app: hash-routed pages in vanilla JS + TradingView Lightweight Charts |

## Run locally (Windows PowerShell)

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-dev.txt
copy .env.example .env          # add your GOOGLE_API_KEY and LANGSMITH_API_KEY
ollama pull hf.co/mradermacher/Fin-R1-GGUF:Q4_K_M   # optional: Fin-R1 locally
uvicorn app.main:app --reload --port 8080
pytest -q                       # 13 tests, no API keys needed
```
Open http://localhost:8080 and sign in as `admin` / `admin123` (broker console) or any name / `client123`. Outside market hours, set `ENFORCE_MARKET_HOURS=false` to let approved paper orders fill.

## Deploy to GCP (Cloud Shell, about 25 min)

1. Create free keys: **Gemini** (aistudio.google.com), **LangSmith** (smith.langchain.com), **Neon** Postgres (neon.tech).
2. Open Cloud Shell, upload this folder's zip, `unzip` it, `cd agentic-trader`.
3. `PROJECT_ID=<your-project> ADMIN_USERS=admin,<you> BILLING_ACCOUNT=<optional-id> ./deploy/deploy.sh`

**Gemini inside GCP:** add `USE_VERTEX=true` to call Gemini through the Gemini Enterprise Agent Platform (formerly Vertex AI). It's billed to your GCP credits, uses the service account (no Gemini key needed) and has higher rate limits than the free AI Studio key.

The script enables APIs; creates secrets, a service account, both Cloud Run services, the scheduler, an uptime check and
budget alerts; and prints your URL. Set `DEPLOY_FINR1=false` to skip the GPU (the `ask_fin_r1` tool then falls back to Gemini).

**Estimated cost for a week:** main service with one warm instance ≈ $8–12 · Fin-R1 GPU only while in use ≈ $3–10 ·
Gemini free tier ≈ $0 · Neon / LangSmith free tiers = $0.

## MCP (Model Context Protocol)
Aureon is both an **MCP server** and an **MCP client**:
- **Server:** `app/mcp_server.py` publishes 5 read-only tools at **`/mcp/`** (Streamable HTTP): `market_quote`, `technical_snapshot`,
  `fundamentals`, `news_sentiment_finbert` and `market_regime`. It's protected by `Authorization: Bearer <access code>`. Trading tools are never exposed.
- **Client:** `app/agents/mcp_client.py` uses **langchain-mcp-adapters** to load MCP tools. The **Bear-Case Analyst** gets its evidence through
  MCP (tool names start with `aureon_`) and falls back to built-in tools if MCP is unreachable. Add outside servers with `MCP_SERVERS` (JSON).

**Live demo:** run `npx @modelcontextprotocol/inspector`, choose Transport **Streamable HTTP**, set the URL to `https://<your-aureon-url>/mcp/`, add the header
`Authorization: Bearer <client access code>`, click **Connect**, then open **Tools** and run `market_regime`. Then ask Aureon "What's the bear case on TCS?"
and watch the live feed show `aureon_*` MCP calls by the Bear-Case Analyst.

## Diagnostics
**Broker console → Run diagnostics** (also run automatically at the end of `deploy.sh`) checks every live dependency once:
Neon, NSE and MCX quotes, candles, indicators, macro, fundamentals, Google and Yahoo news, FinBERT, Whisper, MCP and every Gemini model.
Each shows PASS or FAIL with the reason. Unhandled errors return a readable message, never a bare 500, and the full traceback goes to the Cloud Run logs.

## AI spending limits
Every model call goes through `app/llm_budget.py`, which estimates the cost from token counts. The defaults are:
a **$2/day** cap shared by all clients (the agents pause until midnight IST once it's reached), **25 requests per client per day**, and **45 AI calls per request** (to stop runaway agent loops).
Admins can change all three in **House rules** and watch spend by model and by client in the Broker console. The GCP budget alert ($30) is the second safety net.
The Gemini 2.5 models have been retired, so the defaults are `gemini-3.1-pro-preview` (Manager), `gemini-3.5-flash` and `gemini-3.1-flash-lite`.
Use **Test all models** in the Broker console (deploy runs this check automatically) to confirm each one responds.

## Voice commands
"Scan the watchlist for a swing trade" · "How is my portfolio?" · "Analyse crude oil intraday" ·
"**Approve**" / "**Reject**, too much risk in banks" (decides the pending trade) · "Why did you pick Infosys?"

## Safety
Paper trading only (no broker order API is wired up) · every order needs human approval (Deep Agents `interrupt_on`)
· hard limits in code: max 10% of equity per position, ₹2L per order, 2% daily loss, 8 positions, no naked delivery shorts
· not investment advice.
