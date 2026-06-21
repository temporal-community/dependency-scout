"""Drive a remediation end-to-end: clone a repo, regenerate the affected lockfile(s) to a safe
version, and open a PR with the fix. Used by the standalone `scout remediate` CLI (the proactive
case: a vulnerability already sitting in a lockfile with no open Dependabot PR).

Git/tooling note: this is the one place Scout shells out (git + the ecosystem's lock tool) and
checks a repo out to disk — it can't be done over the GitHub API alone. The lock regeneration is
script-safe (see each remediator); cloning and pushing use the GITHUB_TOKEN.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from platforms.github import _parse_codeowners
from remediators import RemediationResult, get_remediator


@dataclass
class RemediationRun:
    package: str
    target_version: str
    results: list[RemediationResult] = field(default_factory=list)
    pr_url: str = ""
    opened: bool = False
    message: str = ""


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    # `credential.helper=` disables credential helpers for these calls: the token is supplied in
    # the clone URL, so git needs no helper — and this stops it from persisting an `x-access-token`
    # entry to the OS keychain (which would otherwise make every later `git push` prompt the user
    # to pick between accounts). GIT_TERMINAL_PROMPT=0 ensures it never blocks on an interactive
    # prompt either.
    proc = subprocess.run(
        ["git", "-c", "credential.helper=", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc


def _find_projects(root: Path, lockfile_name: str, affects, package: str) -> list[Path]:
    """Project dirs whose lockfile contains the package (skipping .git/vendored trees)."""
    hits: list[Path] = []
    for lock in root.rglob(lockfile_name):
        if ".git" in lock.parts:
            continue
        try:
            if affects(lock.read_text(), package):
                hits.append(lock.parent)
        except OSError:
            continue
    return sorted(hits)


async def remediate_and_open_pr(
    repo: str,
    package: str,
    target_version: str,
    ecosystem: str = "pip",
    *,
    project_dirs: list[str] | None = None,
    dry_run: bool = False,
) -> RemediationRun:
    remediator = get_remediator(ecosystem)
    if remediator is None:
        raise ValueError(
            f"No remediator for ecosystem {ecosystem!r} yet — remediation is available for: pip/uv."
        )
    # Prefer an explicit token; otherwise mint one from App creds (GITHUB_APP_ID + private key) —
    # the App path sidesteps the per-user SAML SSO wall that blocks personal tokens on SSO orgs.
    token = os.environ.get("GITHUB_TOKEN")
    used_app = False
    if not token and os.environ.get("GITHUB_APP_ID"):
        from helpers.github_app import get_installation_token_for_repo

        token = await get_installation_token_for_repo(repo)
        used_app = True
    if not token:
        raise ValueError(
            "Set GITHUB_TOKEN, or GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY[_PATH], to clone and "
            "open a remediation PR."
        )
    commit_name, commit_email = await _commit_identity(used_app, token)

    run = RemediationRun(package=package, target_version=target_version)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        _git(["clone", "--depth", "1", clone_url, str(root)], cwd=tmp)

        if project_dirs:
            projects = [root / d for d in project_dirs]
        else:
            projects = _find_projects(root, remediator.lockfile_name, remediator.affects, package)

        if not projects:
            run.message = f"No project under {repo} has {package} in a {remediator.lockfile_name}."
            return run

        for project in projects:
            run.results.append(remediator.remediate(project, package, target_version))

        fixed = [r for r in run.results if r.changed and r.reached_target]
        if not fixed:
            run.message = (
                f"Couldn't reach {package} {target_version} in any project — see per-project notes "
                "(a parent dependency is likely capping the version)."
            )
            return run

        if dry_run:
            run.message = f"[dry-run] Would open a PR bumping {package} to ≥{target_version}."
            return run

        # Commit the regenerated lockfiles on a Scout branch and push.
        branch = f"scout/security-{package}-{target_version}".replace(" ", "-")
        _git(["checkout", "-b", branch], cwd=root)
        for r in fixed:
            _git(["add", str(Path(r.project_dir) / r.lockfile)], cwd=root)
        _git(
            [
                "-c",
                f"user.name={commit_name}",
                "-c",
                f"user.email={commit_email}",
                "commit",
                "-m",
                f"Security: bump {package} to {target_version}",
            ],
            cwd=root,
        )
        _git(["push", "--force", "origin", f"HEAD:refs/heads/{branch}"], cwd=root)

        owners = _read_codeowners(root)
        run.pr_url = await _open_pr(repo, branch, token, package, target_version, fixed, owners)
        run.opened = True
        run.message = f"Opened {run.pr_url}"
        return run


async def _commit_identity(used_app: bool, token: str) -> tuple[str, str]:
    """Author commits as the acting identity so they're attributed to a real account (and pass
    CLA checks): the App bot when minting via App creds, else the authenticated token user."""
    if used_app:
        from helpers.github_app import get_app_bot_identity

        return await get_app_bot_identity(token)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if r.status_code == 200:
        u = r.json()
        return u["login"], f"{u['id']}+{u['login']}@users.noreply.github.com"
    return "dependency-scout", "dependency-scout@users.noreply.github.com"


def _read_codeowners(root: Path) -> list[str]:
    for rel in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        path = root / rel
        if path.exists():
            return _parse_codeowners(path.read_text())
    return []


async def _open_pr(
    repo: str,
    branch: str,
    token: str,
    package: str,
    target_version: str,
    fixed: list[RemediationResult],
    owners: list[str],
) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        repo_resp = await client.get(f"https://api.github.com/repos/{repo}", headers=headers)
        repo_resp.raise_for_status()
        base = repo_resp.json()["default_branch"]

        bullets = "\n".join(
            f"- `{r.project_dir or '.'}`: {r.package} {r.old_version} → {r.new_version}"
            for r in fixed
        )
        mention = ("\n\ncc " + " ".join(owners)) if owners else ""
        body = (
            f"**Dependency Scout — security remediation.**\n\n"
            f"`{package}` was vulnerable and pinned below the safe release; this regenerates the "
            f"lockfile(s) to **{target_version}** (resolution only — no install scripts ran):\n\n"
            f"{bullets}\n\n"
            f"Review and merge to clear the advisory.{mention}"
        )
        pr_resp = await client.post(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            json={
                "title": f"Security: bump {package} to {target_version}",
                "head": branch,
                "base": base,
                "body": body,
                "maintainer_can_modify": True,
            },
        )
        if pr_resp.status_code == 422:
            # A PR already exists for this head branch — surface it instead of failing.
            existing = await client.get(
                f"https://api.github.com/repos/{repo}/pulls",
                headers=headers,
                params={"head": f"{repo.split('/')[0]}:{branch}", "state": "open"},
            )
            existing.raise_for_status()
            items = existing.json()
            if items:
                return str(items[0]["html_url"])
        pr_resp.raise_for_status()
        pr = pr_resp.json()
        # Best-effort label; don't fail the PR if labelling is rejected.
        try:
            await client.post(
                f"https://api.github.com/repos/{repo}/issues/{pr['number']}/labels",
                headers=headers,
                json={"labels": ["security"]},
            )
        except httpx.HTTPError:
            pass
        return str(pr["html_url"])
