"""Tests for activities/custom_checks.py — the dependency_scout.checks plugin runner."""

from unittest.mock import MagicMock, patch

from temporalio.testing import ActivityEnvironment

from checks.custom_checks import run_all
from models import CheckContext

_CTX = CheckContext(
    package="requests",
    ecosystem="pip",
    old_version="2.28.0",
    new_version="2.31.0",
)


async def test_run_all_returns_empty_when_no_entry_points():
    """run_all returns {} when no dependency_scout.checks plugins are installed."""
    with patch("importlib.metadata.entry_points", return_value=[]):
        env = ActivityEnvironment()
        result = await env.run(run_all, _CTX)
    assert result == {}


async def test_run_all_returns_plugin_result():
    """A successful plugin function's result appears under its entry-point name."""

    async def _good_check(ctx: CheckContext) -> dict:
        return {"vuln_count": 3, "package": ctx.package}

    ep = MagicMock()
    ep.name = "internal_vuln"
    ep.load.return_value = _good_check

    with patch("importlib.metadata.entry_points", return_value=[ep]):
        env = ActivityEnvironment()
        result = await env.run(run_all, _CTX)

    assert result == {"internal_vuln": {"vuln_count": 3, "package": "requests"}}


async def test_run_all_skips_failing_plugin(caplog):
    """A plugin that raises an exception is skipped; its error is logged, not propagated."""
    import logging

    async def _bad_check(ctx: CheckContext) -> dict:
        raise RuntimeError("DB connection refused")

    ep = MagicMock()
    ep.name = "broken_check"
    ep.load.return_value = _bad_check

    with patch("importlib.metadata.entry_points", return_value=[ep]):
        with caplog.at_level(logging.WARNING):
            env = ActivityEnvironment()
            result = await env.run(run_all, _CTX)

    assert result == {}


async def test_run_all_aggregates_multiple_plugins():
    """Results from multiple plugins are merged into a single dict keyed by entry-point name."""

    async def _check_a(ctx: CheckContext) -> dict:
        return {"score": 10}

    async def _check_b(ctx: CheckContext) -> dict:
        return {"alerts": ["suspicious"]}

    ep_a = MagicMock()
    ep_a.name = "check_a"
    ep_a.load.return_value = _check_a

    ep_b = MagicMock()
    ep_b.name = "check_b"
    ep_b.load.return_value = _check_b

    with patch("importlib.metadata.entry_points", return_value=[ep_a, ep_b]):
        env = ActivityEnvironment()
        result = await env.run(run_all, _CTX)

    assert result == {"check_a": {"score": 10}, "check_b": {"alerts": ["suspicious"]}}


async def test_run_all_partial_failure_keeps_successful_results():
    """A mix of passing and failing plugins returns only the successful results."""

    async def _good(ctx: CheckContext) -> dict:
        return {"ok": True}

    async def _bad(ctx: CheckContext) -> dict:
        raise ValueError("oops")

    ep_good = MagicMock()
    ep_good.name = "good"
    ep_good.load.return_value = _good

    ep_bad = MagicMock()
    ep_bad.name = "bad"
    ep_bad.load.return_value = _bad

    with patch("importlib.metadata.entry_points", return_value=[ep_good, ep_bad]):
        env = ActivityEnvironment()
        result = await env.run(run_all, _CTX)

    assert result == {"good": {"ok": True}}
    assert "bad" not in result
