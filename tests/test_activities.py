"""
Unit tests for signal activities. HTTP calls are mocked with respx.
Each test uses ActivityEnvironment to provide the Temporal activity context.
"""
import json
import pytest
import respx
import httpx
from temporalio.testing import ActivityEnvironment

from activities.pypi_metadata import fetch as pypi_fetch
from activities.osv import check as osv_check
from activities.release_age import check as release_age_check
from activities.maintainer import history as maintainer_history


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

PYPI_BASE = "https://pypi.org/pypi"
PYPISTATS_BASE = "https://pypistats.org/api/packages"
OSV_URL = "https://api.osv.dev/v1/query"


def _pypi_response(package: str, version: str, upload_time: str = "2025-01-01T00:00:00Z") -> dict:
    return {
        "info": {
            "name": package,
            "version": version,
            "author": "Test Author",
            "author_email": "test@example.com",
            "maintainer": "",
            "maintainer_email": "",
        },
        "urls": [{"upload_time_iso_8601": upload_time, "upload_time": upload_time.rstrip("Z")}],
    }


# ---------------------------------------------------------------------------
# pypi_metadata
# ---------------------------------------------------------------------------

@respx.mock
async def test_pypi_metadata_fetch_success():
    respx.get(f"{PYPI_BASE}/requests/2.32.0/json").mock(
        return_value=httpx.Response(200, json=_pypi_response("requests", "2.32.0"))
    )
    respx.get(f"{PYPISTATS_BASE}/requests/recent").mock(
        return_value=httpx.Response(200, json={"data": {"last_week": 50_000_000}})
    )

    env = ActivityEnvironment()
    result = await env.run(pypi_fetch, "pip", "requests", "2.31.0", "2.32.0")

    assert result.weekly_downloads == 50_000_000
    assert result.is_major_bump is False
    assert result.publish_account_age_days is None


@respx.mock
async def test_pypi_metadata_major_bump():
    respx.get(f"{PYPI_BASE}/django/5.0.0/json").mock(
        return_value=httpx.Response(200, json=_pypi_response("django", "5.0.0"))
    )
    respx.get(f"{PYPISTATS_BASE}/django/recent").mock(
        return_value=httpx.Response(200, json={"data": {"last_week": 1_000_000}})
    )

    env = ActivityEnvironment()
    result = await env.run(pypi_fetch, "pip", "django", "4.2.0", "5.0.0")
    assert result.is_major_bump is True


@respx.mock
async def test_pypi_metadata_404_raises_non_retryable():
    from temporalio.exceptions import ApplicationError
    respx.get(f"{PYPI_BASE}/nonexistent/1.0.0/json").mock(
        return_value=httpx.Response(404)
    )

    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(pypi_fetch, "pip", "nonexistent", "0.9.0", "1.0.0")
    assert exc_info.value.non_retryable is True


@respx.mock
async def test_pypi_metadata_pypistats_failure_returns_none():
    respx.get(f"{PYPI_BASE}/requests/2.32.0/json").mock(
        return_value=httpx.Response(200, json=_pypi_response("requests", "2.32.0"))
    )
    respx.get(f"{PYPISTATS_BASE}/requests/recent").mock(
        return_value=httpx.Response(500)
    )

    env = ActivityEnvironment()
    result = await env.run(pypi_fetch, "pip", "requests", "2.31.0", "2.32.0")
    assert result.weekly_downloads is None


# ---------------------------------------------------------------------------
# osv
# ---------------------------------------------------------------------------

@respx.mock
async def test_osv_no_vulns():
    respx.post(OSV_URL).mock(
        return_value=httpx.Response(200, json={"vulns": []})
    )

    env = ActivityEnvironment()
    result = await env.run(osv_check, "pip", "requests", "2.31.0", "2.32.0")
    assert result.osv_vulnerabilities == []


@respx.mock
async def test_osv_with_cves():
    respx.post(OSV_URL).mock(
        return_value=httpx.Response(200, json={
            "vulns": [
                {"id": "GHSA-xxxx-yyyy-zzzz", "aliases": ["CVE-2024-12345"]},
                {"id": "GHSA-aaaa-bbbb-cccc", "aliases": []},
            ]
        })
    )

    env = ActivityEnvironment()
    result = await env.run(osv_check, "pip", "badpkg", "1.0.0", "1.0.1")
    assert "CVE-2024-12345" in result.osv_vulnerabilities
    assert "GHSA-aaaa-bbbb-cccc" in result.osv_vulnerabilities


@respx.mock
async def test_osv_passes_correct_ecosystem():
    route = respx.post(OSV_URL).mock(
        return_value=httpx.Response(200, json={})
    )

    env = ActivityEnvironment()
    await env.run(osv_check, "pip", "requests", "2.31.0", "2.32.0")

    body = json.loads(route.calls[0].request.content)
    assert body["package"]["ecosystem"] == "PyPI"


# ---------------------------------------------------------------------------
# release_age
# ---------------------------------------------------------------------------

@respx.mock
async def test_release_age_recent():
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    respx.get(f"{PYPI_BASE}/requests/2.32.0/json").mock(
        return_value=httpx.Response(200, json=_pypi_response("requests", "2.32.0", recent))
    )

    env = ActivityEnvironment()
    result = await env.run(release_age_check, "pip", "requests", "2.31.0", "2.32.0")
    assert 11.0 < result.release_age_hours < 13.0


@respx.mock
async def test_release_age_old():
    respx.get(f"{PYPI_BASE}/requests/2.32.0/json").mock(
        return_value=httpx.Response(200, json=_pypi_response("requests", "2.32.0", "2024-01-01T00:00:00Z"))
    )

    env = ActivityEnvironment()
    result = await env.run(release_age_check, "pip", "requests", "2.31.0", "2.32.0")
    assert result.release_age_hours > 24 * 30  # at least a month old


# ---------------------------------------------------------------------------
# maintainer
# ---------------------------------------------------------------------------

@respx.mock
async def test_maintainer_no_change():
    for version in ("2.31.0", "2.32.0"):
        respx.get(f"{PYPI_BASE}/requests/{version}/json").mock(
            return_value=httpx.Response(200, json=_pypi_response("requests", version))
        )

    env = ActivityEnvironment()
    result = await env.run(maintainer_history, "pip", "requests", "2.31.0", "2.32.0")
    assert result.maintainer_changed is False


@respx.mock
async def test_maintainer_changed():
    old = _pypi_response("pkg", "1.0.0")
    old["info"]["author"] = "original@example.com"
    new = _pypi_response("pkg", "2.0.0")
    new["info"]["author"] = "newcomer@example.com"
    new["info"]["maintainer"] = "newcomer@example.com"

    respx.get(f"{PYPI_BASE}/pkg/1.0.0/json").mock(return_value=httpx.Response(200, json=old))
    respx.get(f"{PYPI_BASE}/pkg/2.0.0/json").mock(return_value=httpx.Response(200, json=new))

    env = ActivityEnvironment()
    result = await env.run(maintainer_history, "pip", "pkg", "1.0.0", "2.0.0")
    assert result.maintainer_changed is True
