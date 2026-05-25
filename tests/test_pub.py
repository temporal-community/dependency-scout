"""Unit tests for the pub.dev (Dart/Flutter) ecosystem provider."""

from __future__ import annotations

import gzip
import io
import re
import tarfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from ecosystems.pub import PubProvider

_API = "https://pub.dev/api"
_NOW = datetime.now(timezone.utc)
_NEW_PUBLISHED = (_NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
_OLD_PUBLISHED = (_NOW - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

PACKAGE = "dio"
OLD_VER = "4.0.6"
NEW_VER = "5.3.2"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _package_response(
    description: str = "A powerful HTTP client for Dart",
    repository: str = "https://github.com/cfug/dio",
    publisher_id: str = "dart.dev",
) -> dict:
    return {
        "name": PACKAGE,
        "latest": {
            "version": NEW_VER,
            "pubspec": {
                "name": PACKAGE,
                "version": NEW_VER,
                "description": description,
                "repository": repository,
                "homepage": "",
            },
            "published": _NEW_PUBLISHED,
        },
        "versions": [
            {"version": OLD_VER, "published": _OLD_PUBLISHED},
            {"version": NEW_VER, "published": _NEW_PUBLISHED},
        ],
        "publisher": {"publisherId": publisher_id} if publisher_id else None,
    }


def _version_response(
    version: str = NEW_VER,
    published: str = _NEW_PUBLISHED,
    repository: str = "https://github.com/cfug/dio",
    archive_sha256: str = "abc123deadbeef",
) -> dict:
    return {
        "version": version,
        "published": published,
        "archive_sha256": archive_sha256,
        "pubspec": {
            "name": PACKAGE,
            "version": version,
            "description": "A powerful HTTP client for Dart",
            "repository": repository,
            "homepage": "",
        },
    }


def _make_tar_gz_bytes() -> bytes:
    """Build a minimal valid .tar.gz archive in memory."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode="w") as tf:
            content = b"void main() {}"
            info = tarfile.TarInfo(name="lib/main.dart")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        gz.write(inner.getvalue())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# fetch_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_metadata_success():
    respx.get(f"{_API}/packages/{PACKAGE}").mock(
        return_value=httpx.Response(200, json=_package_response())
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_metadata, PACKAGE, OLD_VER, NEW_VER)

    assert result.weekly_downloads is None  # pub.dev has no public download stats
    assert result.is_major_bump is True  # 4 → 5
    assert result.package_description == "A powerful HTTP client for Dart"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_metadata_minor_bump():
    respx.get(f"{_API}/packages/{PACKAGE}").mock(
        return_value=httpx.Response(200, json=_package_response())
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_metadata, PACKAGE, "5.3.0", "5.3.2")

    assert result.is_major_bump is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_metadata_404_raises_non_retryable():
    respx.get(f"{_API}/packages/{PACKAGE}").mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()

    with pytest.raises(ApplicationError) as exc_info:
        await env.run(PubProvider().fetch_metadata, PACKAGE, OLD_VER, NEW_VER)

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "PackageNotFound"


# ---------------------------------------------------------------------------
# fetch_release_age
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_release_age_success():
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_version_response())
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_release_age, PACKAGE, NEW_VER)

    assert result.release_age_hours is not None
    assert 40 < result.release_age_hours < 56  # ~48h


@pytest.mark.asyncio
@respx.mock
async def test_fetch_release_age_404_returns_empty():
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_release_age, PACKAGE, NEW_VER)

    assert result.release_age_hours is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_release_age_missing_published_field():
    data = _version_response()
    data["published"] = ""
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_release_age, PACKAGE, NEW_VER)

    assert result.release_age_hours is None


# ---------------------------------------------------------------------------
# fetch_maintainer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_maintainer_returns_no_change():
    """pub.dev doesn't expose per-version publisher changes — always returns no change."""
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_maintainer, PACKAGE, OLD_VER, NEW_VER)

    assert result.maintainer_changed is False


# ---------------------------------------------------------------------------
# get_archive_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_archive_url_with_sha256():
    sha = "abc123deadbeef"
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_version_response(archive_sha256=sha))
    )
    provider = PubProvider()
    async with httpx.AsyncClient() as client:
        result = await provider.get_archive_url(client, PACKAGE, NEW_VER)

    assert result is not None
    url, filename, checksum = result
    assert url == f"https://pub.dev/packages/{PACKAGE}/versions/{NEW_VER}.tar.gz"
    assert filename == f"{PACKAGE}-{NEW_VER}.tar.gz"
    assert checksum == sha


@pytest.mark.asyncio
@respx.mock
async def test_get_archive_url_404_raises_non_retryable():
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )
    provider = PubProvider()

    with pytest.raises(ApplicationError) as exc_info:
        async with httpx.AsyncClient() as client:
            await provider.get_archive_url(client, PACKAGE, NEW_VER)

    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_archive_tar_gz(tmp_path):
    provider = PubProvider()
    archive_bytes = _make_tar_gz_bytes()
    provider.extract_archive(archive_bytes, f"{PACKAGE}-{NEW_VER}.tar.gz", str(tmp_path))
    assert (tmp_path / "lib" / "main.dart").exists()


# ---------------------------------------------------------------------------
# fetch_attestations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_attestations_no_github_has_attestation_false():
    """No GitHub repo → has_attestation=False, no account age."""
    data = _version_response(repository="")
    data["pubspec"]["homepage"] = ""
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_attestations, PACKAGE, OLD_VER, NEW_VER)

    assert result.has_attestation is False
    assert result.publisher_account_age_days is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_attestations_non_200_returns_false():
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(500)
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_attestations, PACKAGE, OLD_VER, NEW_VER)

    assert result.has_attestation is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_attestations_with_github_repo_fetches_account_age(monkeypatch):
    """When a GitHub repo is present, publisher_account_age_days is populated."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_version_response())
    )
    respx.get("https://api.github.com/users/cfug").mock(
        return_value=httpx.Response(
            200,
            json={"login": "cfug", "created_at": "2020-01-01T00:00:00Z"},
        )
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_attestations, PACKAGE, OLD_VER, NEW_VER)

    assert result.has_attestation is False  # pub.dev has no Sigstore
    assert result.publisher_account_age_days is not None
    assert result.publisher_account_age_days > 0


# ---------------------------------------------------------------------------
# fetch_release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_release_no_github_returns_empty():
    data = _version_response(repository="")
    data["pubspec"]["homepage"] = ""
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_release, PACKAGE, OLD_VER, NEW_VER)

    assert result.github_release_exists is False
    assert result.metadata_repo is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_release_non_200_returns_empty():
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(500)
    )
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_release, PACKAGE, OLD_VER, NEW_VER)

    assert result.github_release_exists is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_release_with_github_repo(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_version_response())
    )
    # GitHub API calls all return 404 (no release, no tag signatures)
    respx.get(re.compile(r"https://api\.github\.com/.*")).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_release, PACKAGE, OLD_VER, NEW_VER)

    assert result.metadata_repo == "cfug/dio"
    assert result.github_release_exists is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_release_with_github_release(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    respx.get(f"{_API}/packages/{PACKAGE}/versions/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_version_response())
    )
    respx.get(f"https://api.github.com/repos/cfug/dio/releases/tags/v{NEW_VER}").mock(
        return_value=httpx.Response(
            200,
            json={
                "tag_name": f"v{NEW_VER}",
                "author": {"login": "cfug-bot"},
                "created_at": _NEW_PUBLISHED,
                "published_at": _NEW_PUBLISHED,
                "body": "Bug fix release",
            },
        )
    )
    # Tag signature checks
    respx.get(re.compile(r"https://api\.github\.com/repos/cfug/dio/git/refs/tags/.*")).mock(
        return_value=httpx.Response(404)
    )
    # CI workflow changes
    respx.get(re.compile(r"https://api\.github\.com/repos/cfug/dio/commits.*")).mock(
        return_value=httpx.Response(200, json=[])
    )

    env = ActivityEnvironment()
    result = await env.run(PubProvider().fetch_release, PACKAGE, OLD_VER, NEW_VER)

    assert result.github_release_exists is True
    assert result.metadata_repo == "cfug/dio"
    assert result.release_body == "Bug fix release"


# ---------------------------------------------------------------------------
# Ecosystem registration
# ---------------------------------------------------------------------------


def test_pub_provider_name_re_accepts_valid_names():
    name_re = PubProvider.name_re
    assert name_re.match("flutter")
    assert name_re.match("dio")
    assert name_re.match("http")
    assert name_re.match("provider")
    assert name_re.match("my_package_123")
    assert not name_re.match("MyPackage")  # uppercase not allowed
    assert not name_re.match("1package")  # must start with letter
    assert not name_re.match("bad-hyphen")  # hyphens not allowed
    assert not name_re.match("bad.dot")  # dots not allowed
