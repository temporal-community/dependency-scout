"""
FastAPI webhook receiver for GitHub pull_request events.

Verifies HMAC-SHA256 signature, filters to Dependabot/Renovate PRs,
parses package + version from PR title/body, and starts PRActionWorkflow.
Returns 200 immediately — workflow execution is asynchronous.
"""
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Header, HTTPException, Request
from packaging.utils import canonicalize_name
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter

from activities.ecosystems import get_name_re
from activities.models import PRContext
from helpers.pr_parser import parse_pr
from workflows.pr_action_workflow import PRActionWorkflow

logger = logging.getLogger(__name__)

_BOT_LOGINS = {"dependabot[bot]", "renovate[bot]"}
_PR_ACTIONS = {"opened", "synchronize", "reopened"}

# Fallback for unknown ecosystems — strict enough to block injection attacks.
_FALLBACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,213}$")

# Version: semver-ish — digits, dots, hyphens, plus, tilde, caret, letters
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-~^]{0,127}$")


def _validate_parsed_package(ecosystem: str, package: str, old: str, new: str) -> str | None:
    """Return an error reason string, or None if the input is valid."""
    name_re = get_name_re(ecosystem) or _FALLBACK_NAME_RE
    if not name_re.match(package):
        return f"invalid package name: {package!r}"
    for label, ver in (("old_version", old), ("new_version", new)):
        if ver != "unknown" and not _VERSION_RE.match(ver):
            return f"invalid {label}: {ver!r}"
    return None

_temporal_client: Client | None = None


def _check_config() -> None:
    """Warn at startup about missing or suspicious configuration."""
    if not os.environ.get("GITHUB_WEBHOOK_SECRET"):
        logger.error(
            "GITHUB_WEBHOOK_SECRET is not set — all webhook requests will return 500. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    has_github = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_APP_ID")
    if not has_github:
        logger.warning(
            "No GitHub credentials found (GITHUB_TOKEN or GITHUB_APP_ID). "
            "The Scout will run but cannot post PR comments or take actions."
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info(
            "ANTHROPIC_API_KEY not set — using rule-based classifier instead of Claude."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _temporal_client
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _check_config()
    _temporal_client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
    )
    logger.info(
        "Connected to Temporal at %s (namespace=%s, task_queue=%s)",
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        os.environ.get("TEMPORAL_NAMESPACE", "default"),
        os.environ.get("TEMPORAL_TASK_QUEUE", "dependency-triage"),
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

    payload = json.loads(body)

    if x_github_event == "pull_request_review":
        return await _handle_review(payload)

    if x_github_event != "pull_request":
        logger.debug("Ignored event type: %s", x_github_event)
        return {"status": "ignored", "reason": "not a pull_request event"}

    action = payload.get("action")
    if action not in _PR_ACTIONS:
        logger.debug("Ignored pull_request action: %s", action)
        return {"status": "ignored", "reason": f"action={action}"}

    pr_author = payload.get("pull_request", {}).get("user", {}).get("login", "")
    if pr_author not in _BOT_LOGINS:
        logger.debug("Ignored PR from non-bot author: %s", pr_author)
        return {"status": "ignored", "reason": f"author={pr_author}"}

    repo = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    title = payload["pull_request"]["title"]
    body_text = payload["pull_request"].get("body") or ""
    head_ref = payload["pull_request"]["head"]["ref"]
    parsed = parse_pr(title, body_text, branch=head_ref)
    if not parsed:
        logger.warning(
            "Could not parse package/version from PR title — skipping %s#%s. Title: %r",
            repo, pr_number, title,
        )
        return {"status": "ignored", "reason": "could not parse package/version from PR title"}

    err = _validate_parsed_package(parsed.ecosystem, parsed.package, parsed.old_version, parsed.new_version)
    if err:
        logger.warning("Validation failed for %s#%s: %s", repo, pr_number, err)
        return {"status": "ignored", "reason": err}

    installation_id = payload.get("installation", {}).get("id", 0)
    head_sha = payload["pull_request"]["head"]["sha"]

    # canonicalize_name is PyPI-specific (normalizes Requests → requests); npm package names are case-sensitive
    package_name = canonicalize_name(parsed.package) if parsed.ecosystem == "pip" else parsed.package

    pr_context = PRContext(
        repo=repo,
        pr_number=pr_number,
        pr_author=pr_author,
        installation_id=installation_id,
        ecosystem=parsed.ecosystem,
        package_name=package_name,
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
        execution_timeout=timedelta(days=30),  # backstop for zombie workflows
    )

    logger.info(
        "Started workflow %s for %s#%s (%s %s %s→%s)",
        workflow_id, repo, pr_number,
        parsed.ecosystem, package_name, parsed.old_version, parsed.new_version,
    )
    return {"status": "started", "workflow_id": workflow_id}


async def _handle_review(payload: dict) -> dict:
    """Route a pull_request_review event to the waiting PRActionWorkflow.

    The reviewer identity comes from the HMAC-verified GitHub payload, not from
    a self-reported claim, so it can be trusted as the authoritative approver.
    Only 'submitted' events with state 'approved' or 'changes_requested' are acted on.
    """
    if payload.get("action") != "submitted":
        return {"status": "ignored", "reason": "not a submitted review"}

    state = payload.get("review", {}).get("state", "").lower()
    if state not in {"approved", "changes_requested"}:
        logger.debug("Ignored review with state=%s", state)
        return {"status": "ignored", "reason": f"review state={state}"}

    reviewer = payload.get("review", {}).get("user", {}).get("login", "")
    repo = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]

    workflow_id = f"pr-action-{repo.replace('/', '-')}-{pr_number}"
    decision = "approve" if state == "approved" else "reject"

    try:
        handle = _temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(PRActionWorkflow.submit_decision, decision, reviewer)
    except Exception:
        # Workflow may not exist (PR not from a bot, or already completed) — not an error.
        logger.debug("No active workflow for %s#%s — review signal dropped", repo, pr_number)
        return {"status": "ignored", "reason": "no active workflow for this PR"}

    logger.info("Signalled %s with decision=%s from reviewer=%s", workflow_id, decision, reviewer)
    return {"status": "signalled", "workflow_id": workflow_id, "decision": decision}
