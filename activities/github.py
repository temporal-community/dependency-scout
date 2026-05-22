import os

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from activities.models import PRContext, Verdict
from helpers.comment_formatter import format_comment


def _dry_run() -> bool:
    """True when neither PAT nor GitHub App credentials are configured."""
    return not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GITHUB_APP_ID")


async def _get_headers(installation_id: int) -> dict:
    """
    Resolve auth token, in priority order:
      1. GITHUB_TOKEN (PAT) — convenient for local dev
      2. GitHub App installation token — for production
    """
    if token := os.environ.get("GITHUB_TOKEN"):
        pass  # use PAT directly
    else:
        from helpers.github_app import get_installation_token
        token = await get_installation_token(installation_id)
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo_url(pr: PRContext) -> str:
    return f"https://api.github.com/repos/{pr.repo}"


@activity.defn(name="activities.github.comment")
async def comment(pr: PRContext, verdict: Verdict) -> None:
    body = format_comment(pr, verdict)
    if _dry_run():
        activity.logger.info(f"[dry-run] Would post on {pr.repo}#{pr.pr_number}:\n{body}")
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_repo_url(pr)}/issues/{pr.pr_number}/comments",
            headers=await _get_headers(pr.installation_id),
            json={"body": body},
        )
        if resp.status_code == 401:
            raise ApplicationError("GitHub auth failed", non_retryable=True)
        resp.raise_for_status()
    activity.logger.info(f"Posted comment on {pr.repo}#{pr.pr_number}")


@activity.defn(name="activities.github.merge_pr")
async def merge_pr(pr: PRContext) -> None:
    if _dry_run():
        activity.logger.info(f"[dry-run] Would squash-merge {pr.repo}#{pr.pr_number}")
        return
    headers = await _get_headers(pr.installation_id)
    async with httpx.AsyncClient(timeout=15.0) as client:
        pr_resp = await client.get(
            f"{_repo_url(pr)}/pulls/{pr.pr_number}", headers=headers
        )
        pr_resp.raise_for_status()
        pr_data = pr_resp.json()

        if pr_data["state"] != "open":
            raise ApplicationError(
                f"PR #{pr.pr_number} is {pr_data['state']}, cannot merge",
                non_retryable=True,
            )

        merge_resp = await client.put(
            f"{_repo_url(pr)}/pulls/{pr.pr_number}/merge",
            headers=headers,
            json={"merge_method": "squash", "sha": pr_data["head"]["sha"]},
        )
        if merge_resp.status_code == 405:
            raise ApplicationError(
                f"PR #{pr.pr_number} not mergeable — CI may still be running",
                non_retryable=False,
            )
        if merge_resp.status_code == 422:
            raise ApplicationError(
                f"PR #{pr.pr_number} merge failed: {merge_resp.json().get('message')}",
                non_retryable=True,
            )
        merge_resp.raise_for_status()
    activity.logger.info(f"Merged {pr.repo}#{pr.pr_number} (squash)")


@activity.defn(name="activities.github.request_review")
async def request_review(pr: PRContext, reviewers: list[str]) -> None:
    if _dry_run():
        activity.logger.info(
            f"[dry-run] Would request review on {pr.repo}#{pr.pr_number} from {reviewers}"
        )
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_repo_url(pr)}/pulls/{pr.pr_number}/requested_reviewers",
            headers=await _get_headers(pr.installation_id),
            json={"reviewers": reviewers},
        )
        resp.raise_for_status()
    activity.logger.info(f"Requested review on {pr.repo}#{pr.pr_number} from {reviewers}")


@activity.defn(name="activities.github.label")
async def label(pr: PRContext, label_name: str) -> None:
    if _dry_run():
        activity.logger.info(
            f"[dry-run] Would add label '{label_name}' to {pr.repo}#{pr.pr_number}"
        )
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_repo_url(pr)}/issues/{pr.pr_number}/labels",
            headers=await _get_headers(pr.installation_id),
            json={"labels": [label_name]},
        )
        resp.raise_for_status()
    activity.logger.info(f"Added label '{label_name}' to {pr.repo}#{pr.pr_number}")


@activity.defn(name="activities.github.get_pr")
async def get_pr(pr: PRContext) -> dict:
    if _dry_run():
        return {"state": "open", "mergeable": True, "checks_passed": True}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_repo_url(pr)}/pulls/{pr.pr_number}",
            headers=await _get_headers(pr.installation_id),
        )
        resp.raise_for_status()
        data = resp.json()
    return {
        "state": data["state"],
        "mergeable": data.get("mergeable"),
        "checks_passed": data.get("mergeable_state") == "clean",
    }
