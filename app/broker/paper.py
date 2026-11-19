"""Paper-trading broker: simulated fills at market price ± slippage, with Indian charges.

Every user gets their own account (starting capital from settings). Positions are signed:
positive = long, negative = short (short selling only allowed intraday, product MIS).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import Account, EquitySnapshot, Order, Position, SessionLocal
from app.market.data import get_provider, is_commodity, normalize


def estimate_charges(symbol: str, side: str, qty: int, price: float, product: str = "CNC") -> dict:
    """Approximate Zerodha-style statutory charges in INR (2026 rates, simplified)."""
    turnover = qty * price
    buy = side.upper() == "BUY"
    if is_commodity(symbol):
        brokerage = min(20.0, turnover * 0.0003)
        stt = 0.0 if buy else turnover * 0.0001          # CTT on sell side
        exchange = turnover * 0.000021
        stamp = turnover * 0.00002 if buy else 0.0
    elif product == "MIS":
        brokerage = min(20.0, turnover * 0.0003)
        stt = 0.0 if buy else turnover * 0.00025
        exchange = turnover * 0.0000297
        stamp = turnover * 0.00003 if buy else 0.0
    else:  # CNC delivery
        brokerage = 0.0
        stt = turnover * 0.001
        exchange = turnover * 0.0000297
        stamp = turnover * 0.00015 if buy else 0.0
    sebi = turnover * 0.000001
    gst = 0.18 * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + stamp + sebi + gst
    return {k: round(v, 2) for k, v in {
        "turnover": turnover, "brokerage": brokerage, "stt_ctt": stt, "exchange": exchange,
        "stamp_duty": stamp, "sebi": sebi, "gst": gst, "total": total}.items()}


class PaperBroker:
    async def _account(self, session, account_id: str) -> Account:
        acct = await session.get(Account, account_id)
        if acct is None:
            cap = get_settings().starting_capital
            acct = Account(id=account_id, cash=cap, starting_capital=cap, realized_pnl=0.0, fees_paid=0.0)
            session.add(acct)
            await session.flush()
        return acct

    async def portfolio(self, account_id: str) -> dict:
        provider = get_provider()
        async with SessionLocal() as session:
            acct = await self._account(session, account_id)
            await session.commit()
            positions = (await session.scalars(select(Position).where(Position.account_id == account_id))).all()
        rows, mv = [], 0.0
        for p in positions:
            try:
                ltp = (await provider.quote(p.symbol))["ltp"]
            except Exception:
                ltp = p.avg_price
            value = p.quantity * ltp
            mv += value
            rows.append({
                "symbol": p.symbol, "quantity": p.quantity, "avg_price": round(p.avg_price, 2), "ltp": ltp,
                "market_value": round(value, 2), "unrealized_pnl": round((ltp - p.avg_price) * p.quantity, 2),
                "unrealized_pct": round((ltp / p.avg_price - 1) * 100 * (1 if p.quantity > 0 else -1), 2),
            })
        equity = acct.cash + mv
        return {
            "account_id": account_id, "cash": round(acct.cash, 2), "market_value": round(mv, 2),
            "equity": round(equity, 2), "starting_capital": acct.starting_capital,
            "total_pnl": round(equity - acct.starting_capital, 2),
            "total_return_pct": round((equity / acct.starting_capital - 1) * 100, 2),
            "realized_pnl": round(acct.realized_pnl, 2), "fees_paid": round(acct.fees_paid, 2),
            "positions": sorted(rows, key=lambda r: -abs(r["market_value"])),
        }

    async def day_pnl(self, account_id: str) -> float:
        """Equity change vs the first snapshot of today (UTC day; fine for a demo)."""
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        async with SessionLocal() as session:
            first = await session.scalar(
                select(EquitySnapshot).where(EquitySnapshot.account_id == account_id, EquitySnapshot.taken_at >= today)
                .order_by(EquitySnapshot.taken_at).limit(1))
        if first is None:
            return 0.0
        return (await self.portfolio(account_id))["equity"] - first.equity

    async def snapshot(self, account_id: str) -> None:
        pf = await self.portfolio(account_id)
        async with SessionLocal() as session:
            session.add(EquitySnapshot(account_id=account_id, equity=pf["equity"], cash=pf["cash"]))
            await session.commit()

    async def place_order(self, account_id: str, symbol: str, side: str, quantity: int, order_type: str = "MARKET",
                          limit_price: float | None = None, product: str = "CNC", rationale: str = "",
                          source: str = "agent") -> dict:
        symbol, side, order_type, product = normalize(symbol), side.upper(), order_type.upper(), product.upper()
        quote = await get_provider().quote(symbol)
        ltp = float(quote["ltp"])
        slip = get_settings().slippage_bps / 10_000
        async with SessionLocal() as session:
            acct = await self._account(session, account_id)
            order = Order(account_id=account_id, symbol=symbol, side=side, quantity=quantity, order_type=order_type,
                          limit_price=limit_price, product=product, status="OPEN", rationale=rationale, source=source)
            session.add(order)

            marketable = order_type == "MARKET" or (side == "BUY" and ltp <= (limit_price or 0)) or \
                (side == "SELL" and ltp >= (limit_price or float("inf")))
            if not marketable:
                await session.commit()
                return self._order_dict(order, note="Limit not reached; order resting (re-checked on each tick).")

            fill = ltp * (1 + slip) if side == "BUY" else ltp * (1 - slip)
            if order_type == "LIMIT":
                fill = min(fill, limit_price) if side == "BUY" else max(fill, limit_price)
            await self._fill(session, acct, order, fill)
            await session.commit()
        await self.snapshot(account_id)
        return self._order_dict(order)

    async def _fill(self, session, acct: Account, order: Order, fill: float) -> None:
        charges = estimate_charges(order.symbol, order.side, order.quantity, fill, order.product)
        signed = order.quantity if order.side == "BUY" else -order.quantity
        pos = await session.get(Position, (acct.id, order.symbol))
        realized = 0.0
        if pos is None:
            session.add(Position(account_id=acct.id, symbol=order.symbol, quantity=signed, avg_price=fill))
        else:
            old = pos.quantity
            if old == 0 or (old > 0) == (signed > 0):  # adding to position
                new = old + signed
                pos.avg_price = (pos.avg_price * abs(old) + fill * abs(signed)) / abs(new)
                pos.quantity = new
            else:  # reducing / closing / flipping
                closed = min(abs(old), abs(signed))
                realized = (fill - pos.avg_price) * closed * (1 if old > 0 else -1)
                new = old + signed
                if new == 0:
                    await session.delete(pos)
                elif (new > 0) != (old > 0):  # flipped
                    pos.quantity, pos.avg_price = new, fill
                else:
                    pos.quantity = new
        acct.cash += -signed * fill - charges["total"]
        acct.realized_pnl += realized - charges["total"]
        acct.fees_paid += charges["total"]
        order.status, order.fill_price, order.fees = "FILLED", round(fill, 2), charges["total"]
        order.realized_pnl = round(realized, 2)
        order.filled_at = datetime.now(timezone.utc)

    async def check_open_orders(self, account_id: str | None = None) -> int:
        """Fill resting LIMIT orders whose price has been reached. Returns number filled."""
        filled = 0
        async with SessionLocal() as session:
            q = select(Order).where(Order.status == "OPEN")
            if account_id:
                q = q.where(Order.account_id == account_id)
            for order in (await session.scalars(q)).all():
                ltp = float((await get_provider().quote(order.symbol))["ltp"])
                if (order.side == "BUY" and ltp <= order.limit_price) or (order.side == "SELL" and ltp >= order.limit_price):
                    await self._fill(session, await self._account(session, order.account_id), order, order.limit_price)
                    filled += 1
            await session.commit()
        return filled

    async def cancel_order(self, account_id: str, order_id: str) -> dict:
        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            if not order or order.account_id != account_id:
                return {"error": f"order {order_id} not found"}
            if order.status != "OPEN":
                return {"error": f"order {order_id} is {order.status}, cannot cancel"}
            order.status = "CANCELLED"
            await session.commit()
            return self._order_dict(order)

    async def reject(self, account_id: str, symbol: str, side: str, quantity: int, reason: str, rationale: str = "",
                     order_type: str = "MARKET", limit_price: float | None = None, product: str = "CNC",
                     source: str = "agent") -> dict:
        async with SessionLocal() as session:
            order = Order(account_id=account_id, symbol=normalize(symbol), side=side.upper(), quantity=quantity,
                          order_type=order_type.upper(), limit_price=limit_price, product=product.upper(),
                          status="REJECTED", reject_reason=reason, rationale=rationale, source=source)
            session.add(order)
            await session.commit()
            return self._order_dict(order)

    async def reset(self, account_id: str) -> None:
        """Wipe a paper account back to starting capital (orders, positions, equity history)."""
        async with SessionLocal() as session:
            for model in (Position, Order, EquitySnapshot):
                await session.execute(delete(model).where(model.account_id == account_id))
            acct = await session.get(Account, account_id)
            if acct:
                await session.delete(acct)
            await session.commit()

    async def orders(self, account_id: str | None, limit: int = 50) -> list[dict]:
        async with SessionLocal() as session:
            q = select(Order).order_by(Order.created_at.desc()).limit(limit)
            if account_id:
                q = q.where(Order.account_id == account_id)
            rows = (await session.scalars(q)).all()
        return [self._order_dict(o) for o in rows]

    @staticmethod
    def _order_dict(o: Order, note: str | None = None) -> dict:
        d = {c: getattr(o, c) for c in ("id", "account_id", "symbol", "side", "quantity", "order_type", "product", "limit_price",
                                        "status", "fill_price", "fees", "realized_pnl", "rationale", "reject_reason",
                                        "source")}
        d["created_at"] = o.created_at.isoformat() if o.created_at else None
        d["filled_at"] = o.filled_at.isoformat() if o.filled_at else None
        if note:
            d["note"] = note
        return d


broker = PaperBroker()
