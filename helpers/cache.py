"""Simple in-process TTL cache for Temporal activity results.

Thread-safe for asyncio (single-threaded cooperative scheduling). Contents
are lost on worker restart — that's fine. The goal is to avoid redundant
network calls when multiple repos bump the same package to the same version
within the same worker process lifetime.

Usage:
    _cache = ActivityCache()                    # immutable results — cache forever
    _cache = ActivityCache(ttl_seconds=3600)    # stale-ish results — 1 hour TTL

    # Simple get/set:
    key = (ecosystem, package, old_version, new_version)
    if (hit := _cache.get(key)) is not None:
        return hit
    result = await _fetch(...)
    _cache.set(key, result)
    return result

    # Or with in-flight deduplication (prevents thundering herd when many
    # concurrent activities request the same key simultaneously):
    return await _cache.get_or_compute(key, lambda: _fetch(...))
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

INDEFINITE = float("inf")

_all_caches: list["ActivityCache"] = []


def clear_all_caches() -> None:
    """Clear every ActivityCache instance. Call from test fixtures."""
    for cache in _all_caches:
        cache.clear()


class ActivityCache:
    __slots__ = ("_ttl", "_store", "_pending")

    def __init__(self, ttl_seconds: float = INDEFINITE) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple, tuple[float, Any]] = {}
        self._pending: dict[tuple, asyncio.Future[Any]] = {}
        _all_caches.append(self)

    def get(self, key: tuple) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if self._ttl != INDEFINITE and time.monotonic() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: tuple, value: Any) -> None:
        self._store[key] = (time.monotonic(), value)

    async def get_or_compute(self, key: tuple, fn: Callable[[], Awaitable[T]]) -> T:
        """Return cached value or compute it, deduplicating concurrent identical requests.

        If two coroutines call get_or_compute with the same key simultaneously,
        only one runs fn(); the other awaits the same Future. Errors also propagate
        to all waiters so Temporal can retry each activity independently.
        """
        hit = self.get(key)
        if hit is not None:
            return hit  # type: ignore[return-value]

        if key in self._pending:
            return await self._pending[key]

        fut: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        try:
            result = await fn()
            self.set(key, result)
            fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._pending.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
        self._pending.clear()

    def __len__(self) -> int:
        return len(self._store)
