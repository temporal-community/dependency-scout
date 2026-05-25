"""Unit tests for the Terraform Registry ecosystem provider."""

from __future__ import annotations

import io
import re
import tarfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from ecosystems.terraform import TerraformProvider, _parse_package

_API = "https://registry.terraform.io/v1"
_CODELOAD = "https://codeload.github.com"
_NOW = datetime.now(timezone.utc)
_NEW_TS = (_NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
_OLD_TS = (_NOW - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")

PROVIDER_NS = "hashicorp"
PROVIDER_NAME = "aws"
PROVIDER_PKG = f"{PROVIDER_NS}/{PROVIDER_NAME}"

MODULE_NS = "hashicorp"
MODULE_NAME = "consul"
MODULE_PROVIDER = "aws"
MODULE_PKG = f"{MODULE_NS}/{MODULE_NAME}/{MODULE_PROVIDER}"

OLD_VER = "4.0.0"
NEW_VER = "5.0.0"

GITHUB_REPO = "hashicorp/terraform-provider-aws"
GITHUB_SOURCE = f"https://github.com/{GITHUB_REPO}"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _provider_info(
    source_repo: str = GITHUB_SOURCE,
    description: str = "AWS provider for Terraform",
) -> dict:
    return {
        "id": f"{PROVIDER_NS}/{PROVIDER_NAME}",
        "namespace": PROVIDER_NS,
        "name": PROVIDER_NAME,
        "source": source_repo,
        "description": description,
        "versions": [NEW_VER, OLD_VER],
        "published_at": _NEW_TS,
    }


def _provider_version_info(
    version: str = NEW_VER,
    source_repo: str = GITHUB_SOURCE,
    description: str = "AWS provider for Terraform",
) -> dict:
    return {
        "version": version,
        "published_at": _NEW_TS,
        "source_repo": source_repo,
        "description": description,
    }


def _module_info(
    source: str = GITHUB_SOURCE,
) -> dict:
    return {
        "id": f"{MODULE_NS}/{MODULE_NAME}/{MODULE_PROVIDER}",
        "namespace": MODULE_NS,
        "name": MODULE_NAME,
        "provider": MODULE_PROVIDER,
        "source": source,
        "versions": [{"version": NEW_VER}, {"version": OLD_VER}],
        "published_at": _NEW_TS,
    }


def _module_version_info(
    version: str = NEW_VER,
    source: str = GITHUB_SOURCE,
) -> dict:
    return {
        "version": version,
        "published_at": _NEW_TS,
        "source": source,
    }


def _make_targz(filename: str = "main.tf") -> bytes:
    """Build a minimal valid .tar.gz in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        content = b'provider "aws" {}'
        info = tarfile.TarInfo(name=filename)
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _parse_package
# ---------------------------------------------------------------------------


def test_parse_provider_bare():
    assert _parse_package("hashicorp/aws") == ("hashicorp", "aws", None)


def test_parse_provider_with_registry_prefix():
    assert _parse_package("registry.terraform.io/hashicorp/aws") == ("hashicorp", "aws", None)


def test_parse_module():
    assert _parse_package("hashicorp/consul/aws") == ("hashicorp", "consul", "aws")


def test_parse_invalid_raises():
    with pytest.raises(ApplicationError) as exc_info:
        _parse_package("hashicorp")
    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# fetch_metadata — provider
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_provider_success():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}").mock(
        return_value=httpx.Response(200, json=_provider_info())
    )
    env = ActivityEnvironment()
    provider = TerraformProvider()
    result = await env.run(provider.fetch_metadata, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.weekly_downloads is None
    assert result.is_major_bump is True  # 4.x → 5.x
    assert "AWS" in (result.package_description or "")


@respx.mock
async def test_fetch_metadata_provider_no_description():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}").mock(
        return_value=httpx.Response(200, json=_provider_info(description=""))
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_metadata, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.package_description is None


# ---------------------------------------------------------------------------
# fetch_metadata — module
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_module_success():
    respx.get(f"{_API}/modules/{MODULE_NS}/{MODULE_NAME}/{MODULE_PROVIDER}").mock(
        return_value=httpx.Response(200, json=_module_info())
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_metadata, MODULE_PKG, "0.9.0", "1.0.0")
    assert result.weekly_downloads is None
    assert result.is_major_bump is True


# ---------------------------------------------------------------------------
# fetch_metadata 404 → ApplicationError
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_provider_404():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}").mock(
        return_value=httpx.Response(404)
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(TerraformProvider().fetch_metadata, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert exc_info.value.non_retryable is True


@respx.mock
async def test_fetch_metadata_module_404():
    respx.get(f"{_API}/modules/{MODULE_NS}/{MODULE_NAME}/{MODULE_PROVIDER}").mock(
        return_value=httpx.Response(404)
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(TerraformProvider().fetch_metadata, MODULE_PKG, OLD_VER, NEW_VER)
    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# fetch_release_age — provider
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_age_provider_success():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_provider_version_info())
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_release_age, PROVIDER_PKG, NEW_VER)
    assert result.release_age_hours is not None
    assert 40 < result.release_age_hours < 56  # ~48h


@respx.mock
async def test_fetch_release_age_provider_404_raises():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(TerraformProvider().fetch_release_age, PROVIDER_PKG, NEW_VER)
    assert exc_info.value.non_retryable is True


@respx.mock
async def test_fetch_release_age_missing_published_at():
    data = _provider_version_info()
    data["published_at"] = ""
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_release_age, PROVIDER_PKG, NEW_VER)
    assert result.release_age_hours is None


# ---------------------------------------------------------------------------
# fetch_maintainer
# ---------------------------------------------------------------------------


async def test_fetch_maintainer_always_empty():
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_maintainer, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.maintainer_changed is False


# ---------------------------------------------------------------------------
# get_archive_url — with GitHub source
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_archive_url_github_source():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_provider_version_info())
    )
    owner, repo = GITHUB_REPO.split("/", 1)
    respx.head(f"{_CODELOAD}/{owner}/{repo}/tar.gz/refs/tags/v{NEW_VER}").mock(
        return_value=httpx.Response(200)
    )
    async with httpx.AsyncClient() as client:
        result = await TerraformProvider().get_archive_url(client, PROVIDER_PKG, NEW_VER)
    assert result is not None
    url, filename, checksum = result
    assert "codeload.github.com" in url
    assert f"v{NEW_VER}" in url
    assert filename == f"{PROVIDER_NAME}-{NEW_VER}.tar.gz"
    assert checksum == ""


@respx.mock
async def test_get_archive_url_bare_tag_fallback():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=_provider_version_info())
    )
    owner, repo = GITHUB_REPO.split("/", 1)
    # v-prefix 404, bare tag 200
    respx.head(f"{_CODELOAD}/{owner}/{repo}/tar.gz/refs/tags/v{NEW_VER}").mock(
        return_value=httpx.Response(404)
    )
    respx.head(f"{_CODELOAD}/{owner}/{repo}/tar.gz/refs/tags/{NEW_VER}").mock(
        return_value=httpx.Response(200)
    )
    async with httpx.AsyncClient() as client:
        result = await TerraformProvider().get_archive_url(client, PROVIDER_PKG, NEW_VER)
    assert result is not None
    url, _, _ = result
    assert f"/{NEW_VER}" in url
    assert f"/v{NEW_VER}" not in url


# ---------------------------------------------------------------------------
# get_archive_url — no source → None
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_archive_url_no_source():
    data = _provider_version_info(source_repo="")
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        result = await TerraformProvider().get_archive_url(client, PROVIDER_PKG, NEW_VER)
    assert result is None


@respx.mock
async def test_get_archive_url_non_github_source():
    data = _provider_version_info(source_repo="https://gitlab.com/example/provider")
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        result = await TerraformProvider().get_archive_url(client, PROVIDER_PKG, NEW_VER)
    assert result is None


@respx.mock
async def test_get_archive_url_non_200_returns_none():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(500)
    )
    async with httpx.AsyncClient() as client:
        result = await TerraformProvider().get_archive_url(client, PROVIDER_PKG, NEW_VER)
    assert result is None


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_archive(tmp_path):
    provider = TerraformProvider()
    tar_bytes = _make_targz(filename=f"{PROVIDER_NAME}-{NEW_VER}/main.tf")
    provider.extract_archive(tar_bytes, f"{PROVIDER_NAME}-{NEW_VER}.tar.gz", str(tmp_path))
    assert (tmp_path / f"{PROVIDER_NAME}-{NEW_VER}" / "main.tf").exists()


# ---------------------------------------------------------------------------
# fetch_attestations — no github
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_attestations_no_source():
    data = _provider_version_info(source_repo="")
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_attestations, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.has_attestation is False
    assert result.publisher_account_age_days is None


@respx.mock
async def test_fetch_attestations_non_200():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(503)
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_attestations, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.has_attestation is False


# ---------------------------------------------------------------------------
# fetch_attestations — with github source (tag sig = None → no attestation)
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_attestations_with_github_no_sig(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    data = _provider_version_info()
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    # tag ref 404 → no annotated tag → sig = None
    respx.get(re.compile(r"https://api\.github\.com/repos/.*/git/refs/tags/.*")).mock(
        return_value=httpx.Response(404)
    )
    # user account endpoint also 404 (no GITHUB_TOKEN)
    respx.get(re.compile(r"https://api\.github\.com/users/.*")).mock(
        return_value=httpx.Response(404)
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_attestations, PROVIDER_PKG, OLD_VER, NEW_VER)
    # new_sig is None (no annotated tag) → has_attestation is False
    assert result.has_attestation is False


# ---------------------------------------------------------------------------
# fetch_release — with github source
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_with_github_source(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    data = _provider_version_info()
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    # All GitHub API calls return 404
    respx.get(re.compile(r"https://api\.github\.com/.*")).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_release, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.metadata_repo == GITHUB_REPO


@respx.mock
async def test_fetch_release_no_source():
    data = _provider_version_info(source_repo="")
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_release, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.github_release_exists is False
    assert result.metadata_repo is None


@respx.mock
async def test_fetch_release_non_200_returns_empty():
    respx.get(f"{_API}/providers/{PROVIDER_NS}/{PROVIDER_NAME}/{NEW_VER}").mock(
        return_value=httpx.Response(500)
    )
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_release, PROVIDER_PKG, OLD_VER, NEW_VER)
    assert result.github_release_exists is False


# ---------------------------------------------------------------------------
# fetch_release — with module
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_module_with_github_source(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    data = _module_version_info()
    respx.get(f"{_API}/modules/{MODULE_NS}/{MODULE_NAME}/{MODULE_PROVIDER}/{NEW_VER}").mock(
        return_value=httpx.Response(200, json=data)
    )
    respx.get(re.compile(r"https://api\.github\.com/.*")).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    result = await env.run(TerraformProvider().fetch_release, MODULE_PKG, OLD_VER, NEW_VER)
    assert result.metadata_repo == GITHUB_REPO


# ---------------------------------------------------------------------------
# name_re
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "hashicorp/aws",
        "hashicorp/consul/aws",
        "registry.terraform.io/hashicorp/aws",
        "my-org/my-module/google",
        "a/b",
    ],
)
def test_name_re_valid(name):
    assert TerraformProvider.name_re.match(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "UPPERCASE/thing",  # uppercase not allowed per regex
        "a" * 130 + "/b",  # too long
    ],
)
def test_name_re_rejects_invalid(name):
    assert not TerraformProvider.name_re.match(name)
