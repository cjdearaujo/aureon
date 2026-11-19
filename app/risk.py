"""Hard risk limits — the safety net under the agents.

The Risk Manager *agent* reasons about risk; this module is the non-negotiable gate that every
order passes through right before execution, so a model mistake can never breach the limits.
"""
from __future__ import annotations

from app.broker.paper import broker, estimate_charges
from app.config import get_settings
from app.market.data import get_provider, is_commodity, normalize
from app.settings_store import RISK_PROFILES, get_prefs


async def check_trade(account_id: str, symbol: str, side: str, quantity: int,
                      price: float | None = None, product: str = "CNC") -> dict:
    s = get_settings()
    symbol, side, product = normalize(symbol), side.upper(), product.upper()
    violations, warnings = [], []
    prefs = await get_prefs(account_id)
    mult = RISK_PROFILES[prefs["risk_profile"]]          # user's risk profile scales the admin caps down
    max_pos_pct, max_order = s.max_position_pct * mult, s.max_order_value * mult
    if s.trading_halted:
        violations.append("trading is halted by the Aureon admin (kill switch)")

    if quantity <= 0:
        violations.append("quantity must be positive")
    if side not in ("BUY", "SELL"):
        violations.append("side must be BUY or SELL")
    if product not in ("CNC", "MIS"):
        violations.append("product must be CNC (delivery/swing) or MIS (intraday)")

    px = price or float((await get_provider().quote(symbol))["ltp"])
    value = px * max(quantity, 0)
    pf = await broker.portfolio(account_id)
    equity = pf["equity"]
    held = next((p["quantity"] for p in pf["positions"] if p["symbol"] == symbol), 0)
    signed = quantity if side == "BUY" else -quantity
    new_qty = held + signed
    increases = abs(new_qty) > abs(held)
    from app.market.hours import now_ist

    if product == "MIS" and increases and s.enforce_market_hours and now_ist().strftime("%H:%M") >= "15:20":
        violations.append("no new intraday (MIS) positions after the 15:20 IST square-off cutoff")

    if side == "SELL" and new_qty < 0 and product == "CNC" and not is_commodity(symbol):
        violations.append(f"cannot short {symbol} on delivery (CNC); held {held}. Use MIS for intraday shorts.")
    if value > max_order:
        violations.append(f"order value ₹{value:,.0f} exceeds max ₹{max_order:,.0f} ({prefs['risk_profile']} profile)")
    if increases and abs(new_qty) * px > max_pos_pct * equity:
        violations.append(f"position would be ₹{abs(new_qty) * px:,.0f} = {abs(new_qty) * px / equity:.1%} of equity "
                          f"(limit {max_pos_pct:.1%}, {prefs['risk_profile']} profile)")
    if side == "BUY" and increases:
        charges = estimate_charges(symbol, side, quantity, px, product)["total"]
        if pf["cash"] - value - charges < s.min_cash_buffer_pct * equity:
            violations.append(f"insufficient cash: ₹{pf['cash']:,.0f} available, keeps <{s.min_cash_buffer_pct:.0%} buffer")
    open_syms = {p["symbol"] for p in pf["positions"]}
    if held == 0 and symbol not in open_syms and len(open_syms) >= s.max_open_positions:
        violations.append(f"max open positions ({s.max_open_positions}) reached")
    day_pnl = await broker.day_pnl(account_id)
    if increases and day_pnl < -s.max_daily_loss_pct * equity:
        violations.append(f"daily loss limit hit (₹{day_pnl:,.0f}); only risk-reducing orders allowed today")
    if symbol not in s.watchlist:
        warnings.append(f"{symbol} is not on the watchlist")

    return {
        "approved_by_risk_engine": not violations, "symbol": symbol, "side": side, "quantity": quantity,
        "est_price": round(px, 2), "order_value": round(value, 2),
        "pct_of_equity": round(value / equity * 100, 2) if equity else None,
        "position_after": new_qty, "violations": violations, "warnings": warnings,
        "risk_profile": prefs["risk_profile"],
        "limits": {"max_position_pct": round(max_pos_pct, 4), "max_order_value": max_order,
                   "max_daily_loss_pct": s.max_daily_loss_pct, "max_open_positions": s.max_open_positions},
    }
