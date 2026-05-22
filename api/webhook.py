"""
FastAPI webhook receiver for GitHub pull_request events.

Verifies HMAC-SHA256 signature, filters to Dependabot/Renovate PRs,
parses package + version from PR title/body, and starts PRActionWorkflow.
Returns 200 immediately — workflow execution is asynchronous.
"""
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from packaging.utils import canonicalize_name
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter

from activities.models import PRContext
from helpers.pr_parser import parse_pr
from workflows.pr_action_workflow import PRActionWorkflow

_BOT_LOGINS = {"dependabot[bot]", "renovate[bot]"}
_PR_ACTIONS = {"opened", "synchronize", "reopened"}

_temporal_client: Client | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _temporal_client
    _temporal_client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
    )
    yield


app = FastAPI(lifespan=lifespan)


def _verify_signature(body: bytes, signature: str) -> None:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET not configured")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "temporal_connected": _temporal_client is not None}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str = Header(...),
    x_github_event: str = Header(...),
) -> dict:
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "not a pull_request event"}

    payload = json.loads(body)

    action = payload.get("action")
    if action not in _PR_ACTIONS:
        return {"status": "ignored", "reason": f"action={action}"}

    pr_author = payload.get("pull_request", {}).get("user", {}).get("login", "")
    if pr_author not in _BOT_LOGINS:
        return {"status": "ignored", "reason": f"author={pr_author}"}

    title = payload["pull_request"]["title"]
    body_text = payload["pull_request"].get("body") or ""
    parsed = parse_pr(title, body_text)
    if not parsed:
        return {"status": "ignored", "reason": "could not parse package/version from PR title"}

    installation_id = payload.get("installation", {}).get("id", 0)
    repo = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    head_sha = payload["pull_request"]["head"]["sha"]

    pr_context = PRContext(
        repo=repo,
        pr_number=pr_number,
        pr_author=pr_author,
        installation_id=installation_id,
        ecosystem=parsed.ecosystem,
        package_name=canonicalize_name(parsed.package),  # normalize: Requests == requests
        old_version=parsed.old_version,
        new_version=parsed.new_version,
        head_sha=head_sha,
    )

    workflow_id = f"pr-action-{repo.replace('/', '-')}-{pr_number}"
    await _temporal_client.start_workflow(
        PRActionWorkflow.run,
        pr_context,
        id=workflow_id,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "dependency-triage"),
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    )

    return {"status": "started", "workflow_id": workflow_id}
