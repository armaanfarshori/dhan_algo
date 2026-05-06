"""
DhanHQ v2 Async Client Wrapper
Production-grade async HTTP client for the DhanHQ Trading API v2.
Handles auth, rate limiting, retries, and response normalisation.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from collections import deque

import aiohttp

logger = logging.getLogger("dhan.client")


class RateLimiter:
    """Token-bucket rate limiter respecting DhanHQ API limits."""

    LIMITS = {
        "orders":        {"per_sec": 10, "per_min": 250, "per_hour": 1000, "per_day": 7000},
        "data":          {"per_sec": 5,  "per_min": None, "per_hour": None, "per_day": 100_000},
        "quote":         {"per_sec": 1,  "per_min": None, "per_hour": None, "per_day": None},
        "non_trading":   {"per_sec": 20, "per_min": None, "per_hour": None, "per_day": None},
    }

    def __init__(self, category: str = "orders"):
        self.category = category
        cfg = self.LIMITS[category]
        self.per_sec = cfg["per_sec"]
        self._second_window: deque = deque()

    async def acquire(self):
        now = time.monotonic()
        # Clear entries older than 1 second
        while self._second_window and now - self._second_window[0] > 1.0:
            self._second_window.popleft()
        if len(self._second_window) >= self.per_sec:
            sleep_for = 1.0 - (now - self._second_window[0])
            if sleep_for > 0:
                logger.debug(f"Rate limit hit ({self.category}), sleeping {sleep_for:.3f}s")
                await asyncio.sleep(sleep_for)
        self._second_window.append(time.monotonic())


class DhanAPIError(Exception):
    """Raised when DhanHQ API returns an error response."""
    def __init__(self, error_type: str, error_code: str, error_message: str):
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(f"[{error_code}] {error_message}")


AUTH_ERROR_CODES = {"DH-901", "DH-807", "DH-902"}


class DhanClient:
    """
    Async wrapper around the DhanHQ REST API v2.

    Usage:
        async with DhanClient(client_id="...", access_token="...") as dhan:
            ltp = await dhan.get_ltp({"NSE_EQ": [1333]})

    With auth manager (auto token refresh):
        async with DhanClient(client_id="...", access_token="...", auth_manager=mgr) as dhan:
            ...
    """

    BASE_URL = "https://api.dhan.co/v2"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        sandbox: bool = False,
        max_retries: int = 3,
        timeout: int = 10,
        auth_manager=None,
    ):
        self.client_id    = client_id
        self.access_token = access_token
        self.sandbox      = sandbox
        self.max_retries  = max_retries
        self.timeout      = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._auth_manager = auth_manager

        self._rate_limiters = {
            cat: RateLimiter(cat)
            for cat in ("orders", "data", "quote", "non_trading")
        }

        # Register token refresh callback if auth manager provided
        if auth_manager:
            auth_manager.on_token_refresh(self._on_token_refreshed)

    def _on_token_refreshed(self, new_token: str):
        """Called by DhanAuthManager when a new token is generated."""
        self.access_token = new_token
        if self._session:
            self._session.headers.update({"access-token": new_token})
        logger.info("DhanClient: access token updated")

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "access-token": self.access_token,
            "client-id":    self.client_id,
        }

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers=self._headers,
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        rate_category: str = "orders",
        payload: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Any:
        """Core request method with retry logic and rate limiting."""
        if self._session is None:
            raise RuntimeError("Use DhanClient as async context manager.")

        rl = self._rate_limiters[rate_category]
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"

        for attempt in range(1, self.max_retries + 1):
            await rl.acquire()
            try:
                async with self._session.request(
                    method, url, json=payload, params=params
                ) as resp:
                    body = await resp.json(content_type=None)

                    if resp.status == 200 or resp.status == 202:
                        return body

                    # DhanHQ error envelope
                    if isinstance(body, dict) and "errorCode" in body:
                        error_code = body.get("errorCode", "")
                        # Auto-refresh token on auth errors and retry once
                        if error_code in AUTH_ERROR_CODES and self._auth_manager:
                            logger.warning(f"Auth error {error_code} — refreshing token and retrying")
                            new_token = await self._auth_manager.handle_auth_error()
                            self._on_token_refreshed(new_token)
                            continue
                        raise DhanAPIError(
                            body.get("errorType", "UNKNOWN"),
                            error_code or str(resp.status),
                            body.get("errorMessage", "Unknown error"),
                        )

                    # Retriable HTTP errors
                    if resp.status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                        wait = 2 ** attempt
                        logger.warning(f"HTTP {resp.status} on {endpoint}, retry {attempt} in {wait}s")
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()

            except aiohttp.ClientError as e:
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(2 ** attempt)
                logger.warning(f"Network error {e}, retry {attempt}")

        raise RuntimeError(f"All {self.max_retries} retries exhausted for {endpoint}")

    # ------------------------------------------------------------------ #
    #  ORDER MANAGEMENT
    # ------------------------------------------------------------------ #

    async def place_order(
        self,
        transaction_type: str,
        exchange_segment: str,
        product_type: str,
        order_type: str,
        security_id: str,
        quantity: int,
        price: float = 0.0,
        trigger_price: float = 0.0,
        validity: str = "DAY",
        disclosed_quantity: int = 0,
        after_market_order: bool = False,
        amo_time: str = "",
        correlation_id: str = "",
        slice_order: bool = False,
    ) -> Dict:
        """Place a new order (or sliced order for F&O over freeze limit)."""
        payload = {
            "dhanClientId": self.client_id,
            "correlationId": correlation_id,
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": validity,
            "securityId": security_id,
            "quantity": quantity,
            "disclosedQuantity": disclosed_quantity,
            "price": price,
            "triggerPrice": trigger_price,
            "afterMarketOrder": after_market_order,
            "amoTime": amo_time,
        }
        endpoint = "orders/slicing" if slice_order else "orders"
        result = await self._request("POST", endpoint, "orders", payload)
        logger.info(f"Order placed: {result}")
        return result

    async def modify_order(
        self,
        order_id: str,
        order_type: str,
        quantity: int,
        price: float,
        trigger_price: float = 0.0,
        validity: str = "DAY",
        disclosed_quantity: int = 0,
    ) -> Dict:
        """Modify a pending order."""
        payload = {
            "dhanClientId": self.client_id,
            "orderId": order_id,
            "orderType": order_type,
            "quantity": quantity,
            "price": price,
            "disclosedQuantity": disclosed_quantity,
            "triggerPrice": trigger_price,
            "validity": validity,
        }
        return await self._request("PUT", f"orders/{order_id}", "orders", payload)

    async def cancel_order(self, order_id: str) -> Dict:
        """Cancel a pending order."""
        return await self._request("DELETE", f"orders/{order_id}", "orders")

    async def get_order_book(self) -> List[Dict]:
        """Retrieve all orders for today."""
        return await self._request("GET", "orders", "non_trading")

    async def get_order_by_id(self, order_id: str) -> Dict:
        """Retrieve a specific order by its ID."""
        return await self._request("GET", f"orders/{order_id}", "non_trading")

    async def get_order_by_correlation_id(self, correlation_id: str) -> Dict:
        """Retrieve order using your custom correlation ID."""
        return await self._request("GET", f"orders/external/{correlation_id}", "non_trading")

    async def get_trade_book(self) -> List[Dict]:
        """Retrieve all trades for today."""
        return await self._request("GET", "trades", "non_trading")

    async def get_trades_by_order(self, order_id: str) -> List[Dict]:
        """Retrieve trades for a specific order."""
        return await self._request("GET", f"trades/{order_id}", "non_trading")

    # ------------------------------------------------------------------ #
    #  FOREVER ORDERS (GTT / OCO)
    # ------------------------------------------------------------------ #

    async def create_forever_order(
        self,
        order_flag: str,          # "SINGLE" or "OCO"
        transaction_type: str,
        exchange_segment: str,
        product_type: str,
        order_type: str,
        security_id: str,
        quantity: int,
        price: float,
        trigger_price: float,
        validity: str = "DAY",
        disclosed_quantity: int = 0,
        price1: float = 0.0,
        trigger_price1: float = 0.0,
        quantity1: int = 0,
        correlation_id: str = "",
    ) -> Dict:
        """Create a Forever (GTT) or OCO order."""
        payload = {
            "dhanClientId": self.client_id,
            "correlationId": correlation_id,
            "orderFlag": order_flag,
            "transactionType": transaction_type,
            "exchangeSegment": exchange_segment,
            "productType": product_type,
            "orderType": order_type,
            "validity": validity,
            "securityId": security_id,
            "quantity": quantity,
            "disclosedQuantity": disclosed_quantity,
            "price": price,
            "triggerPrice": trigger_price,
        }
        if order_flag == "OCO":
            payload.update({"price1": price1, "triggerPrice1": trigger_price1, "quantity1": quantity1})
        return await self._request("POST", "forever/orders", "orders", payload)

    async def modify_forever_order(
        self,
        order_id: str,
        order_flag: str,
        order_type: str,
        leg_name: str,
        quantity: int,
        price: float,
        trigger_price: float,
        validity: str = "DAY",
        disclosed_quantity: int = 0,
    ) -> Dict:
        payload = {
            "dhanClientId": self.client_id,
            "orderId": order_id,
            "orderFlag": order_flag,
            "orderType": order_type,
            "legName": leg_name,
            "quantity": quantity,
            "price": price,
            "disclosedQuantity": disclosed_quantity,
            "triggerPrice": trigger_price,
            "validity": validity,
        }
        return await self._request("PUT", f"forever/orders/{order_id}", "orders", payload)

    async def cancel_forever_order(self, order_id: str) -> Dict:
        return await self._request("DELETE", f"forever/orders/{order_id}", "orders")

    async def get_forever_orders(self) -> List[Dict]:
        return await self._request("GET", "forever/all", "non_trading")

    # ------------------------------------------------------------------ #
    #  MARKET DATA
    # ------------------------------------------------------------------ #

    async def get_ltp(self, instruments: Dict[str, List[int]]) -> Dict:
        """
        Get Last Traded Price for up to 1000 instruments.
        instruments = {"NSE_EQ": [1333, 11536], "NSE_FNO": [49081]}
        """
        return await self._request("POST", "marketfeed/ltp", "quote", instruments)

    async def get_ohlc(self, instruments: Dict[str, List[int]]) -> Dict:
        """Get OHLC + LTP for up to 1000 instruments."""
        return await self._request("POST", "marketfeed/ohlc", "quote", instruments)

    async def get_full_quote(self, instruments: Dict[str, List[int]]) -> Dict:
        """Get full market depth + OI + OHLC for up to 1000 instruments."""
        return await self._request("POST", "marketfeed/quote", "quote", instruments)

    # ------------------------------------------------------------------ #
    #  PORTFOLIO & POSITIONS
    # ------------------------------------------------------------------ #

    async def get_holdings(self) -> List[Dict]:
        return await self._request("GET", "holdings", "non_trading")

    async def get_positions(self) -> List[Dict]:
        return await self._request("GET", "positions", "non_trading")

    async def get_funds(self) -> Dict:
        return await self._request("GET", "fundlimit", "non_trading")

    async def convert_position(
        self,
        from_product_type: str,
        to_product_type: str,
        exchange_segment: str,
        security_id: str,
        trading_symbol: str,
        quantity: int,
        transaction_type: str,
    ) -> Dict:
        payload = {
            "dhanClientId": self.client_id,
            "fromProductType": from_product_type,
            "toProductType": to_product_type,
            "exchangeSegment": exchange_segment,
            "securityId": security_id,
            "tradingSymbol": trading_symbol,
            "convertQty": quantity,
            "transactionType": transaction_type,
        }
        return await self._request("POST", "positions/convert", "orders", payload)

    # ------------------------------------------------------------------ #
    #  OPTION CHAIN
    # ------------------------------------------------------------------ #

    async def get_option_chain(
        self, underlying_scrip: str, expiry_date: str
    ) -> Dict:
        params = {"UnderlyingScrip": underlying_scrip, "ExpiryDate": expiry_date}
        return await self._request("GET", "optionchain", "data", params=params)

    # ------------------------------------------------------------------ #
    #  HISTORICAL DATA
    # ------------------------------------------------------------------ #

    async def get_daily_historical(
        self,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_date: str,
        to_date: str,
    ) -> Dict:
        payload = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "fromDate": from_date,
            "toDate": to_date,
        }
        return await self._request("POST", "charts/historical", "data", payload)

    async def get_intraday_historical(
        self,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> Dict:
        payload = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": interval,
            "fromDate": from_date,
            "toDate": to_date,
        }
        return await self._request("POST", "charts/intraday", "data", payload)
