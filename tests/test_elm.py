"""
Unit tests for the Elm ecosystem provider.
HTTP calls mocked with respx; Temporal context via ActivityEnvironment.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import time

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from ecosystems.elm import ElmProvider

_ELM_API = "https://package.elm-lang.org"
_CODELOAD = "https://codeload.github.com"

PACKAGE = "mdgriffith/elm-ui"
OLD_VER = "1.0.0"
NEW_VER = "1.1.3"
AUTHOR = "mdgriffith"
REPO = "elm-ui"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _releases(versions: dict[str, int] | None = None) -> dict:
    """Return a minimal releases.json dict."""
    if versions is None:
        # Use Unix ms timestamps
        now_ms = int(time.time() * 1000)
        versions = {
            OLD_VER: now_ms - 180 * 24 * 3600 * 1000,
            NEW_VER: now_ms - 48 * 3600 * 1000,
        }
    return versions


def _make_tgz_bytes() -> bytes:
    """Build a minimal valid .tar.gz in memory."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode="w") as tf:
            content = b"module Main exposing (..)\n"
            info = tarfile.TarInfo(name=f"{REPO}-{NEW_VER}/src/Main.elm")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        gz.write(inner.getvalue())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# fetch_metadata — success
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_success():
    respx.get(f"{_ELM_API}/packages/{AUTHOR}/{REPO}/releases.json").mock(
        return_value=httpx.Response(200, json=_releases())
    )
    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_metadata, PACKAGE, OLD_VER, NEW_VER)
    assert result.is_major_bump is False
    assert result.weekly_downloads is None
    assert result.package_description is None


@respx.mock
async def test_fetch_metadata_major_bump():
    respx.get(f"{_ELM_API}/packages/{AUTHOR}/{REPO}/releases.json").mock(
        return_value=httpx.Response(200, json=_releases())
    )
    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_metadata, PACKAGE, "1.0.0", "2.0.0")
    assert result.is_major_bump is True


# ---------------------------------------------------------------------------
# fetch_metadata — 404
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_404():
    respx.get(f"{_ELM_API}/packages/{AUTHOR}/{REPO}/releases.json").mock(
        return_value=httpx.Response(404)
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(ElmProvider().fetch_metadata, PACKAGE, OLD_VER, NEW_VER)
    assert exc_info.value.non_retryable is True
    assert "PackageNotFound" in str(exc_info.value)


# ---------------------------------------------------------------------------
# fetch_release_age — success
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_age_success():
    hours_ago = 48.0
    timestamp_ms = int((time.time() - hours_ago * 3600) * 1000)
    releases = {NEW_VER: timestamp_ms}
    respx.get(f"{_ELM_API}/packages/{AUTHOR}/{REPO}/releases.json").mock(
        return_value=httpx.Response(200, json=releases)
    )
    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_release_age, PACKAGE, NEW_VER)
    assert result.release_age_hours is not None
    assert 47.0 < result.release_age_hours < 49.0


# ---------------------------------------------------------------------------
# fetch_release_age — version not in releases
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_age_version_not_found():
    respx.get(f"{_ELM_API}/packages/{AUTHOR}/{REPO}/releases.json").mock(
        return_value=httpx.Response(200, json={OLD_VER: 1234567890000})
    )
    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_release_age, PACKAGE, "9.9.9")
    assert result.release_age_hours is None


@respx.mock
async def test_fetch_release_age_api_error():
    respx.get(f"{_ELM_API}/packages/{AUTHOR}/{REPO}/releases.json").mock(
        return_value=httpx.Response(503)
    )
    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_release_age, PACKAGE, NEW_VER)
    assert result.release_age_hours is None


# ---------------------------------------------------------------------------
# fetch_maintainer — calls fetch_vcs_account_age
# ---------------------------------------------------------------------------


async def test_fetch_maintainer_calls_account_age(monkeypatch):
    called_with: list = []

    async def _mock_age(platform: str, owner: str) -> int | None:
        called_with.append((platform, owner))
        return 500

    monkeypatch.setattr("ecosystems.elm.fetch_vcs_account_age", _mock_age)
    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_maintainer, PACKAGE, OLD_VER, NEW_VER)
    assert result.maintainer_changed is False
    assert called_with == [("github", AUTHOR)]


# ---------------------------------------------------------------------------
# get_archive_url — bare semver tag succeeds first try
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_archive_url_bare_tag():
    url = f"{_CODELOAD}/{AUTHOR}/{REPO}/tar.gz/refs/tags/{NEW_VER}"
    respx.head(url).mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient(timeout=15.0) as client:
        result = await ElmProvider().get_archive_url(client, PACKAGE, NEW_VER)

    assert result is not None
    got_url, fname, integrity = result
    assert NEW_VER in got_url
    assert f"v{NEW_VER}" not in got_url
    assert fname == f"{REPO}-{NEW_VER}.tar.gz"
    assert integrity == ""


@respx.mock
async def test_get_archive_url_vtag_fallback():
    bare_url = f"{_CODELOAD}/{AUTHOR}/{REPO}/tar.gz/refs/tags/{NEW_VER}"
    vtag_url = f"{_CODELOAD}/{AUTHOR}/{REPO}/tar.gz/refs/tags/v{NEW_VER}"
    respx.head(bare_url).mock(return_value=httpx.Response(404))
    respx.head(vtag_url).mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient(timeout=15.0) as client:
        result = await ElmProvider().get_archive_url(client, PACKAGE, NEW_VER)

    assert result is not None
    got_url, _, _ = result
    assert f"v{NEW_VER}" in got_url


@respx.mock
async def test_get_archive_url_both_tags_404():
    respx.head(f"{_CODELOAD}/{AUTHOR}/{REPO}/tar.gz/refs/tags/{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )
    respx.head(f"{_CODELOAD}/{AUTHOR}/{REPO}/tar.gz/refs/tags/v{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        result = await ElmProvider().get_archive_url(client, PACKAGE, NEW_VER)

    assert result is None


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_archive():
    import tempfile
    from pathlib import Path

    tgz_bytes = _make_tgz_bytes()
    with tempfile.TemporaryDirectory() as dest:
        ElmProvider().extract_archive(tgz_bytes, f"{REPO}-{NEW_VER}.tar.gz", dest)
        assert (Path(dest) / f"{REPO}-{NEW_VER}" / "src" / "Main.elm").exists()


# ---------------------------------------------------------------------------
# fetch_attestations — signed tag
# ---------------------------------------------------------------------------


async def test_fetch_attestations_signed_tag(monkeypatch):
    async def _mock_tag_sig(platform, owner, repo, version, token):
        return True  # verified signature

    async def _mock_age(platform, owner):
        return 1000

    monkeypatch.setattr("ecosystems.elm.fetch_vcs_tag_signature", _mock_tag_sig)
    monkeypatch.setattr("ecosystems.elm.fetch_vcs_account_age", _mock_age)

    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_attestations, PACKAGE, OLD_VER, NEW_VER)
    assert result.has_attestation is True
    assert result.publisher_repo == PACKAGE
    assert result.publisher_account_age_days == 1000


async def test_fetch_attestations_unsigned_tag(monkeypatch):
    async def _mock_tag_sig(platform, owner, repo, version, token):
        return None  # no annotated tag

    async def _mock_age(platform, owner):
        return 200

    monkeypatch.setattr("ecosystems.elm.fetch_vcs_tag_signature", _mock_tag_sig)
    monkeypatch.setattr("ecosystems.elm.fetch_vcs_account_age", _mock_age)

    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_attestations, PACKAGE, OLD_VER, NEW_VER)
    assert result.has_attestation is False
    assert result.publisher_repo is None


# ---------------------------------------------------------------------------
# fetch_release — with CI signal
# ---------------------------------------------------------------------------


async def test_fetch_release_with_ci_signal(monkeypatch):
    async def _mock_vcs_release(platform, owner, repo, version, token):
        return {
            "created_at": "2024-06-01T12:00:00+00:00",
            "published_at": "2024-06-01T12:00:00+00:00",
            "body": "Release notes here",
            "author": {"login": "mdgriffith"},
        }

    async def _mock_tag_sig(platform, owner, repo, version, token):
        return True

    async def _mock_ci(platform, owner, repo):
        return 3  # CI changed 3 days ago

    monkeypatch.setattr("ecosystems.elm.fetch_vcs_release", _mock_vcs_release)
    monkeypatch.setattr("ecosystems.elm.fetch_vcs_tag_signature", _mock_tag_sig)
    monkeypatch.setattr("ecosystems.elm.fetch_vcs_ci_workflow_changes", _mock_ci)

    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_release, PACKAGE, OLD_VER, NEW_VER)
    assert result.metadata_repo == PACKAGE
    assert result.github_release_exists is True
    assert result.ci_workflow_changed_days_ago == 3
    assert result.tag_signature_verified is True


async def test_fetch_release_no_github_release(monkeypatch):
    async def _mock_vcs_release(platform, owner, repo, version, token):
        return None

    async def _mock_tag_sig(platform, owner, repo, version, token):
        return None

    async def _mock_ci(platform, owner, repo):
        return None

    monkeypatch.setattr("ecosystems.elm.fetch_vcs_release", _mock_vcs_release)
    monkeypatch.setattr("ecosystems.elm.fetch_vcs_tag_signature", _mock_tag_sig)
    monkeypatch.setattr("ecosystems.elm.fetch_vcs_ci_workflow_changes", _mock_ci)

    env = ActivityEnvironment()
    result = await env.run(ElmProvider().fetch_release, PACKAGE, OLD_VER, NEW_VER)
    assert result.metadata_repo == PACKAGE
    assert result.github_release_exists is False


# ---------------------------------------------------------------------------
# name_re validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "elm/core",
        "elm/html",
        "mdgriffith/elm-ui",
        "NoRedInk/elm-json-decode-pipeline",
        "author/package",
        "a/b",
    ],
)
def test_name_re_valid(name):
    assert ElmProvider.name_re.match(name), f"Expected {name!r} to be valid"


@pytest.mark.parametrize(
    "name",
    [
        "noslash",
        "../evil/path",
        "author/",
        "/package",
        "author/pkg/extra",
        "author/pkg with space",
    ],
)
def test_name_re_rejects_invalid(name):
    assert not ElmProvider.name_re.match(name), f"Expected {name!r} to be rejected"
