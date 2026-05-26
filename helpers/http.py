"""Shared httpx.AsyncClient for all signal activities.

A single client with connection pooling replaces per-call instantiation,
reusing TCP connections and TLS sessions across activity invocations.
Timeouts are specified per-request, not per-client.
"""

import asyncio
from typing import Any

import httpx

_client: httpx.AsyncClient | None = None

# Transient errors worth retrying — network blips, server overload, protocol hiccups.
_TRANSIENT_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


def get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first call."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )
    return _client


async def get_with_retry(
    url: str,
    *,
    max_attempts: int = 3,
    backoff: float = 0.1,
    timeout: float = 10.0,
    **kwargs: Any,
) -> httpx.Response | None:
    """GET url with automatic retry on transient failures.

    Retries on network errors (_TRANSIENT_EXCEPTIONS) and HTTP 5xx responses
    using exponential backoff. Returns None when all attempts fail so callers
    can degrade gracefully rather than propagating exceptions.

    Non-transient errors (4xx, unexpected exception types) return None immediately
    without retrying — they won't get better with another attempt.
    """
    for attempt in range(max_attempts):
        try:
            resp = await get_client().get(url, timeout=timeout, **kwargs)
            if resp.status_code < 500:
                return resp
            # 5xx: server-side transient error, fall through to retry
        except _TRANSIENT_EXCEPTIONS:
            pass
        except Exception:
            return None  # Non-transient — give up immediately
        if attempt < max_attempts - 1:
            await asyncio.sleep(backoff * (2**attempt))
    return None
