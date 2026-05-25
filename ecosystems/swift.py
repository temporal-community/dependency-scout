"""Swift Package Manager ecosystem provider.

Swift packages are distributed as source code via git repositories — there is
no centralised package registry.  Dependabot supplies the package URL directly,
typically a GitHub URL such as ``https://github.com/apple/swift-argument-parser``
or the bare form ``github.com/owner/repo``.  All signals come from the GitHub
(or GitLab) API.
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
    """Return (v-prefixed, bare) tag names, e.g. "1.2.3" → ("v1.2.3", "1.2.3")."""
    bare = version.lstrip("vV")
    return f"v{bare}", bare


def _parse_package(package: str) -> tuple[str, str, str]:
    """Parse a Swift package URL into (platform, owner, repo).

    Accepts URLs in these forms:
    - ``https://github.com/owner/repo``
    - ``https://github.com/owner/repo.git``
    - ``github.com/owner/repo``
    - ``https://gitlab.com/owner/repo``
    - bare ``gitlab.com/owner/repo``

    Returns (platform, owner, repo).  ``platform`` is "github" or "gitlab".
    Raises ApplicationError (non_retryable) when the URL cannot be parsed.
    """
    # Strip scheme
    url = package.strip()
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            url = url[len(scheme) :]
            break

    # Strip trailing slash and .git suffix
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    # Determine platform from host
    custom_gitlab_base = os.environ.get("GITLAB_BASE_URL", "").rstrip("/")
    custom_gitlab_host = ""
    if custom_gitlab_base and "://" in custom_gitlab_base:
        custom_gitlab_host = custom_gitlab_base.split("://", 1)[1]

    if "github.com" in url:
        platform = "github"
        # Remove "github.com/" prefix to get owner/repo
        host_stripped = url.split("github.com/", 1)[-1]
    elif "gitlab.com" in url:
        platform = "gitlab"
        host_stripped = url.split("gitlab.com/", 1)[-1]
    elif custom_gitlab_host and custom_gitlab_host in url:
        platform = "gitlab"
        host_stripped = url.split(custom_gitlab_host + "/", 1)[-1]
    else:
        # Default to GitHub — the vast majority of Swift packages live there
        platform = "github"
        # Strip any remaining host by taking everything after the first "/"
        parts = url.split("/", 1)
        host_stripped = parts[1] if len(parts) == 2 else url

    # Split owner/repo
    path_parts = host_stripped.split("/")
    if len(path_parts) < 2 or not path_parts[0] or not path_parts[1]:
        raise ApplicationError(
            f"PackageNotFound: cannot parse Swift package URL {package!r} into owner/repo",
            non_retryable=True,
        )

    owner = path_parts[0]
    repo = path_parts[1]
    return platform, owner, repo


def _gh_headers(token: str | None) -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class SwiftProvider(EcosystemProviderBase):
    """Provider for Swift Package Manager dependency bumps.

    All signals are repo-based — there is no Swift package registry:
    - Metadata       → repo info (description, major-bump)
    - Release age    → GitHub/GitLab release or tag commit timestamp
    - Maintainer     → no registry maintainer list; returns safe default
    - Archive        → source tarball from codeload.github.com (GitHub) or
                       gitlab.com archive (GitLab)
    - Attestations   → annotated tag signature + owner account age
    - Release        → release notes, tag signing, CI workflow tampering
    """

    # ------------------------------------------------------------------
    # fetch_metadata
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> MetadataChecks:
        platform, owner, repo = _parse_package(package)
        token = os.environ.get("GITHUB_TOKEN")
        client = get_client()

        if platform == "github":
            try:
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
                    f"PackageNotFound: {package} not found on GitHub",
                    non_retryable=True,
                )
            if resp.status_code != 200:
                raise ApplicationError(
                    f"PackageNotFound: {package} — GitHub API {resp.status_code}",
                    non_retryable=False,
                )
            data = resp.json()
            description = data.get("description") or None
        else:
            # GitLab
            import urllib.parse as _urlparse

            base_url = os.environ.get("GITLAB_BASE_URL", "https://gitlab.com").rstrip("/")
            encoded = _urlparse.quote(f"{owner}/{repo}", safe="")
            gl_token = os.environ.get("GITLAB_TOKEN")
            headers: dict[str, str] = {}
            if gl_token:
                headers["Authorization"] = f"Bearer {gl_token}"
            try:
                resp = await client.get(
                    f"{base_url}/api/v4/projects/{encoded}",
                    headers=headers,
                    timeout=10.0,
                )
            except Exception as exc:
                raise ApplicationError(
                    f"PackageNotFound: {package} — GitLab API error: {exc}",
                    non_retryable=False,
                ) from exc
            if resp.status_code == 404:
                raise ApplicationError(
                    f"PackageNotFound: {package} not found on GitLab",
                    non_retryable=True,
                )
            if resp.status_code != 200:
                raise ApplicationError(
                    f"PackageNotFound: {package} — GitLab API {resp.status_code}",
                    non_retryable=False,
                )
            data = resp.json()
            description = data.get("description") or None

        return MetadataChecks(
            weekly_downloads=None,
            is_major_bump=is_major(old_version, new_version),
            package_description=description,
        )

    # ------------------------------------------------------------------
    # fetch_release_age
    # ------------------------------------------------------------------

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeChecks:
        platform, owner, repo = _parse_package(package)
        token = os.environ.get("GITHUB_TOKEN")

        # Try a formal release first (many Swift packages publish GitHub releases)
        release = await fetch_vcs_release(platform, owner, repo, new_version, token)
        if release:
            ts = release.get("published_at") or release.get("created_at", "")
            if ts:
                try:
                    published = parse_upload_time(ts)
                    hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
                    return ReleaseAgeChecks(release_age_hours=round(hours, 1))
                except Exception:  # noqa: BLE001
                    pass

        # Fall back: most-recent commit on the tag
        if platform == "github":
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
                        date_str = (
                            resp.json()[0].get("commit", {}).get("committer", {}).get("date", "")
                        )
                        if date_str:
                            committed = parse_upload_time(date_str)
                            hours = (datetime.now(timezone.utc) - committed).total_seconds() / 3600
                            return ReleaseAgeChecks(release_age_hours=round(hours, 1))
            except Exception:  # noqa: BLE001
                pass

        return ReleaseAgeChecks()

    # ------------------------------------------------------------------
    # fetch_maintainer
    # ------------------------------------------------------------------

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerChecks:
        # Swift has no package registry with a maintainer list.
        # Owner account age is surfaced via fetch_attestations.
        return MaintainerChecks()

    # ------------------------------------------------------------------
    # get_archive_url
    # ------------------------------------------------------------------

    async def get_archive_url(
        self, client, package: str, version: str
    ) -> tuple[str, str, str] | None:
        platform, owner, repo = _parse_package(package)
        for tag in _tags(version):
            if platform == "github":
                url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/tags/{tag}"
            else:
                # GitLab archive
                base = os.environ.get("GITLAB_BASE_URL", "https://gitlab.com").rstrip("/")
                url = f"{base}/{owner}/{repo}/-/archive/{tag}/{repo}-{tag}.tar.gz"
            try:
                validate_archive_url(url)
                resp = await client.head(url, timeout=10.0, follow_redirects=True)
                if resp.status_code == 200:
                    return url, f"{repo}-{version}.tar.gz", ""
            except Exception:  # noqa: BLE001
                continue
        return None

    # ------------------------------------------------------------------
    # extract_archive
    # ------------------------------------------------------------------

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
            safe_tar_extractall(tf, dest)

    # ------------------------------------------------------------------
    # fetch_attestations
    # ------------------------------------------------------------------

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationChecks:
        platform, owner, repo = _parse_package(package)
        token = os.environ.get("GITHUB_TOKEN")
        new_sig, old_sig, account_age = await asyncio.gather(
            fetch_vcs_tag_signature(platform, owner, repo, new_version, token),
            fetch_vcs_tag_signature(platform, owner, repo, old_version, token),
            fetch_vcs_account_age(platform, owner),
            return_exceptions=True,
        )
        if isinstance(new_sig, Exception):
            new_sig = None
        if isinstance(old_sig, Exception):
            old_sig = None
        if isinstance(account_age, Exception):
            account_age = None
        # Narrow the type so mypy is happy after the Exception guard above.
        account_age_days: int | None = account_age if isinstance(account_age, int) else None

        return AttestationChecks(
            has_attestation=new_sig is True,
            publisher_kind="GitHub" if new_sig is True else None,
            publisher_repo=f"{owner}/{repo}" if new_sig is True else None,
            publisher_changed=(old_sig is True) and (new_sig is not True),
            old_publisher_repo=(
                f"{owner}/{repo}" if (old_sig is True) and (new_sig is not True) else None
            ),
            publisher_account_age_days=account_age_days,
        )

    # ------------------------------------------------------------------
    # fetch_release
    # ------------------------------------------------------------------

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseChecks:
        platform, owner, repo = _parse_package(package)
        token = os.environ.get("GITHUB_TOKEN")
        release, old_sig, new_sig, ci_days = await asyncio.gather(
            fetch_vcs_release(platform, owner, repo, version, token),
            fetch_vcs_tag_signature(platform, owner, repo, old_version, token),
            fetch_vcs_tag_signature(platform, owner, repo, version, token),
            fetch_vcs_ci_workflow_changes(platform, owner, repo),
            return_exceptions=True,
        )
        if isinstance(release, Exception):
            release = None
        if isinstance(old_sig, Exception):
            old_sig = None
        if isinstance(new_sig, Exception):
            new_sig = None
        if isinstance(ci_days, Exception):
            ci_days = None

        metadata_repo = f"{owner}/{repo}"

        if not release:
            return ReleaseChecks(
                metadata_repo=metadata_repo,
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
        return checks.model_copy(
            update={
                "metadata_repo": metadata_repo,
                "ci_workflow_changed_days_ago": ci_days,
            }
        )
