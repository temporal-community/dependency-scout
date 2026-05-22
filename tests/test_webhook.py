"""
Tests for the FastAPI webhook receiver.

Uses httpx.AsyncClient with ASGITransport so tests run in-process.
The Temporal client is mocked so no real Temporal server is needed.
"""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

TEST_SECRET = "test-webhook-secret"
TEST_REPO = "owner/repo"
TEST_PR_NUMBER = 42


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


TEST_HEAD_SHA = "abc123def456" * 3  # realistic-looking SHA

def _dependabot_payload(
    action: str = "opened",
    title: str = "Bump requests from 2.31.0 to 2.32.0",
    author: str = "dependabot[bot]",
    pr_number: int = TEST_PR_NUMBER,
    installation_id: int = 12345,
) -> bytes:
    payload = {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "title": title,
            "body": "",
            "user": {"login": author},
            "head": {"sha": TEST_HEAD_SHA},
        },
        "repository": {"full_name": TEST_REPO},
        "installation": {"id": installation_id},
    }
    return json.dumps(payload).encode()


@pytest.fixture
async def client(monkeypatch):
    """AsyncClient with mocked Temporal — no real server needed.

    ASGITransport does not trigger FastAPI lifespan, so we inject the mock
    Temporal client directly into the module-level variable instead.
    """
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)

    mock_tc = AsyncMock()
    mock_tc.start_workflow = AsyncMock(return_value=MagicMock(id="wf-id"))

    import api.webhook as webhook_module
    monkeypatch.setattr(webhook_module, "_temporal_client", mock_tc)

    from api.webhook import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, mock_tc


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

async def test_dependabot_pr_starts_workflow(client):
    ac, mock_tc = client
    body = _dependabot_payload()
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"
    assert "pr-action-owner-repo-42" in data["workflow_id"]
    mock_tc.start_workflow.assert_called_once()


async def test_renovate_pr_starts_workflow(client):
    ac, mock_tc = client
    body = _dependabot_payload(
        title="Update dependency requests to v2.32.0",
        author="renovate[bot]",
    )
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


async def test_workflow_id_uses_repo_and_pr_number(client):
    ac, mock_tc = client
    body = _dependabot_payload(pr_number=99)
    await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )
    _, kwargs = mock_tc.start_workflow.call_args
    assert kwargs["id"] == "pr-action-owner-repo-99"


async def test_pr_context_fields_correct(client):
    ac, mock_tc = client
    body = _dependabot_payload(installation_id=99999)
    await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )
    args, _ = mock_tc.start_workflow.call_args
    pr_context = args[1]
    assert pr_context.repo == TEST_REPO
    assert pr_context.pr_number == TEST_PR_NUMBER
    assert pr_context.package_name == "requests"
    assert pr_context.old_version == "2.31.0"
    assert pr_context.new_version == "2.32.0"
    assert pr_context.installation_id == 99999
    assert pr_context.head_sha == TEST_HEAD_SHA


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

async def test_invalid_signature_returns_401(client):
    ac, _ = client
    body = _dependabot_payload()
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, "wrong-secret"), "X-GitHub-Event": "pull_request"},
    )
    assert resp.status_code == 401


async def test_missing_signature_header_returns_422(client):
    ac, _ = client
    body = _dependabot_payload()
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Filtering — all should return 200 "ignored"
# ---------------------------------------------------------------------------

async def test_non_pr_event_ignored(client):
    ac, mock_tc = client
    body = _dependabot_payload()
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "push"},
    )
    assert resp.json()["status"] == "ignored"
    mock_tc.start_workflow.assert_not_called()


async def test_closed_action_ignored(client):
    ac, mock_tc = client
    body = _dependabot_payload(action="closed")
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )
    assert resp.json()["status"] == "ignored"
    mock_tc.start_workflow.assert_not_called()


async def test_human_author_ignored(client):
    ac, mock_tc = client
    body = _dependabot_payload(author="octocat")
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )
    assert resp.json()["status"] == "ignored"
    mock_tc.start_workflow.assert_not_called()


async def test_unparseable_title_ignored(client):
    ac, mock_tc = client
    body = _dependabot_payload(title="chore: update CI config")
    resp = await ac.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
    )
    assert resp.json()["status"] == "ignored"
    mock_tc.start_workflow.assert_not_called()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def test_healthz(client):
    ac, _ = client
    resp = await ac.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
