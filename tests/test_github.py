"""
Tests for activities/github.py.

All non-dry-run tests set GITHUB_TOKEN (PAT path) to bypass GitHub App auth.
HTTP calls are mocked with respx. Activities run inside ActivityEnvironment.
"""
import pytest
import respx
import httpx
from temporalio.testing import ActivityEnvironment
from temporalio.exceptions import ApplicationError

from activities.github import (
    comment,
    merge_pr,
    request_review,
    label,
    close_pr,
    get_pr,
    _dry_run,
)
from activities.models import PRContext, Verdict


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

REPO = "owner/repo"
PR_NUM = 42
INSTALL_ID = 123
BASE_URL = f"https://api.github.com/repos/{REPO}"


@pytest.fixture
def pr():
    return PRContext(
        repo=REPO,
        pr_number=PR_NUM,
        pr_author="dependabot[bot]",
        installation_id=INSTALL_ID,
        ecosystem="pip",
        package_name="requests",
        old_version="2.31.0",
        new_version="2.32.0",
        head_sha="abc123",
    )


@pytest.fixture
def verdict():
    return Verdict(
        classification="green",
        confidence=0.95,
        reasoning="Routine patch bump.",
        flags=[],
    )


@pytest.fixture
def with_pat(monkeypatch):
    """Set GITHUB_TOKEN so activities use PAT auth instead of GitHub App flow."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_pat")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)


@pytest.fixture
def dry_run(monkeypatch):
    """Remove all auth env vars to force dry-run mode."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)


def _open_pr_payload(sha: str = "abc123") -> dict:
    return {"state": "open", "head": {"sha": sha}, "mergeable": True, "mergeable_state": "clean"}


# ---------------------------------------------------------------------------
# _dry_run
# ---------------------------------------------------------------------------

def test_dry_run_true_when_no_credentials(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    assert _dry_run() is True


def test_dry_run_false_with_pat(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    assert _dry_run() is False


def test_dry_run_false_with_app_id(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    assert _dry_run() is False


# ---------------------------------------------------------------------------
# comment
# ---------------------------------------------------------------------------

@respx.mock
async def test_comment_dry_run_makes_no_http_call(pr, verdict, dry_run):
    # respx.mock with no registered routes — any HTTP call would raise
    env = ActivityEnvironment()
    await env.run(comment, pr, verdict)  # should return without making any call


@respx.mock
async def test_comment_posts_to_correct_url(pr, verdict, with_pat):
    route = respx.post(f"{BASE_URL}/issues/{PR_NUM}/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    env = ActivityEnvironment()
    await env.run(comment, pr, verdict)
    assert route.called


@respx.mock
async def test_comment_body_contains_verdict_badge(pr, verdict, with_pat):
    route = respx.post(f"{BASE_URL}/issues/{PR_NUM}/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    env = ActivityEnvironment()
    await env.run(comment, pr, verdict)
    body = route.calls[0].request.content.decode()
    assert "GREEN" in body


@respx.mock
async def test_comment_401_raises_non_retryable(pr, verdict, with_pat):
    respx.post(f"{BASE_URL}/issues/{PR_NUM}/comments").mock(
        return_value=httpx.Response(401)
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(comment, pr, verdict)
    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# merge_pr
# ---------------------------------------------------------------------------

@respx.mock
async def test_merge_pr_dry_run_makes_no_http_call(pr, dry_run):
    env = ActivityEnvironment()
    await env.run(merge_pr, pr)


@respx.mock
async def test_merge_pr_pr_not_open_raises_non_retryable(pr, with_pat):
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json={"state": "closed", "head": {"sha": "abc123"}})
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(merge_pr, pr)
    assert exc_info.value.non_retryable is True
    assert "closed" in str(exc_info.value)


@respx.mock
async def test_merge_pr_sha_mismatch_raises_non_retryable(pr, with_pat):
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json=_open_pr_payload(sha="different_sha"))
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(merge_pr, pr)
    assert exc_info.value.non_retryable is True
    assert "SHA" in str(exc_info.value)


@respx.mock
async def test_merge_pr_skips_sha_check_when_head_sha_empty(pr, with_pat):
    pr.head_sha = ""
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json=_open_pr_payload(sha="anything"))
    )
    respx.put(f"{BASE_URL}/pulls/{PR_NUM}/merge").mock(
        return_value=httpx.Response(200, json={"merged": True})
    )
    env = ActivityEnvironment()
    await env.run(merge_pr, pr)  # should not raise


@respx.mock
async def test_merge_pr_405_is_retryable(pr, with_pat):
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json=_open_pr_payload())
    )
    respx.put(f"{BASE_URL}/pulls/{PR_NUM}/merge").mock(
        return_value=httpx.Response(405, json={"message": "not mergeable"})
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(merge_pr, pr)
    assert exc_info.value.non_retryable is False


@respx.mock
async def test_merge_pr_422_is_non_retryable(pr, with_pat):
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json=_open_pr_payload())
    )
    respx.put(f"{BASE_URL}/pulls/{PR_NUM}/merge").mock(
        return_value=httpx.Response(422, json={"message": "merge conflict"})
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(merge_pr, pr)
    assert exc_info.value.non_retryable is True
    assert "merge conflict" in str(exc_info.value)


@respx.mock
async def test_merge_pr_success_uses_squash(pr, with_pat):
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json=_open_pr_payload())
    )
    merge_route = respx.put(f"{BASE_URL}/pulls/{PR_NUM}/merge").mock(
        return_value=httpx.Response(200, json={"merged": True})
    )
    env = ActivityEnvironment()
    await env.run(merge_pr, pr)

    import json
    body = json.loads(merge_route.calls[0].request.content)
    assert body["merge_method"] == "squash"
    assert body["sha"] == "abc123"


# ---------------------------------------------------------------------------
# request_review
# ---------------------------------------------------------------------------

@respx.mock
async def test_request_review_dry_run_makes_no_http_call(pr, dry_run):
    env = ActivityEnvironment()
    await env.run(request_review, pr, ["alice", "bob"])


@respx.mock
async def test_request_review_posts_reviewers(pr, with_pat):
    route = respx.post(f"{BASE_URL}/pulls/{PR_NUM}/requested_reviewers").mock(
        return_value=httpx.Response(201, json={})
    )
    env = ActivityEnvironment()
    await env.run(request_review, pr, ["alice", "bob"])

    import json
    body = json.loads(route.calls[0].request.content)
    assert body["reviewers"] == ["alice", "bob"]


# ---------------------------------------------------------------------------
# label
# ---------------------------------------------------------------------------

@respx.mock
async def test_label_dry_run_makes_no_http_call(pr, dry_run):
    env = ActivityEnvironment()
    await env.run(label, pr, "supply-chain-suspicious")


@respx.mock
async def test_label_posts_to_correct_url(pr, with_pat):
    route = respx.post(f"{BASE_URL}/issues/{PR_NUM}/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    env = ActivityEnvironment()
    await env.run(label, pr, "supply-chain-suspicious")

    import json
    body = json.loads(route.calls[0].request.content)
    assert body["labels"] == ["supply-chain-suspicious"]


# ---------------------------------------------------------------------------
# close_pr
# ---------------------------------------------------------------------------

@respx.mock
async def test_close_pr_dry_run_makes_no_http_call(pr, dry_run):
    env = ActivityEnvironment()
    await env.run(close_pr, pr, "Suspicious release.")


@respx.mock
async def test_close_pr_posts_comment_then_patches_pr(pr, with_pat):
    comment_route = respx.post(f"{BASE_URL}/issues/{PR_NUM}/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    close_route = respx.patch(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json={})
    )
    env = ActivityEnvironment()
    await env.run(close_pr, pr, "Suspicious release.")

    assert comment_route.called
    assert close_route.called

    import json
    close_body = json.loads(close_route.calls[0].request.content)
    assert close_body["state"] == "closed"


@respx.mock
async def test_close_pr_with_ignore_dependabot_includes_magic_phrase(pr, with_pat):
    respx.post(f"{BASE_URL}/issues/{PR_NUM}/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    respx.patch(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json={})
    )
    comment_route = respx.post(f"{BASE_URL}/issues/{PR_NUM}/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    env = ActivityEnvironment()
    await env.run(close_pr, pr, "Blocked.", ignore_dependabot=True)

    # The closing comment body must contain the Dependabot magic phrase
    comment_body = comment_route.calls[0].request.content.decode()
    assert "@dependabot ignore this dependency" in comment_body


@respx.mock
async def test_close_pr_422_raises_non_retryable(pr, with_pat):
    respx.post(f"{BASE_URL}/issues/{PR_NUM}/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    respx.patch(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(422, json={"message": "already closed"})
    )
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(close_pr, pr, "Blocked.")
    assert exc_info.value.non_retryable is True
    assert "already closed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# get_pr
# ---------------------------------------------------------------------------

@respx.mock
async def test_get_pr_dry_run_returns_open_state(pr, dry_run):
    env = ActivityEnvironment()
    result = await env.run(get_pr, pr)
    assert result["state"] == "open"
    assert result["mergeable"] is True


@respx.mock
async def test_get_pr_returns_state_and_checks(pr, with_pat):
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json={
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
        })
    )
    env = ActivityEnvironment()
    result = await env.run(get_pr, pr)
    assert result["state"] == "open"
    assert result["mergeable"] is True
    assert result["checks_passed"] is True


@respx.mock
async def test_get_pr_checks_not_passed_when_mergeable_state_not_clean(pr, with_pat):
    respx.get(f"{BASE_URL}/pulls/{PR_NUM}").mock(
        return_value=httpx.Response(200, json={
            "state": "open",
            "mergeable": True,
            "mergeable_state": "blocked",
        })
    )
    env = ActivityEnvironment()
    result = await env.run(get_pr, pr)
    assert result["checks_passed"] is False
