"""
restocker_client.py - async HTTP client for Restocker's /api/v1/bank/* API; the
only path by which the Banking bot touches the coin wallet or stock exchange.
"""

from __future__ import annotations

import uuid
import asyncio
import aiohttp

EXPECTED_API_VERSION = "1.1"


class RestockerError(Exception):
    """Raised when the API returns ok:false or an HTTP error."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class RestockerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        if not base_url:
            raise ValueError("RESTOCKER_API_URL is required")
        if not token:
            raise ValueError("RESTOCKER_BANK_TOKEN is required")
        self.base_url = base_url.rstrip("/")
        self._headers = {"X-Bank-Token": token, "Content-Type": "application/json"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout, headers=self._headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, *, params=None, json=None, retries: int = 0) -> dict:
        """Make one API call.

        `retries` re-attempts ONLY transient connection failures/timeouts (never
        HTTP 4xx). It's safe to retry calls that carry an idempotency_key (the
        server dedups) or are read-only GETs — so callers set retries there and
        leave it at 0 for non-idempotent writes like stock buy/sell.
        """
        sess = await self._sess()
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                async with sess.request(method, url, params=params, json=json) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        text = await resp.text()
                        raise RestockerError(f"Non-JSON response ({resp.status}): {text[:200]}",
                                             status=resp.status)
                    if resp.status >= 400 or not data.get("ok", False):
                        raise RestockerError(
                            data.get("error", f"HTTP {resp.status}"),
                            status=resp.status,
                            code=data.get("error"),
                        )
                    return data
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                if attempt >= retries:
                    raise RestockerError(f"Connection error: {e}") from e
                attempt += 1
                await asyncio.sleep(0.5 * attempt)
            except aiohttp.ClientError as e:
                raise RestockerError(f"Connection error: {e}") from e

    @staticmethod
    def _key() -> str:
        return uuid.uuid4().hex

    async def health(self) -> dict:
        """Public, unauthenticated health probe. Returns {ok, version, enabled, ...}."""
        return await self._request("GET", "/api/v1/bank/health", retries=2)

    async def ping(self) -> dict:
        """Authenticated probe — also verifies the token is accepted."""
        return await self._request("GET", "/api/v1/bank/ping", retries=2)

    async def check_version(self) -> tuple[bool, str | None]:
        """Return (matches, server_version). Best-effort; never raises."""
        try:
            data = await self.health()
            sv = data.get("version")
            return (sv == EXPECTED_API_VERSION, sv)
        except RestockerError:
            return (False, None)

    async def get_balance(self, user_id: str | int) -> dict:
        return await self._request("GET", "/api/v1/bank/balance",
                                   params={"user_id": str(user_id)}, retries=2)

    async def adjust(self, user_id: str | int, amount: int, *, reason: str = "",
                     count_principal: bool = True, idempotency_key: str | None = None) -> dict:
        """Credit (amount>0) or debit (amount<0) a wallet. Raises RestockerError
        with code='insufficient' if a debit would overdraw. Carries an
        idempotency key so a transient-error retry can't double-apply."""
        return await self._request("POST", "/api/v1/bank/adjust", retries=2, json={
            "user_id": str(user_id),
            "amount": int(amount),
            "reason": reason,
            "count_principal": count_principal,
            "idempotency_key": idempotency_key or self._key(),
        })

    async def transfer(self, from_user, to_user, amount: int, *, reason: str = "",
                       idempotency_key: str | None = None) -> dict:
        return await self._request("POST", "/api/v1/bank/transfer", retries=2, json={
            "from_user": str(from_user),
            "to_user": str(to_user),
            "amount": int(amount),
            "reason": reason,
            "idempotency_key": idempotency_key or self._key(),
        })

    async def list_stocks(self) -> list[dict]:
        data = await self._request("GET", "/api/v1/bank/stocks", retries=2)
        return data.get("markets", [])

    async def portfolio(self, user_id: str | int) -> list[dict]:
        data = await self._request("GET", "/api/v1/bank/portfolio",
                                   params={"user_id": str(user_id)}, retries=2)
        return data.get("holdings", [])

    #: The two trade calls, with everything core's API already accepts and this
    #: client never sent: an idempotency key, and the bounds the user agreed to.
    #:
    #: Without a key these ran at `retries=0`, because a retried trade with no key
    #: buys twice. That left the one money path where a timeout is unrecoverable:
    #: the bank could not ask whether the trade had happened. With a key the server
    #: dedups and replays the stored response, so a transport retry is safe.
    #:
    #: `quote_price` and `max_total`/`min_total` are what the user was shown and
    #: confirmed. If the market has moved past them the engine refuses with
    #: `slippage` BEFORE anything moves, and that code releases the key, so a
    #: re-quote goes straight through.

    async def stock_buy(self, user_id, market_id: str, shares: int, *,
                        name: str | None = None, idempotency_key: str | None = None,
                        quote_price: float | None = None,
                        max_total: int | None = None,
                        max_slippage_bps: int | None = None) -> dict:
        body = {"user_id": str(user_id), "market_id": market_id,
                "shares": int(shares), "name": name}
        if idempotency_key:
            body["idempotency_key"] = str(idempotency_key)
        if quote_price is not None:
            body["quote_price"] = float(quote_price)
        if max_total is not None:
            body["max_total"] = int(max_total)
        if max_slippage_bps is not None:
            body["max_slippage_bps"] = int(max_slippage_bps)
        return await self._request("POST", "/api/v1/bank/stock/buy", json=body,
                                   retries=2 if idempotency_key else 0)

    async def stock_sell(self, user_id, market_id: str, shares: int, *,
                         name: str | None = None, idempotency_key: str | None = None,
                         quote_price: float | None = None,
                         min_total: int | None = None,
                         max_slippage_bps: int | None = None) -> dict:
        body = {"user_id": str(user_id), "market_id": market_id,
                "shares": int(shares), "name": name}
        if idempotency_key:
            body["idempotency_key"] = str(idempotency_key)
        if quote_price is not None:
            body["quote_price"] = float(quote_price)
        if min_total is not None:
            body["min_total"] = int(min_total)
        if max_slippage_bps is not None:
            body["max_slippage_bps"] = int(max_slippage_bps)
        return await self._request("POST", "/api/v1/bank/stock/sell", json=body,
                                   retries=2 if idempotency_key else 0)
