"""Unit tests for the Mix (Hex.pm) ecosystem provider."""

from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from ecosystems.mix import MixProvider

_API = "https://hex.pm/api"
_CDN = "https://repo.hex.pm/tarballs"
_NOW = datetime.now(timezone.utc)
_OLD_TS = (_NOW - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
_NEW_TS = (_NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

PACKAGE = "phoenix"
OLD_VER = "1.6.0"
NEW_VER = "1.7.0"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _package_response(
    description: str = "A productive web framework",
    weekly_downloads: int = 50_000,
    github_url: str = "https://github.com/phoenixframework/phoenix",
) -> dict:
    return {
        "name": PACKAGE,
        "description": description,
        "downloads": {
            "all": 5_000_000,
            "week": weekly_downloads,
        },
        "releases": [
            {"version": NEW_VER, "inserted_at": _NEW_TS},
            {"version": OLD_VER, "inserted_at": _OLD_TS},
        ],
        "owners": [
            {"username": "chrismccord", "inserted_at": _OLD_TS},
        ],
        "links": {
            "GitHub": github_url,
        },
    }


def _release_response(version: str = NEW_VER, inserted_at: str = _NEW_TS) -> dict:
    return {
        "version": version,
        "inserted_at": inserted_at,
        "publisher": {"username": "chrismccord"},
    }


def _make_hex_tar_bytes() -> bytes:
    """Build a minimal valid Hex .tar archive in memory.

    Hex tarballs are plain tar files containing a contents.tar.gz.
    """
    # Build inner contents.tar.gz
    inner_buf = io.BytesIO()
    with tarfile.open(fileobj=inner_buf, mode="w:gz") as inner_tf:
        content = b"defmodule Phoenix do\nend\n"
        info = tarfile.TarInfo(name="lib/phoenix.ex")
        info.size = len(content)
        inner_tf.addfile(info, io.BytesIO(content))
    inner_bytes = inner_buf.getvalue()

    # Build outer plain tar containing contents.tar.gz
    outer_buf = io.BytesIO()
    with tarfile.open(fileobj=outer_buf, mode="w:") as outer_tf:
        info = tarfile.TarInfo(name="contents.tar.gz")
        info.size = len(inner_bytes)
        outer_tf.addfile(info, io.BytesIO(inner_bytes))

    return outer_buf.getvalue()


# ---------------------------------------------------------------------------
# fetch_metadata
# ---------------------------------------------------------------------------


@respx.mock
async def test_metadata_success():
    respx.get(f"{_API}/packages/{PACKAGE}").mock(
        return_value=httpx.Response(200, json=_package_response())
    )
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE, OLD_VER, NEW_VER)
    assert result.weekly_downloads == 50_000
    assert result.is_major_bump is False
    assert "web framework" in (result.package_description or "")


@respx.mock
async def test_metadata_major_bump():
    respx.get(f"{_API}/packages/{PACKAGE}").mock(
        return_value=httpx.Response(200, json=_package_response())
    )
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE, "1.0.0", "2.0.0")
    assert result.is_major_bump is True


@respx.mock
async def test_metadata_404_raises_non_retryable():
    respx.get(f"{_API}/packages/{PACKAGE}").mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    provider = MixProvider()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(provider.fetch_metadata, PACKAGE, OLD_VER, NEW_VER)
    assert exc_info.value.non_retryable is True


@respx.mock
async def test_metadata_owner_prefix_normalised():
    """Package names like 'owner/phoenix' should be normalised to 'phoenix'."""
    respx.get(f"{_API}/packages/{PACKAGE}").mock(
        return_value=httpx.Response(200, json=_package_response())
    )
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_metadata, f"phoenixframework/{PACKAGE}", OLD_VER, NEW_VER)
    assert result.package_description is not None


# ---------------------------------------------------------------------------
# fetch_release_age
# ---------------------------------------------------------------------------


@respx.mock
async def test_release_age_success():
    respx.get(f"{_API}/packages/{PACKAGE}/releases/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_release_response())
    )
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE, NEW_VER)
    assert result.release_age_hours is not None
    assert 40 < result.release_age_hours < 56  # ~48h


@respx.mock
async def test_release_age_404_returns_none():
    respx.get(f"{_API}/packages/{PACKAGE}/releases/{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE, NEW_VER)
    assert result.release_age_hours is None


@respx.mock
async def test_release_age_server_error_returns_none():
    respx.get(f"{_API}/packages/{PACKAGE}/releases/{NEW_VER}").mock(
        return_value=httpx.Response(503)
    )
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE, NEW_VER)
    assert result.release_age_hours is None


# ---------------------------------------------------------------------------
# fetch_maintainer
# ---------------------------------------------------------------------------


async def test_fetch_maintainer_returns_empty():
    """Hex doesn't expose per-release owner history; always returns MaintainerChecks()."""
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_maintainer, PACKAGE, OLD_VER, NEW_VER)
    assert result.maintainer_changed is False


# ---------------------------------------------------------------------------
# get_archive_url
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_archive_url_success():
    provider = MixProvider()
    async with httpx.AsyncClient() as client:
        result = await provider.get_archive_url(client, PACKAGE, NEW_VER)
    assert result is not None
    url, filename, checksum = result
    assert url == f"{_CDN}/{PACKAGE}-{NEW_VER}.tar"
    assert filename == f"{PACKAGE}-{NEW_VER}.tar"
    assert checksum == ""


@respx.mock
async def test_get_archive_url_owner_prefix_stripped():
    provider = MixProvider()
    async with httpx.AsyncClient() as client:
        result = await provider.get_archive_url(client, f"owner/{PACKAGE}", NEW_VER)
    assert result is not None
    url, filename, _ = result
    assert "owner/" not in url
    assert filename == f"{PACKAGE}-{NEW_VER}.tar"


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_archive(tmp_path):
    provider = MixProvider()
    tar_bytes = _make_hex_tar_bytes()
    provider.extract_archive(tar_bytes, f"{PACKAGE}-{NEW_VER}.tar", str(tmp_path))
    assert (tmp_path / "lib" / "phoenix.ex").exists()


def test_extract_archive_missing_contents(tmp_path):
    """A tar without contents.tar.gz should raise a non-retryable error."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        content = b"metadata"
        info = tarfile.TarInfo(name="metadata.config")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    tar_bytes = buf.getvalue()

    provider = MixProvider()
    with pytest.raises(ApplicationError) as exc_info:
        provider.extract_archive(tar_bytes, f"{PACKAGE}-{NEW_VER}.tar", str(tmp_path))
    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# fetch_attestations
# ---------------------------------------------------------------------------


@respx.mock
async def test_attestations_no_github_link():
    """Without a GitHub link, has_attestation should be False."""
    data = _package_response(github_url="")
    data["links"] = {}
    respx.get(f"{_API}/packages/{PACKAGE}").mock(return_value=httpx.Response(200, json=data))
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_attestations, PACKAGE, OLD_VER, NEW_VER)
    assert result.has_attestation is False
    assert result.publisher_account_age_days is None


@respx.mock
async def test_attestations_with_github_link(monkeypatch):
    """With a GitHub link, has_attestation is still False (Hex has no Sigstore)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    respx.get(f"{_API}/packages/{PACKAGE}").mock(
        return_value=httpx.Response(200, json=_package_response())
    )
    # GitHub account age endpoint — no token so fetch_vcs_account_age returns None immediately
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_attestations, PACKAGE, OLD_VER, NEW_VER)
    assert result.has_attestation is False


@respx.mock
async def test_attestations_api_error():
    """An API error should return empty AttestationChecks gracefully."""
    respx.get(f"{_API}/packages/{PACKAGE}").mock(return_value=httpx.Response(503))
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_attestations, PACKAGE, OLD_VER, NEW_VER)
    assert result.has_attestation is False


# ---------------------------------------------------------------------------
# fetch_release
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_no_github_link():
    data = _package_response(github_url="")
    data["links"] = {}
    respx.get(f"{_API}/packages/{PACKAGE}").mock(return_value=httpx.Response(200, json=data))
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_release, PACKAGE, OLD_VER, NEW_VER)
    assert result.github_release_exists is False
    assert result.metadata_repo is None


@respx.mock
async def test_fetch_release_api_error():
    respx.get(f"{_API}/packages/{PACKAGE}").mock(return_value=httpx.Response(500))
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_release, PACKAGE, OLD_VER, NEW_VER)
    assert result.github_release_exists is False


@respx.mock
async def test_fetch_release_with_github_link(monkeypatch):
    import re

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    respx.get(f"{_API}/packages/{PACKAGE}").mock(
        return_value=httpx.Response(200, json=_package_response())
    )
    # GitHub API returns 404 for all calls (no release, no tags)
    respx.get(re.compile(r"https://api\.github\.com/.*")).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    provider = MixProvider()
    result = await env.run(provider.fetch_release, PACKAGE, OLD_VER, NEW_VER)
    assert result.metadata_repo == "phoenixframework/phoenix"
