"""Finance-specialist models from Hugging Face, used as expert tools by the agents.

- FinBERT (ProsusAI/finbert): BERT fine-tuned on financial news (Financial PhraseBank).
  Runs on CPU inside the main container; scores headline sentiment.
- Fin-R1 (SUFE-AIFLM-Lab/Fin-R1): Qwen2.5-7B fine-tuned (SFT + RL) on 60k financial reasoning
  chains. Served by Ollama on a Cloud Run L4 GPU that scales to zero (or locally).
  Not trained for tool calling, so agents *consult* it rather than run on it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


# ---------------- FinBERT ----------------
@lru_cache
def _finbert():
    from transformers import pipeline

    return pipeline("text-classification", model=get_settings().finbert_model, top_k=None, device=-1)


async def finbert_sentiment(texts: list[str]) -> list[dict]:
    if not texts:
        return []
    if not get_settings().finbert_enabled:
        return [{"label": "neutral", "score": 0.0, "note": "FinBERT disabled"} for _ in texts]

    def _run():
        out = []
        for scores in _finbert()(texts, truncation=True, max_length=128):
            best = max(scores, key=lambda x: x["score"])
            d = {x["label"]: x["score"] for x in scores}
            out.append({"label": best["label"], "confidence": round(best["score"], 3),
                        "net": round(d.get("positive", 0) - d.get("negative", 0), 3)})
        return out

    return await asyncio.to_thread(_run)


async def fetch_news(query: str, limit: int = 8, symbol: str | None = None) -> list[dict]:
    """Free headlines: Google News RSS (India edition), falling back to Yahoo Finance news."""
    try:
        return await _google_news(query, limit)
    except Exception as exc:
        log.warning("Google News unavailable (%s); trying Yahoo Finance news", exc)
    try:
        return await _yahoo_news(symbol or query.split()[0], limit)
    except Exception as exc:
        log.warning("Yahoo news unavailable too (%s)", exc)
        return []


async def _yahoo_news(symbol: str, limit: int) -> list[dict]:
    from app.market.data import COMMODITIES

    s = symbol.upper()
    ticker = COMMODITIES[s][0] if s in COMMODITIES else f"{s}.NS"

    def _get():
        import yfinance as yf

        out = []
        for n in (yf.Ticker(ticker).news or [])[:limit]:
            c = n.get("content", n)
            out.append({"title": c.get("title", ""), "published": c.get("pubDate") or c.get("providerPublishTime", ""),
                        "source": (c.get("provider") or {}).get("displayName", "Yahoo Finance")
                        if isinstance(c.get("provider"), dict) else "Yahoo Finance"})
        return [x for x in out if x["title"]]

    return await asyncio.to_thread(_get)


async def _google_news(query: str, limit: int) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Aureon research bot)"}) as c:
        r = await c.get(url)
        r.raise_for_status()
    items = []
    for it in ElementTree.fromstring(r.text).iter("item"):
        items.append({"title": it.findtext("title", ""), "published": it.findtext("pubDate", ""),
                      "source": it.findtext("source", "")})
        if len(items) >= limit:
            break
    return items


# ---------------- Fin-R1 ----------------
def _id_token(audience: str) -> str | None:
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token

        return id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)
    except Exception as exc:  # pragma: no cover - only on GCP
        log.warning("could not fetch ID token for Fin-R1: %s", exc)
        return None


async def consult_fin_r1(question: str) -> dict:
    """Ask Fin-R1; falls back to the utility Gemini model if the GPU service is unavailable."""
    s = get_settings()
    if s.finr1_url:
        headers = {}
        if s.finr1_auth and (tok := await asyncio.to_thread(_id_token, s.finr1_url)):
            headers["Authorization"] = f"Bearer {tok}"
        payload = {"model": s.finr1_model, "stream": False, "options": {"temperature": 0.2, "num_predict": 1200},
                   "messages": [{"role": "user", "content": question}]}
        try:
            async with httpx.AsyncClient(timeout=s.finr1_timeout) as c:
                r = await c.post(f"{s.finr1_url.rstrip('/')}/api/chat", json=payload, headers=headers)
                r.raise_for_status()
            text = r.json()["message"]["content"]
            answer = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() or text
            return {"model": "Fin-R1 (finance-tuned, Hugging Face)", "answer": answer[:4000]}
        except Exception as exc:
            log.warning("Fin-R1 unavailable (%s); using fallback", exc)
    from app.agents.models import make_model

    msg = await make_model(s.utility_model).ainvoke(
        "You are a financial analyst. Answer precisely with numbers where possible.\n\n" + question)
    return {"model": f"{s.utility_model} (fallback; Fin-R1 not reachable)", "answer": str(msg.content)[:4000]}
