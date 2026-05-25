"""Unit tests for the Swift Package Manager ecosystem provider."""

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

from ecosystems.swift import SwiftProvider, _parse_package, _tags

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GH_API = "https://api.github.com"
_CODELOAD = "https://codeload.github.com"

_NOW = datetime.now(timezone.utc)
_OLD_TS = (_NOW - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
_NEW_TS = (_NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

PACKAGE_HTTPS = "https://github.com/apple/swift-argument-parser"
PACKAGE_BARE = "github.com/apple/swift-argument-parser"
PACKAGE_GIT = "https://github.com/apple/swift-argument-parser.git"
OWNER = "apple"
REPO = "swift-argument-parser"
OLD_VER = "1.2.0"
NEW_VER = "1.3.0"


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _gh_repo(description: str = "A library for building command-line interfaces.") -> dict:
    return {
        "id": 123456,
        "name": REPO,
        "full_name": f"{OWNER}/{REPO}",
        "description": description,
        "stargazers_count": 3000,
        "forks_count": 200,
        "archived": False,
    }


def _gh_release(published_at: str = _NEW_TS) -> dict:
    return {
        "tag_name": f"v{NEW_VER}",
        "name": f"v{NEW_VER}",
        "published_at": published_at,
        "created_at": published_at,
        "body": "Bug fixes and performance improvements.",
        "author": {"login": "swift-bot"},
    }


def _make_tar_gz(name: str = REPO, version: str = NEW_VER) -> bytes:
    """Build a minimal valid .tar.gz in memory."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode="w") as tf:
            content = b"// Swift source"
            info = tarfile.TarInfo(name=f"{name}-{version}/Sources/main.swift")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        gz.write(inner.getvalue())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _parse_package helper
# ---------------------------------------------------------------------------


def test_parse_package_https_url():
    platform, owner, repo = _parse_package(PACKAGE_HTTPS)
    assert platform == "github"
    assert owner == OWNER
    assert repo == REPO


def test_parse_package_bare_format():
    platform, owner, repo = _parse_package(PACKAGE_BARE)
    assert platform == "github"
    assert owner == OWNER
    assert repo == REPO


def test_parse_package_git_suffix_stripped():
    platform, owner, repo = _parse_package(PACKAGE_GIT)
    assert platform == "github"
    assert owner == OWNER
    assert repo == REPO


def test_parse_package_gitlab_url():
    platform, owner, repo = _parse_package("https://gitlab.com/myorg/mypackage")
    assert platform == "gitlab"
    assert owner == "myorg"
    assert repo == "mypackage"


def test_parse_package_invalid_raises():
    with pytest.raises(ApplicationError) as exc_info:
        _parse_package("not-a-url-at-all")
    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# _tags helper
# ---------------------------------------------------------------------------


def test_tags_bare_version():
    assert _tags("1.3.0") == ("v1.3.0", "1.3.0")


def test_tags_already_prefixed():
    assert _tags("v1.3.0") == ("v1.3.0", "1.3.0")


# ---------------------------------------------------------------------------
# fetch_metadata — happy paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_https_url():
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(200, json=_gh_repo())
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert result.weekly_downloads is None
    assert result.is_major_bump is False
    assert result.package_description is not None
    assert "command-line" in result.package_description


@respx.mock
async def test_fetch_metadata_bare_url():
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(200, json=_gh_repo())
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE_BARE, OLD_VER, NEW_VER)
    assert result.is_major_bump is False


@respx.mock
async def test_fetch_metadata_major_bump():
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(200, json=_gh_repo())
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE_HTTPS, "1.0.0", "2.0.0")
    assert result.is_major_bump is True


@respx.mock
async def test_fetch_metadata_null_description():
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(200, json=_gh_repo(description=None))
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_metadata, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert result.package_description is None


# ---------------------------------------------------------------------------
# fetch_metadata — error paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_404_raises_non_retryable():
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}").mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    provider = SwiftProvider()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(provider.fetch_metadata, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert exc_info.value.non_retryable is True


@respx.mock
async def test_fetch_metadata_500_raises_retryable():
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}").mock(return_value=httpx.Response(500))
    env = ActivityEnvironment()
    provider = SwiftProvider()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(provider.fetch_metadata, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert exc_info.value.non_retryable is False


# ---------------------------------------------------------------------------
# fetch_release_age
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_age_from_release():
    # Formal release exists → use published_at
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/releases/tags/v{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_gh_release(_NEW_TS))
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE_HTTPS, NEW_VER)
    assert result.release_age_hours is not None
    assert 40 < result.release_age_hours < 56  # ~48 h


@respx.mock
async def test_fetch_release_age_falls_back_to_commit(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # No formal release
    respx.get(re.compile(r".*/releases/tags/.*")).mock(return_value=httpx.Response(404))
    # Tag commit fallback
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/commits").mock(
        return_value=httpx.Response(
            200,
            json=[{"commit": {"committer": {"date": _NEW_TS}}}],
        )
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE_HTTPS, NEW_VER)
    assert result.release_age_hours is not None
    assert 40 < result.release_age_hours < 56


@respx.mock
async def test_fetch_release_age_returns_empty_when_no_data(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    respx.get(re.compile(r".*/releases/tags/.*")).mock(return_value=httpx.Response(404))
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/commits").mock(
        return_value=httpx.Response(200, json=[])
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_release_age, PACKAGE_HTTPS, NEW_VER)
    assert result.release_age_hours is None


# ---------------------------------------------------------------------------
# fetch_maintainer
# ---------------------------------------------------------------------------


async def test_fetch_maintainer_always_empty():
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_maintainer, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert result.maintainer_changed is False


# ---------------------------------------------------------------------------
# get_archive_url
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_archive_url_github_v_prefixed_tag():
    expected_url = f"{_CODELOAD}/{OWNER}/{REPO}/tar.gz/refs/tags/v{NEW_VER}"
    respx.head(expected_url).mock(return_value=httpx.Response(200))
    provider = SwiftProvider()
    async with httpx.AsyncClient() as client:
        result = await provider.get_archive_url(client, PACKAGE_HTTPS, NEW_VER)
    assert result is not None
    url, filename, checksum = result
    assert url == expected_url
    assert REPO in filename
    assert checksum == ""


@respx.mock
async def test_get_archive_url_github_bare_tag_fallback():
    # v-prefixed tag returns 404, bare tag succeeds
    v_url = f"{_CODELOAD}/{OWNER}/{REPO}/tar.gz/refs/tags/v{NEW_VER}"
    bare_url = f"{_CODELOAD}/{OWNER}/{REPO}/tar.gz/refs/tags/{NEW_VER}"
    respx.head(v_url).mock(return_value=httpx.Response(404))
    respx.head(bare_url).mock(return_value=httpx.Response(200))
    provider = SwiftProvider()
    async with httpx.AsyncClient() as client:
        result = await provider.get_archive_url(client, PACKAGE_HTTPS, NEW_VER)
    assert result is not None
    url, _, _ = result
    assert url == bare_url


@respx.mock
async def test_get_archive_url_returns_none_when_not_found():
    respx.head(re.compile(r".*codeload\.github\.com.*")).mock(return_value=httpx.Response(404))
    provider = SwiftProvider()
    async with httpx.AsyncClient() as client:
        result = await provider.get_archive_url(client, PACKAGE_HTTPS, NEW_VER)
    assert result is None


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_archive(tmp_path):
    provider = SwiftProvider()
    tar_bytes = _make_tar_gz()
    provider.extract_archive(tar_bytes, f"{REPO}-{NEW_VER}.tar.gz", str(tmp_path))
    assert (tmp_path / f"{REPO}-{NEW_VER}" / "Sources" / "main.swift").exists()


# ---------------------------------------------------------------------------
# fetch_attestations
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_attestations_signed_tag(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    # Tag ref — annotated
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/git/refs/tags/v{NEW_VER}").mock(
        return_value=httpx.Response(
            200,
            json={"ref": f"refs/tags/v{NEW_VER}", "object": {"type": "tag", "sha": "abc123"}},
        )
    )
    # Annotated tag object with verified signature
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/git/tags/abc123").mock(
        return_value=httpx.Response(
            200, json={"verification": {"verified": True, "reason": "valid"}}
        )
    )
    # Old version tag — not found
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/git/refs/tags/v{OLD_VER}").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/git/refs/tags/{OLD_VER}").mock(
        return_value=httpx.Response(404)
    )
    # Account age endpoint
    respx.get(f"{_GH_API}/users/{OWNER}").mock(
        return_value=httpx.Response(
            200, json={"created_at": "2010-01-01T00:00:00Z", "login": OWNER}
        )
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_attestations, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert result.has_attestation is True
    assert result.publisher_kind == "GitHub"
    assert result.publisher_repo == f"{OWNER}/{REPO}"
    assert result.publisher_account_age_days is not None
    assert result.publisher_account_age_days > 0


@respx.mock
async def test_fetch_attestations_no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # Without a token, fetch_vcs_account_age returns None; tag sig endpoints still
    # called but return 404
    respx.get(re.compile(r".*/git/refs/tags/.*")).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_attestations, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert result.has_attestation is False
    assert result.publisher_account_age_days is None


# ---------------------------------------------------------------------------
# fetch_release
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_with_ci_signal(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    # Formal release
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/releases/tags/v{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_gh_release(_NEW_TS))
    )
    respx.get(f"{_GH_API}/repos/{OWNER}/{REPO}/releases/tags/{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )
    # Tag signatures — not signed
    respx.get(re.compile(r".*/git/refs/tags/.*")).mock(return_value=httpx.Response(404))
    # CI workflow commits — changed 3 days ago
    respx.get(
        f"{_GH_API}/repos/{OWNER}/{REPO}/commits",
        params__contains={"path": ".github/workflows"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"commit": {"committer": {"date": (_NOW - timedelta(days=3)).isoformat()}}}],
        )
    )
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_release, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert result.github_release_exists is True
    assert result.metadata_repo == f"{OWNER}/{REPO}"
    assert result.ci_workflow_changed_days_ago is not None
    assert result.ci_workflow_changed_days_ago <= 4  # ~3 days


@respx.mock
async def test_fetch_release_no_release_returns_metadata_repo(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    respx.get(re.compile(r".*/releases/tags/.*")).mock(return_value=httpx.Response(404))
    respx.get(re.compile(r".*/git/refs/tags/.*")).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    provider = SwiftProvider()
    result = await env.run(provider.fetch_release, PACKAGE_HTTPS, OLD_VER, NEW_VER)
    assert result.github_release_exists is False
    assert result.metadata_repo == f"{OWNER}/{REPO}"
