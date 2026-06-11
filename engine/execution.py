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
        logger.warning("🔴 [LIVE] %s %d %s @ ~₹%.2f  order_id=%s — %s",
                       intent.side, intent.qty, intent.security_id, ref_price,
                       order_id, intent.reason)
        if self._db:
            await self._db.log_order(
                security_id=intent.security_id,
                exchange_segment=intent.exchange_segment,
                side=intent.side, qty=intent.qty,
                order_type="MARKET", product_type=intent.product_type,
                mode="LIVE", price=ref_price,
                dhan_order_id=order_id, run_id=self._run_id, status="PENDING",
            )
        return Fill(security_id=intent.security_id, side=intent.side,
                    qty=intent.qty, price=ref_price, order_id=order_id, mode="LIVE")
