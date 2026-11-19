"""Model factory: one LangChain chat model per agent, chosen in config (.env)."""
from __future__ import annotations

from functools import lru_cache

from langchain.chat_models import init_chat_model

from app.config import get_settings


@lru_cache
def make_model(spec: str):
    """spec = 'provider:model', e.g. 'google_genai:gemini-2.5-pro' or 'ollama:qwen3:8b'."""
    s = get_settings()
    provider, _, name = spec.partition(":")
    from app.llm_budget import guard

    kwargs = {"temperature": s.llm_temperature, "max_retries": s.llm_max_retries, "callbacks": [guard]}
    if provider == "ollama":
        kwargs = {"temperature": s.llm_temperature, "base_url": s.ollama_base_url, "callbacks": [guard]}
    elif provider == "google_genai" and s.gemini_vertex:
        kwargs.update(vertexai=True, project=s.gcp_project, location=s.gcp_location)
    return init_chat_model(name, model_provider=provider, **kwargs)
