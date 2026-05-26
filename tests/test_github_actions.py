"""Tests for the GitHub Actions ecosystem provider."""

from __future__ import annotations

import pytest
import respx
import httpx
from temporalio.testing import ActivityEnvironment

from checks import metadata, release_age, maintainer, attestation, release_notes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ENV = ActivityEnvironment()
PACKAGE = "actions/checkout"
OLD = "4"
NEW = "6"


def _gh_repo_resp(description: str = "Action's checkout", archived: bool = False) -> dict:
    return {"description": description, "archived": archived, "full_name": PACKAGE}


def _gh_release_resp(tag: str = "v6") -> dict:
    return {
        "tag_name": tag,
        "published_at": "2024-01-15T10:00:00Z",
        "created_at": "2024-01-15T09:55:00Z",
        "body": "Changelog for v6",
        "author": {"login": "github-actions[bot]"},
    }


def _commits_resp(date: str = "2024-01-15T10:00:00Z") -> list:
    return [{"commit": {"committer": {"date": date}}}]


# ---------------------------------------------------------------------------
# fetch_metadata
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_metadata_success():
    respx.get("https://api.github.com/repos/actions/checkout").mock(
        return_value=httpx.Response(200, json=_gh_repo_resp("Checkout action"))
    )
    result = await ENV.run(metadata.fetch, "github_actions", PACKAGE, OLD, NEW)
    assert result.package_description == "Checkout action"
    assert result.weekly_downloads is None
    assert isinstance(result.is_major_bump, bool)


@respx.mock
async def test_fetch_metadata_404():
    respx.get("https://api.github.com/repos/actions/checkout").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="PackageNotFound"):
        await ENV.run(metadata.fetch, "github_actions", PACKAGE, OLD, NEW)


@respx.mock
async def test_fetch_metadata_major_bump():
    respx.get("https://api.github.com/repos/actions/checkout").mock(
        return_value=httpx.Response(200, json=_gh_repo_resp())
    )
    result = await ENV.run(metadata.fetch, "github_actions", PACKAGE, "4", "5")
    assert result.is_major_bump is True


@respx.mock
async def test_fetch_metadata_non_major_bump():
    respx.get("https://api.github.com/repos/actions/checkout").mock(
        return_value=httpx.Response(200, json=_gh_repo_resp())
    )
    result = await ENV.run(metadata.fetch, "github_actions", PACKAGE, "4", "4")
    assert result.is_major_bump is False


# ---------------------------------------------------------------------------
# fetch_release_age
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_age_from_github_release():
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/v6").mock(
        return_value=httpx.Response(200, json=_gh_release_resp("v6"))
    )
    result = await ENV.run(release_age.check, "github_actions", PACKAGE, OLD, NEW)
    assert result.release_age_hours is not None
    assert result.release_age_hours > 0


@respx.mock
async def test_fetch_release_age_fallback_to_commits():
    # No release found for either v6 or 6
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/v6").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/6").mock(
        return_value=httpx.Response(404)
    )
    # Fallback: commits on the tag
    respx.get("https://api.github.com/repos/actions/checkout/commits").mock(
        return_value=httpx.Response(200, json=_commits_resp("2024-01-10T08:00:00Z"))
    )
    result = await ENV.run(release_age.check, "github_actions", PACKAGE, OLD, NEW)
    assert result.release_age_hours is not None


@respx.mock
async def test_fetch_release_age_no_data():
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/v6").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/6").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/repos/actions/checkout/commits").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await ENV.run(release_age.check, "github_actions", PACKAGE, OLD, NEW)
    assert result.release_age_hours is None


# ---------------------------------------------------------------------------
# fetch_maintainer
# ---------------------------------------------------------------------------


async def test_fetch_maintainer_returns_default():
    result = await ENV.run(maintainer.history, "github_actions", PACKAGE, OLD, NEW)
    assert result.maintainer_changed is False
    assert result.new_maintainer_account_age_days is None


# ---------------------------------------------------------------------------
# fetch_attestations
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_attestations_unsigned_tag(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    # Lightweight tag (no annotated tag object)
    for tag in ("v6", "6", "v4", "4"):
        respx.get(f"https://api.github.com/repos/actions/checkout/git/refs/tags/{tag}").mock(
            return_value=httpx.Response(
                200, json={"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": "abc"}}
            )
        )
    respx.get("https://api.github.com/users/actions").mock(
        return_value=httpx.Response(200, json={"created_at": "2020-01-01T00:00:00Z"})
    )
    result = await ENV.run(attestation.check, "github_actions", PACKAGE, OLD, NEW)
    assert result.has_attestation is False
    assert result.publisher_account_age_days is not None
    assert result.publisher_account_age_days > 0


@respx.mock
async def test_fetch_attestations_signed_new_tag():
    # Annotated tag for new version
    respx.get("https://api.github.com/repos/actions/checkout/git/refs/tags/v6").mock(
        return_value=httpx.Response(
            200, json={"ref": "refs/tags/v6", "object": {"type": "tag", "sha": "tagsha6"}}
        )
    )
    respx.get("https://api.github.com/repos/actions/checkout/git/tags/tagsha6").mock(
        return_value=httpx.Response(200, json={"verification": {"verified": True}})
    )
    # Old tag unsigned
    respx.get("https://api.github.com/repos/actions/checkout/git/refs/tags/v4").mock(
        return_value=httpx.Response(
            200, json={"ref": "refs/tags/v4", "object": {"type": "commit", "sha": "abc"}}
        )
    )
    respx.get("https://api.github.com/repos/actions/checkout/git/refs/tags/6").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/repos/actions/checkout/git/refs/tags/4").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/users/actions").mock(
        return_value=httpx.Response(200, json={"created_at": "2018-05-01T00:00:00Z"})
    )
    result = await ENV.run(attestation.check, "github_actions", PACKAGE, OLD, NEW)
    assert result.has_attestation is True
    assert result.publisher_repo == "actions/checkout"


# ---------------------------------------------------------------------------
# fetch_release (release_notes check)
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_release_with_release_notes(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/v6").mock(
        return_value=httpx.Response(200, json=_gh_release_resp("v6"))
    )
    # tag sigs
    for tag in ("v6", "6", "v4", "4"):
        respx.get(f"https://api.github.com/repos/actions/checkout/git/refs/tags/{tag}").mock(
            return_value=httpx.Response(
                200, json={"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": "abc"}}
            )
        )
    # CI workflow changes
    respx.get("https://api.github.com/repos/actions/checkout/commits").mock(
        return_value=httpx.Response(
            200, json=[{"commit": {"committer": {"date": "2024-01-14T10:00:00Z"}}}]
        )
    )
    result = await ENV.run(release_notes.check, "github_actions", PACKAGE, OLD, NEW)
    assert result.github_release_exists is True
    assert result.ci_workflow_changed_days_ago is not None


@respx.mock
async def test_fetch_release_no_release():
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/v6").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/repos/actions/checkout/releases/tags/6").mock(
        return_value=httpx.Response(404)
    )
    for tag in ("v6", "6", "v4", "4"):
        respx.get(f"https://api.github.com/repos/actions/checkout/git/refs/tags/{tag}").mock(
            return_value=httpx.Response(404)
        )
    respx.get("https://api.github.com/repos/actions/checkout/commits").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await ENV.run(release_notes.check, "github_actions", PACKAGE, OLD, NEW)
    assert result.github_release_exists is False


# ---------------------------------------------------------------------------
# Invalid package name
# ---------------------------------------------------------------------------


async def test_invalid_package_name_raises():
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="PackageNotFound"):
        await ENV.run(metadata.fetch, "github_actions", "not-a-valid-action", OLD, NEW)
