"""Coalesce concurrent per-item async lookups into batched calls.

DataLoader-style. Turns N concurrent ``load(key)`` calls into roughly
``ceil(N / max_batch)`` ``batch_fn(keys)`` calls, so a check that hits a shared,
rate-limited upstream (e.g. Socket's quota-metered /v0/purl) makes one request
for a whole sweep of packages instead of one request per package.

Process-local: each worker coalesces only the activities it runs — there is no
cross-worker coordination — which is exactly right for a Temporal worker, where
a sweep's check activities land together in one event loop. If the batched call
fails, every coalesced waiter receives the same exception, so callers degrade
uniformly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

_all_batchers: list["CoalescingBatcher"] = []


def reset_all_batchers() -> None:
    """Drop pending state on every batcher. Call from test fixtures for isolation."""
    for b in _all_batchers:
        b._pending.clear()
        if b._timer is not None:
            b._timer.cancel()
            b._timer = None


class CoalescingBatcher:
    __slots__ = ("_batch_fn", "_max_batch", "_window", "_pending", "_timer")

    def __init__(
        self,
        batch_fn: Callable[[list[Any]], Awaitable[dict[Any, Any]]],
        *,
        max_batch: int = 100,
        window_seconds: float = 0.15,
    ) -> None:
        """``batch_fn`` takes the list of distinct keys collected in a window and returns a
        ``{key: value}`` dict. A key missing from the returned dict resolves to ``None``."""
        self._batch_fn = batch_fn
        self._max_batch = max_batch
        self._window = window_seconds
        self._pending: dict[Any, asyncio.Future] = {}
        self._timer: asyncio.TimerHandle | None = None
        _all_batchers.append(self)

    async def load(self, key: Hashable) -> Any:
        """Return the batched value for ``key``, coalescing with concurrent loads."""
        existing = self._pending.get(key)
        if existing is not None:
            return await existing  # identical key already queued this window

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[key] = fut

        if len(self._pending) >= self._max_batch:
            self._flush()
        elif self._timer is None:
            self._timer = loop.call_later(self._window, self._flush)
        return await fut

    def _flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._pending:
            return
        batch = self._pending
        self._pending = {}
        asyncio.ensure_future(self._run(batch))

    async def _run(self, batch: dict[Any, asyncio.Future]) -> None:
        try:
            results = await self._batch_fn(list(batch.keys()))
        except Exception as exc:  # noqa: BLE001 — propagate to every coalesced waiter
            for fut in batch.values():
                if not fut.done():
                    fut.set_exception(exc)
            return
        for key, fut in batch.items():
            if not fut.done():
                fut.set_result(results.get(key))
