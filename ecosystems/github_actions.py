"""GitHub Actions ecosystem provider.

Packages are "owner/repo" strings (e.g. "actions/checkout").
Versions are the Dependabot-supplied tag number with any leading "v" stripped
(e.g. "4", "6", "3.1.0").  All signals come from the GitHub API — there is no
separate package registry.
"""

from __future__ import annotations

import asyncio
import io
import os
import tarfile
from datetime import datetime, timezone

from temporalio.exceptions import ApplicationError

from ecosystems import (
    EcosystemProviderBase,
    build_release_checks,
    fetch_vcs_account_age,
    fetch_vcs_ci_workflow_changes,
    fetch_vcs_release,
    fetch_vcs_tag_signature,
    is_major,
    parse_upload_time,
    safe_tar_extractall,
    validate_archive_url,
)
from helpers.http import get_client
from models import (
    AttestationChecks,
    MaintainerChecks,
    MetadataChecks,
    ReleaseAgeChecks,
    ReleaseChecks,
)


def _tags(version: str) -> tuple[str, str]:
    """Return (v-prefixed, bare) tag names, e.g. "4" → ("v4", "4")."""
    bare = version.lstrip("vV")
    return f"v{bare}", bare


def _parse(package: str) -> tuple[str, str]:
    parts = package.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ApplicationError(
            f"PackageNotFound: {package!r} is not a valid 'owner/repo' action name",
            non_retryable=True,
        )
    return parts[0], parts[1]


def _gh_headers(token: str | None) -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class GitHubActionsProvider(EcosystemProviderBase):
    """Provider for GitHub Actions version bumps.

    Signals are entirely repo-based:
    - Metadata       → repo info (description, archived status, major-bump)
    - Release age    → GitHub release or tag commit timestamp
    - Maintainer     → no registry maintainer list; returns safe default
    - Archive        → source tarball from codeload.github.com
    - Attestations   → annotated tag signature + owner account age
    - Release        → release notes, tag signing, CI workflow tampering
    """

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> MetadataChecks:
        owner, repo = _parse(package)
        token = os.environ.get("GITHUB_TOKEN")
        try:
            client = get_client()
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=_gh_headers(token),
                timeout=10.0,
            )
        except Exception as exc:
            raise ApplicationError(
                f"PackageNotFound: {package} — GitHub API error: {exc}",
                non_retryable=False,
            ) from exc
        if resp.status_code == 404:
            raise ApplicationError(
                f"PackageNotFound: {package} not found on GitHub", non_retryable=True
            )
        if resp.status_code != 200:
            raise ApplicationError(
                f"PackageNotFound: {package} — GitHub API {resp.status_code}",
                non_retryable=False,
            )
        data = resp.json()
        return MetadataChecks(
            weekly_downloads=None,
            is_major_bump=is_major(old_version, new_version),
            package_description=data.get("description") or None,
        )

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeChecks:
        owner, repo = _parse(package)
        token = os.environ.get("GITHUB_TOKEN")
        # Try the GitHub release first (most actions publish one).
        release = await fetch_vcs_release("github", owner, repo, new_version, token)
        if release:
            ts = release.get("published_at") or release.get("created_at", "")
            if ts:
                try:
                    published = parse_upload_time(ts)
                    hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
                    return ReleaseAgeChecks(release_age_hours=round(hours, 1))
                except Exception:
                    pass
        # Fall back: most-recent commit on the tag branch.
        try:
            client = get_client()
            for tag in _tags(new_version):
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits",
                    headers=_gh_headers(token),
                    params={"sha": tag, "per_page": "1"},
                    timeout=10.0,
                )
                if resp.status_code == 200 and resp.json():
                    date_str = resp.json()[0].get("commit", {}).get("committer", {}).get("date", "")
                    if date_str:
                        committed = parse_upload_time(date_str)
                        hours = (datetime.now(timezone.utc) - committed).total_seconds() / 3600
                        return ReleaseAgeChecks(release_age_hours=round(hours, 1))
        except Exception:
            pass
        return ReleaseAgeChecks()

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerChecks:
        # GitHub Actions have no package-registry maintainer list.
        # Owner account age is surfaced via fetch_attestations.
        return MaintainerChecks()

    async def get_archive_url(
        self, client, package: str, version: str
    ) -> tuple[str, str, str] | None:
        owner, repo = _parse(package)
        for tag in _tags(version):
            url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/tags/{tag}"
            try:
                validate_archive_url(url)
                resp = await client.head(url, timeout=10.0, follow_redirects=True)
                if resp.status_code == 200:
                    return url, f"{repo}-{version}.tar.gz", ""
            except Exception:
                continue
        return None

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
            safe_tar_extractall(tf, dest)

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationChecks:
        owner, repo = _parse(package)
        token = os.environ.get("GITHUB_TOKEN")
        new_sig, old_sig, account_age = await asyncio.gather(
            fetch_vcs_tag_signature("github", owner, repo, new_version, token),
            fetch_vcs_tag_signature("github", owner, repo, old_version, token),
            fetch_vcs_account_age("github", owner),
            return_exceptions=True,
        )
        # Treat any exception as None (graceful degradation).
        if isinstance(new_sig, BaseException):
            new_sig = None
        if isinstance(old_sig, BaseException):
            old_sig = None
        if isinstance(account_age, BaseException):
            account_age = None

        # Signed annotated tag is the strongest available provenance signal for
        # GitHub Actions (Sigstore/SLSA not applicable to action source trees).
        return AttestationChecks(
            has_attestation=new_sig is True,
            publisher_kind="GitHub" if new_sig is True else None,
            publisher_repo=f"{owner}/{repo}" if new_sig is True else None,
            publisher_changed=(old_sig is True) and (new_sig is not True),
            old_publisher_repo=(
                f"{owner}/{repo}" if (old_sig is True) and (new_sig is not True) else None
            ),
            publisher_account_age_days=account_age,
        )

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseChecks:
        owner, repo = _parse(package)
        token = os.environ.get("GITHUB_TOKEN")
        release, old_sig, new_sig, ci_days = await asyncio.gather(
            fetch_vcs_release("github", owner, repo, version, token),
            fetch_vcs_tag_signature("github", owner, repo, old_version, token),
            fetch_vcs_tag_signature("github", owner, repo, version, token),
            fetch_vcs_ci_workflow_changes("github", owner, repo),
            return_exceptions=True,
        )
        if isinstance(release, BaseException):
            release = None
        if isinstance(old_sig, BaseException):
            old_sig = None
        if isinstance(new_sig, BaseException):
            new_sig = None
        if isinstance(ci_days, BaseException):
            ci_days = None

        if not release:
            return ReleaseChecks(
                tag_signature_verified=new_sig,
                tag_was_previously_signed=(old_sig is True) and (new_sig is not True),
                ci_workflow_changed_days_ago=ci_days,
            )
        checks = build_release_checks(
            release,
            registry_time=None,
            tag_signature_verified=new_sig,
            old_tag_signature_verified=old_sig,
        )
        return checks.model_copy(update={"ci_workflow_changed_days_ago": ci_days})
