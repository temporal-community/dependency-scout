"""Ecosystem provider for the Elm package registry (package.elm-lang.org).

Package names use author/package format: e.g. "elm/core", "elm/html",
"mdgriffith/elm-ui". Because Elm packages ARE GitHub repos, the package
name is also the owner/repo slug — all GitHub VCS helpers work directly.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tarfile
from datetime import datetime, timezone

import httpx
from temporalio.exceptions import ApplicationError

from ecosystems import (
    EcosystemProviderBase,
    build_release_checks,
    fetch_vcs_account_age,
    fetch_vcs_ci_workflow_changes,
    fetch_vcs_release,
    fetch_vcs_tag_signature,
    is_major,
    safe_tar_extractall,
    validate_archive_url,
)
from models import (
    AttestationChecks,
    MaintainerChecks,
    MetadataChecks,
    ReleaseAgeChecks,
    ReleaseChecks,
)
from helpers.http import get_client

_ELM_API = "https://package.elm-lang.org"
_CODELOAD = "https://codeload.github.com"


class ElmProvider(EcosystemProviderBase):
    ecosystem_name = "elm"
    osv_name = "Elm"
    dependabot_slug = "elm"
    name_re = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9][a-zA-Z0-9-]{0,38}$")

    # ------------------------------------------------------------------
    # fetch_metadata
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> MetadataChecks:
        client = get_client()
        author, pkg = package.split("/", 1)
        resp = await client.get(f"{_ELM_API}/packages/{author}/{pkg}/releases.json", timeout=15.0)
        if resp.status_code == 404:
            raise ApplicationError(
                f"PackageNotFound: {package} not found on the Elm package registry",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()

        return MetadataChecks(
            weekly_downloads=None,
            is_major_bump=is_major(old_version, new_version),
            package_description=None,
        )

    # ------------------------------------------------------------------
    # fetch_release_age
    # ------------------------------------------------------------------

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeChecks:
        client = get_client()
        author, pkg = package.split("/", 1)
        resp = await client.get(f"{_ELM_API}/packages/{author}/{pkg}/releases.json", timeout=15.0)
        if resp.status_code != 200:
            return ReleaseAgeChecks()

        releases: dict[str, int] = resp.json()
        timestamp_ms = releases.get(new_version)
        if timestamp_ms is None:
            return ReleaseAgeChecks()

        release_time = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        hours = (datetime.now(timezone.utc) - release_time).total_seconds() / 3600
        return ReleaseAgeChecks(release_age_hours=max(0.0, hours))

    # ------------------------------------------------------------------
    # fetch_maintainer
    # ------------------------------------------------------------------

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerChecks:
        # Elm packages are GitHub repos — no per-release maintainer history available
        # from the Elm registry API itself.
        owner = package.split("/")[0]
        await fetch_vcs_account_age("github", owner)
        return MaintainerChecks()

    # ------------------------------------------------------------------
    # get_archive_url
    # ------------------------------------------------------------------

    async def get_archive_url(
        self, client: httpx.AsyncClient, package: str, version: str
    ) -> tuple[str, str, str] | None:
        author, pkg = package.split("/", 1)
        filename = f"{pkg}-{version}.tar.gz"

        for tag in (version, f"v{version}"):
            url = f"{_CODELOAD}/{author}/{pkg}/tar.gz/refs/tags/{tag}"
            validate_archive_url(url)
            try:
                head = await client.head(url, follow_redirects=True)
                if head.status_code == 200:
                    return url, filename, ""
            except Exception:  # noqa: BLE001
                continue

        return None

    # ------------------------------------------------------------------
    # extract_archive
    # ------------------------------------------------------------------

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None:
        buf = io.BytesIO(archive_bytes)
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            safe_tar_extractall(tf, dest)

    # ------------------------------------------------------------------
    # fetch_attestations
    # ------------------------------------------------------------------

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationChecks:
        token = os.environ.get("GITHUB_TOKEN")
        owner, repo = package.split("/", 1)

        new_sig, old_sig, age_days = await asyncio.gather(
            fetch_vcs_tag_signature("github", owner, repo, new_version, token),
            fetch_vcs_tag_signature("github", owner, repo, old_version, token),
            fetch_vcs_account_age("github", owner),
        )

        has_attestation = new_sig is True
        publisher_changed = old_sig is not None and old_sig is not new_sig and new_sig is not True

        return AttestationChecks(
            has_attestation=has_attestation,
            publisher_kind="github_actions" if has_attestation else None,
            publisher_repo=package if has_attestation else None,
            publisher_changed=publisher_changed,
            publisher_account_age_days=age_days,
        )

    # ------------------------------------------------------------------
    # fetch_release
    # ------------------------------------------------------------------

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseChecks:
        token = os.environ.get("GITHUB_TOKEN")
        owner, repo = package.split("/", 1)

        release, new_sig, old_sig, ci_days = await asyncio.gather(
            fetch_vcs_release("github", owner, repo, version, token),
            fetch_vcs_tag_signature("github", owner, repo, version, token),
            fetch_vcs_tag_signature("github", owner, repo, old_version, token),
            fetch_vcs_ci_workflow_changes("github", owner, repo),
        )

        extra: dict = {"metadata_repo": package}
        if ci_days is not None:
            extra["ci_workflow_changed_days_ago"] = ci_days

        if release:
            return build_release_checks(release, None, new_sig, old_sig).model_copy(update=extra)
        return ReleaseChecks(**extra)
