"""Runtime-editable settings.

- Global settings (admin console): risk limits, watchlist, market-hours rule, kill switch.
  Stored in the DB and applied onto the live Settings object (single Cloud Run instance).
- User preferences (settings page): risk profile, trading style, voice, defaults.
"""
from __future__ import annotations

from app.config import get_settings
from app.db import KV, SessionLocal

GLOBAL_FIELDS = ("max_position_pct", "max_order_value", "max_daily_loss_pct", "max_open_positions",
                 "min_cash_buffer_pct", "watchlist", "enforce_market_hours", "trading_halted", "starting_capital",
                 "llm_daily_budget_usd", "llm_user_daily_runs", "llm_max_calls_per_run")

RISK_PROFILES = {"conservative": 0.5, "balanced": 0.75, "aggressive": 1.0}  # x admin caps

DEFAULT_PREFS = {"display_name": "", "risk_profile": "balanced", "trading_style": "swing",
                 "default_product": "CNC", "speak_replies": True, "default_symbol": "RELIANCE",
                 "notes_for_manager": ""}


async def _get(key: str) -> dict | None:
    async with SessionLocal() as s:
        row = await s.get(KV, key)
        return dict(row.value) if row else None


async def _put(key: str, value: dict) -> None:
    async with SessionLocal() as s:
        row = await s.get(KV, key)
        if row:
            row.value = value
        else:
            s.add(KV(key=key, value=value))
        await s.commit()


def global_settings() -> dict:
    st = get_settings()
    return {f: getattr(st, f) for f in GLOBAL_FIELDS}


async def load_global() -> None:
    saved = await _get("global") or {}
    apply_global(saved)


def apply_global(values: dict) -> dict:
    st = get_settings()
    for k, v in values.items():
        if k not in GLOBAL_FIELDS:
            continue
        if k == "watchlist" and isinstance(v, str):
            v = [x.strip().upper() for x in v.split(",") if x.strip()]
        current = getattr(st, k)
        setattr(st, k, type(current)(v) if not isinstance(current, list) else [str(x).upper() for x in v])
    return global_settings()


async def save_global(values: dict) -> dict:
    out = apply_global(values)
    await _put("global", out)
    return out


async def get_prefs(user: str) -> dict:
    return {**DEFAULT_PREFS, **(await _get(f"prefs:{user}") or {})}


async def save_prefs(user: str, values: dict) -> dict:
    prefs = await get_prefs(user)
    for k, v in values.items():
        if k in DEFAULT_PREFS:
            prefs[k] = type(DEFAULT_PREFS[k])(v)
    if prefs["risk_profile"] not in RISK_PROFILES:
        prefs["risk_profile"] = "balanced"
    prefs["notes_for_manager"] = prefs["notes_for_manager"][:500]
    await _put(f"prefs:{user}", prefs)
    return prefs
