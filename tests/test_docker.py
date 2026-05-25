"""Unit tests for the Docker Hub ecosystem provider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from ecosystems.docker import DockerProvider, _parse_image

_HUB = "https://hub.docker.com/v2/repositories"
_NOW = datetime.now(timezone.utc)
_NEW_TS = (_NOW - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

PACKAGE = "hashicorp/terraform"
BARE_PACKAGE = "nginx"
OLD_VER = "1.5.0"
NEW_VER = "1.6.0"


# ---------------------------------------------------------------------------
# _parse_image helper
# ---------------------------------------------------------------------------


def test_parse_image_with_namespace():
    ns, repo = _parse_image("hashicorp/terraform")
    assert ns == "hashicorp"
    assert repo == "terraform"


def test_parse_image_bare_name():
    ns, repo = _parse_image("nginx")
    assert ns == "library"
    assert repo == "nginx"


def test_parse_image_library_prefix():
    ns, repo = _parse_image("library/nginx")
    assert ns == "library"
    assert repo == "nginx"


# ---------------------------------------------------------------------------
# fetch_metadata
# ---------------------------------------------------------------------------


@respx.mock
async def test_metadata_success_with_namespace():
    respx.get(f"{_HUB}/hashicorp/terraform/").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "terraform",
                "description": "Terraform enables you to safely and predictably create infrastructure",
                "pull_count": 1_000_000,
                "last_updated": _NEW_TS,
                "is_official": False,
                "star_count": 500,
            },
        )
    )
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE, OLD_VER, NEW_VER)
    assert result.weekly_downloads is None  # Docker Hub only has total pull count
    assert result.is_major_bump is False
    assert result.package_description is not None
    assert "infrastructure" in result.package_description


@respx.mock
async def test_metadata_success_bare_image_normalises_to_library():
    """Bare image names like 'nginx' should be routed to library/nginx on Docker Hub."""
    respx.get(f"{_HUB}/library/nginx/").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "nginx",
                "description": "Official build of Nginx.",
                "pull_count": 5_000_000,
                "last_updated": _NEW_TS,
                "is_official": True,
                "star_count": 1000,
            },
        )
    )
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_metadata, BARE_PACKAGE, "1.24", "1.25")
    assert result.weekly_downloads is None
    assert result.package_description == "Official build of Nginx."


@respx.mock
async def test_metadata_404_raises_non_retryable():
    respx.get(f"{_HUB}/hashicorp/terraform/").mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    provider = DockerProvider()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(provider.fetch_metadata, PACKAGE, OLD_VER, NEW_VER)
    assert exc_info.value.non_retryable is True


@respx.mock
async def test_metadata_major_bump_detected():
    respx.get(f"{_HUB}/hashicorp/terraform/").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "terraform",
                "description": "Infrastructure as code",
                "pull_count": 1_000_000,
                "last_updated": _NEW_TS,
                "is_official": False,
                "star_count": 500,
            },
        )
    )
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE, "1.0.0", "2.0.0")
    assert result.is_major_bump is True


# ---------------------------------------------------------------------------
# fetch_release_age
# ---------------------------------------------------------------------------


@respx.mock
async def test_release_age_success():
    respx.get(f"{_HUB}/hashicorp/terraform/tags/{NEW_VER}").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": NEW_VER,
                "last_updated": _NEW_TS,
                "full_size": 123456,
                "images": [],
            },
        )
    )
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE, NEW_VER)
    assert result.release_age_hours is not None
    assert 28 < result.release_age_hours < 44  # ~36h


@respx.mock
async def test_release_age_404_returns_empty():
    respx.get(f"{_HUB}/hashicorp/terraform/tags/{NEW_VER}").mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE, NEW_VER)
    assert result.release_age_hours is None


# ---------------------------------------------------------------------------
# fetch_maintainer
# ---------------------------------------------------------------------------


async def test_fetch_maintainer_returns_empty():
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_maintainer, PACKAGE, OLD_VER, NEW_VER)
    assert result.maintainer_changed is False


# ---------------------------------------------------------------------------
# get_archive_url
# ---------------------------------------------------------------------------


async def test_get_archive_url_returns_none():
    provider = DockerProvider()
    async with httpx.AsyncClient() as client:
        result = await provider.get_archive_url(client, PACKAGE, NEW_VER)
    assert result is None


# ---------------------------------------------------------------------------
# fetch_attestations
# ---------------------------------------------------------------------------


async def test_fetch_attestations_returns_empty():
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_attestations, PACKAGE, OLD_VER, NEW_VER)
    assert result.has_attestation is False


# ---------------------------------------------------------------------------
# fetch_release
# ---------------------------------------------------------------------------


async def test_fetch_release_returns_empty():
    env = ActivityEnvironment()
    provider = DockerProvider()
    result = await env.run(provider.fetch_release, PACKAGE, OLD_VER, NEW_VER)
    assert result.github_release_exists is False
