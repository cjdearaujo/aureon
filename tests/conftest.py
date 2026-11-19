import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.update(MARKET_DATA_PROVIDER="synthetic",
                  ENFORCE_MARKET_HOURS="false", FINBERT_ENABLED="false", MCP_ENABLED="false", APP_ENV="test", TICK_TOKEN="t")
if os.path.exists("test.db"):
    os.remove("test.db")
