"""Unit tests for the CoalescingBatcher (helpers/batcher.py)."""

import asyncio

import pytest

from helpers.batcher import CoalescingBatcher


async def test_concurrent_distinct_keys_coalesce_into_one_call():
    calls: list[list[int]] = []

    async def batch_fn(keys):
        calls.append(list(keys))
        return {k: k * 10 for k in keys}

    b = CoalescingBatcher(batch_fn, window_seconds=0.05)
    results = await asyncio.gather(b.load(1), b.load(2), b.load(3))

    assert results == [10, 20, 30]
    assert len(calls) == 1  # one batched call for all three
    assert sorted(calls[0]) == [1, 2, 3]


async def test_single_key_still_works():
    async def batch_fn(keys):
        return {k: f"v{k}" for k in keys}

    b = CoalescingBatcher(batch_fn, window_seconds=0.05)
    assert await b.load("a") == "va"


async def test_duplicate_concurrent_keys_deduped():
    calls: list[list[str]] = []

    async def batch_fn(keys):
        calls.append(list(keys))
        return {k: k.upper() for k in keys}

    b = CoalescingBatcher(batch_fn, window_seconds=0.05)
    r1, r2 = await asyncio.gather(b.load("x"), b.load("x"))

    assert r1 == r2 == "X"
    assert calls == [["x"]]  # the key was requested once


async def test_missing_key_resolves_to_none():
    async def batch_fn(keys):
        return {}  # batch_fn omits the key

    b = CoalescingBatcher(batch_fn, window_seconds=0.05)
    assert await b.load("gone") is None


async def test_error_propagates_to_all_waiters():
    async def batch_fn(keys):
        raise RuntimeError("boom")

    b = CoalescingBatcher(batch_fn, window_seconds=0.05)
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.gather(b.load(1), b.load(2))


async def test_max_batch_flushes_early():
    calls: list[list[int]] = []

    async def batch_fn(keys):
        calls.append(list(keys))
        return {k: k for k in keys}

    # max_batch=2 → 3 concurrent loads flush as 2 + 1.
    b = CoalescingBatcher(batch_fn, max_batch=2, window_seconds=0.05)
    results = await asyncio.gather(b.load(1), b.load(2), b.load(3))

    assert sorted(results) == [1, 2, 3]
    assert len(calls) == 2
    assert sorted(len(c) for c in calls) == [1, 2]
