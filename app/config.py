"""Central configuration, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "Aureon"
    app_env: Literal["local", "dev", "prod", "test"] = "local"
    log_level: str = "INFO"
    app_passcode: str | None = Field(default=None, description="Access code for regular users")
    admin_passcode: str | None = Field(default=None, description="Access code for master (admin) accounts")
    admin_users: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["admin"])
    secret_key: str = "dev-only-change-me"   # signs login tokens

    # --- Models: one per agent. Any LangChain 'provider:model' string works:
    #     google_genai:gemini-2.5-pro | ollama:qwen3:8b | anthropic:... | openai:...
    # Current Gemini family (Nov 2026; the 2.5 models were retired). Check the admin "LLM check" after deploy.
    manager_model: str = "google_genai:gemini-3.1-pro-preview"
    research_model: str = "google_genai:gemini-3.5-flash"
    quant_model: str = "google_genai:gemini-3.5-flash"
    risk_model: str = "google_genai:gemini-3.5-flash"
    execution_model: str = "google_genai:gemini-3.1-flash-lite"
    macro_model: str = "google_genai:gemini-3.5-flash"
    fundamental_model: str = "google_genai:gemini-3.5-flash"
    bear_model: str = "google_genai:gemini-3.1-flash-lite"
    compliance_model: str = "google_genai:gemini-3.1-flash-lite"
    performance_model: str = "google_genai:gemini-3.1-flash-lite"
    utility_model: str = "google_genai:gemini-3.1-flash-lite"   # voice-intent parsing, Fin-R1 fallback
    llm_temperature: float = 0.1
    llm_max_retries: int = 6                                    # backoff on free-tier 429s
    agent_recursion_limit: int = 60
    ollama_base_url: str = "http://localhost:11434"
    # Gemini via Gemini Enterprise Agent Platform (formerly Vertex AI; inside GCP: billed to your project/credits, auth via the service account, no API key)
    gemini_vertex: bool = False
    gcp_project: str | None = None
    gcp_location: str = "global"           # Gemini 3.x preview models are served from the global endpoint

    # --- LLM spending guardrails (editable by admins in House rules) ---
    llm_daily_budget_usd: float = 2.0     # all users combined, estimated from token usage; resets midnight IST
    llm_user_daily_runs: int = 25         # chat/voice requests per client per day (admins exempt)
    llm_max_calls_per_run: int = 45       # stops runaway agent loops within one request
    llm_prices: dict[str, list[float]] = Field(default_factory=dict)  # override, e.g. {"gemini-3.5-flash":[1.5,9]}

    # --- MCP (Model Context Protocol) ---
    mcp_enabled: bool = True              # Bear-Case Analyst gets its evidence from MCP tools
    mcp_self_url: str | None = None       # default: Aureon's own MCP server at http://127.0.0.1:$PORT/mcp/
    mcp_servers: dict[str, dict] = Field(default_factory=dict)  # extra MCP servers (JSON), merged in

    # --- Finance-specialist models (Hugging Face) ---
    finr1_url: str | None = None          # Ollama endpoint serving Fin-R1 (Cloud Run GPU or local)
    finr1_model: str = "hf.co/mradermacher/Fin-R1-GGUF:Q4_K_M"
    finr1_auth: bool = False              # True on GCP: send a Google ID token to the private service
    finr1_timeout: float = 180.0
    finbert_enabled: bool = True
    finbert_model: str = "ProsusAI/finbert"

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///./trader.db"
    redis_url: str | None = None

    # --- Market data: yfinance (free, delayed) | angelone (free SmartAPI, NSE+MCX) | synthetic (tests) ---
    market_data_provider: Literal["yfinance", "angelone", "synthetic"] = "yfinance"
    angel_api_key: str | None = None
    angel_client_code: str | None = None
    angel_pin: str | None = None
    angel_totp_secret: str | None = None
    quote_cache_seconds: int = 20

    # --- Paper trading account ---
    starting_capital: float = 1_000_000.0  # INR, per user
    slippage_bps: float = 5.0

    # --- Risk limits (enforced in code, not just in prompts) ---
    max_position_pct: float = 0.10       # max % of equity in one symbol
    max_order_value: float = 200_000.0   # INR per order
    max_daily_loss_pct: float = 0.02     # halt new buys after this drawdown today
    max_open_positions: int = 8
    min_cash_buffer_pct: float = 0.05
    trading_halted: bool = False             # admin kill switch

    watchlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "ITC", "GOLD", "CRUDEOIL"]
    )

    # --- Autonomous mode ---
    # The agent scans and PROPOSES trades; every order still waits for human approval.
    enforce_market_hours: bool = True
    tick_token: str | None = None           # Cloud Scheduler sends this as X-Tick-Token

    # --- Voice: faster-whisper baked into the image (CPU, int8) ---
    whisper_model: str = "small"            # tiny | base | small (small handles Indian English well)
    whisper_language: str | None = "en"

    @field_validator("admin_users", mode="before")
    @classmethod
    def _split_admins(cls, v):
        if isinstance(v, str):
            return [s.strip().lower() for s in v.split(",") if s.strip()]
        return [s.lower() for s in v]

    @field_validator("watchlist", mode="before")
    @classmethod
    def _split_watchlist(cls, v):
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return [s.upper() for s in v]


@lru_cache
def get_settings() -> Settings:
    return Settings()
