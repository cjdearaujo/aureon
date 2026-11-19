"""NSE trading session helpers (IST)."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 15), time(15, 30)

# Add exchange holidays here (YYYY-MM-DD) or load them from NSE's holiday API.
HOLIDAYS: set[date] = set()


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open(at: datetime | None = None) -> bool:
    at = (at or now_ist()).astimezone(IST)
    if at.weekday() >= 5 or at.date() in HOLIDAYS:
        return False
    return OPEN <= at.time() <= CLOSE


def market_status() -> dict:
    n = now_ist()
    return {
        "exchange": "NSE",
        "now_ist": n.strftime("%Y-%m-%d %H:%M:%S"),
        "is_open": is_market_open(n),
        "session": f"{OPEN:%H:%M}-{CLOSE:%H:%M} IST, Mon-Fri",
    }
