"""
Order executors — the ONLY place where paper and live behavior differ.

Strategies emit OrderIntent; the runner routes intents through the risk gate
and then to exactly one executor. Paper and live share every other code path,
so paper sessions actually validate live behavior.

    PaperExecutor — simulated fill at reference price + adverse slippage,
                    journalled to the same orders table as live.
    LiveExecutor  — Dhan market order. Fill price approximated by the
                    reference price until the postback/order-detail flow
                    lands (acceptable for MARKET orders on liquid NSE_EQ).
"""
import logging
from abc import ABC, abstractmethod
from typing import Optional

from engine.types import OrderIntent, Fill

logger = logging.getLogger("dhan.engine.execution")


class OrderExecutor(ABC):
    mode: str = "?"

    @abstractmethod
    async def submit(self, intent: OrderIntent, ref_price: float) -> Optional[Fill]:
        """Execute the intent. Returns a Fill, or None if execution failed."""


class PaperExecutor(OrderExecutor):
    mode = "PAPER"

    def __init__(self, db_backend=None, run_id=None, slippage_bps: float = 2.0):
        self._db = db_backend
        self._run_id = run_id
        self._slippage_bps = slippage_bps

    async def submit(self, intent: OrderIntent, ref_price: float) -> Optional[Fill]:
        if ref_price <= 0:
            logger.warning("PaperExecutor: no reference price for %s — rejecting", intent.security_id)
            return None
        # Adverse slippage: pay up on buys, receive less on sells. A market
        # order never fills at the last printed price.
        slip = ref_price * self._slippage_bps / 10_000
        fill_price = round(ref_price + slip if intent.side == "BUY" else ref_price - slip, 2)

        logger.info("📝 [PAPER] %s %d %s @ ₹%.2f (ref %.2f, slip %.1fbps) — %s",
                    intent.side, intent.qty, intent.security_id, fill_price,
                    ref_price, self._slippage_bps, intent.reason)

        if self._db:
            await self._db.log_order(
                security_id=intent.security_id,
                exchange_segment=intent.exchange_segment,
                side=intent.side, qty=intent.qty,
                order_type="MARKET", product_type=intent.product_type,
                mode="PAPER", price=fill_price,
                dhan_order_id=None, run_id=self._run_id, status="TRADED",
            )
        return Fill(security_id=intent.security_id, side=intent.side,
                    qty=intent.qty, price=fill_price, mode="PAPER")


class LiveExecutor(OrderExecutor):
    mode = "LIVE"

    # Confirmation polls after placing a MARKET order — Dhan usually fills
    # liquid NSE_EQ within a second; ~8s of patience covers slow tape.
    _CONFIRM_WAITS = [0.5, 1.0, 1.5, 2.0, 3.0]

    def __init__(self, client, db_backend=None, run_id=None):
        self._client = client
        self._db = db_backend
        self._run_id = run_id

    async def submit(self, intent: OrderIntent, ref_price: float) -> Optional[Fill]:
        try:
            result = await self._client.place_order(
                transaction_type=intent.side,
                exchange_segment=intent.exchange_segment,
                product_type=intent.product_type,
                order_type="MARKET",
                security_id=intent.security_id,
                quantity=intent.qty,
            )
        except Exception as exc:
            logger.error("🔴 LIVE order FAILED %s %d %s: %s",
                         intent.side, intent.qty, intent.security_id, exc)
            return None

        order_id = (result or {}).get("orderId")
        logger.warning("🔴 [LIVE] %s %d %s placed @ ref ₹%.2f  order_id=%s — %s",
                       intent.side, intent.qty, intent.security_id, ref_price,
                       order_id, intent.reason)
        if not order_id:
            logger.critical("LIVE order returned no orderId (%s) — treating as failed", result)
            return None

        fill = await self._confirm_fill(order_id, intent, ref_price)

        if self._db and fill is not None:
            await self._db.log_order(
                security_id=intent.security_id,
                exchange_segment=intent.exchange_segment,
                side=intent.side, qty=fill.qty,
                order_type="MARKET", product_type=intent.product_type,
                mode="LIVE", price=fill.price,
                dhan_order_id=order_id, run_id=self._run_id, status="TRADED",
            )
        return fill

    async def _confirm_fill(self, order_id: str, intent: OrderIntent,
                            ref_price: float) -> Optional[Fill]:
        """Poll the order until TRADED and use the broker's ACTUAL average
        price — portfolio P&L must track the broker, not our reference tick.
        Rejections/cancellations return None (the strategy's direction stays
        consumed; exits retry next poll). If the order is still pending after
        all polls, fall back to the reference price LOUDLY — the boot/EOD
        broker reconcile will correct any drift."""
        import asyncio as _aio
        for wait in self._CONFIRM_WAITS:
            await _aio.sleep(wait)
            try:
                od = await self._client.get_order_by_id(order_id)
            except Exception as exc:
                logger.warning("LIVE confirm: get_order_by_id failed (%s) — retrying", exc)
                continue
            data = od.get("data", od) if isinstance(od, dict) else {}
            if isinstance(data, list):
                data = data[0] if data else {}
            status = str(data.get("orderStatus") or data.get("status") or "").upper()
            if status == "TRADED":
                avg = float(data.get("averageTradedPrice") or data.get("avgPrice")
                            or data.get("price") or ref_price)
                qty = int(data.get("filledQty") or data.get("filled_qty") or intent.qty)
                logger.warning("🔴 [LIVE] CONFIRMED %s %d %s @ ₹%.2f (order %s)",
                               intent.side, qty, intent.security_id, avg, order_id)
                return Fill(security_id=intent.security_id, side=intent.side,
                            qty=qty, price=avg, order_id=order_id, mode="LIVE")
            if status in ("REJECTED", "CANCELLED"):
                logger.critical("🔴 LIVE order %s %s: %s — %s", order_id, status,
                                intent.security_id,
                                data.get("omsErrorDescription") or data.get("reason") or "")
                return None
            # TRANSIT / PENDING — keep waiting

        logger.critical("🔴 LIVE order %s UNCONFIRMED after %ss — assuming fill @ ref ₹%.2f; "
                        "broker reconcile will correct drift",
                        order_id, sum(self._CONFIRM_WAITS), ref_price)
        return Fill(security_id=intent.security_id, side=intent.side,
                    qty=intent.qty, price=ref_price, order_id=order_id, mode="LIVE")
