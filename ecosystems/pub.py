"""Ecosystem provider for Dart/Flutter's pub.dev package registry.

Package names are lowercase with underscores: e.g. "flutter", "dio", "http", "provider"
API docs: https://pub.dev/help/api
"""

from __future__ import annotations

import asyncio
import io
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
    parse_upload_time,
    parse_vcs_repo,
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

_PUB_API = "https://pub.dev/api"


class PubProvider(EcosystemProviderBase):
    ecosystem_name = "pub"
    osv_name = "Pub"
    dependabot_slug = "pub"
    name_re = re.compile(r"^[a-z][a-z0-9_]{0,213}$")

    # ------------------------------------------------------------------
    # fetch_metadata
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> MetadataChecks:
        client = get_client()
        resp = await client.get(f"{_PUB_API}/packages/{package}", timeout=15.0)
        if resp.status_code == 404:
            raise ApplicationError(
                f"{package} not found on pub.dev",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()
        data = resp.json()

        description = (data.get("latest") or {}).get("pubspec", {}).get("description") or ""
        description = description[:500] or None

        return MetadataChecks(
            weekly_downloads=None,  # pub.dev does not expose download counts via public API
            is_major_bump=is_major(old_version, new_version),
            package_description=description,
        )

    # ------------------------------------------------------------------
    # fetch_release_age
    # ------------------------------------------------------------------

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeChecks:
        client = get_client()
        resp = await client.get(
            f"{_PUB_API}/packages/{package}/versions/{new_version}", timeout=15.0
        )
        if resp.status_code == 404:
            return ReleaseAgeChecks()
        if resp.status_code != 200:
            return ReleaseAgeChecks()
        data = resp.json()

        raw = data.get("published", "")
        if not raw:
            return ReleaseAgeChecks()

        try:
            upload_time = parse_upload_time(raw)
            hours = (datetime.now(timezone.utc) - upload_time).total_seconds() / 3600
            return ReleaseAgeChecks(release_age_hours=max(0.0, hours))
        except Exception:  # noqa: BLE001
            return ReleaseAgeChecks()

    # ------------------------------------------------------------------
    # fetch_maintainer
    # ------------------------------------------------------------------

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerChecks:
        # pub.dev does not expose per-version publisher changes via a public API
        return MaintainerChecks()

    # ------------------------------------------------------------------
    # get_archive_url
    # ------------------------------------------------------------------

    async def get_archive_url(
        self, client: httpx.AsyncClient, package: str, version: str
    ) -> tuple[str, str, str] | None:
        resp = await client.get(f"{_PUB_API}/packages/{package}/versions/{version}", timeout=15.0)
        if resp.status_code == 404:
            raise ApplicationError(
                f"{package} {version} not found on pub.dev",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()
        data = resp.json()

        archive_sha256 = data.get("archive_sha256", "")
        url = f"https://pub.dev/packages/{package}/versions/{version}.tar.gz"
        validate_archive_url(url)
        filename = f"{package}-{version}.tar.gz"
        return url, filename, archive_sha256

    # ------------------------------------------------------------------
    # extract_archive
    # ------------------------------------------------------------------

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None:
        buf = io.BytesIO(archive_bytes)
        with tarfile.open(fileobj=buf) as tf:
            safe_tar_extractall(tf, dest)

    # ------------------------------------------------------------------
    # fetch_attestations
    # ------------------------------------------------------------------

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationChecks:
        client = get_client()
        resp = await client.get(
            f"{_PUB_API}/packages/{package}/versions/{new_version}", timeout=15.0
        )
        if resp.status_code != 200:
            return AttestationChecks(has_attestation=False)

        data = resp.json()
        pubspec = data.get("pubspec") or {}
        source_url = pubspec.get("repository") or pubspec.get("homepage") or ""

        age_days: int | None = None
        if source_url:
            vcs = parse_vcs_repo(source_url)
            if vcs:
                platform, owner_repo = vcs
                owner = owner_repo.split("/")[0]
                age_days = await fetch_vcs_account_age(platform, owner)

        return AttestationChecks(
            has_attestation=False,  # pub.dev does not support Sigstore attestation
            publisher_account_age_days=age_days,
        )

    # ------------------------------------------------------------------
    # fetch_release
    # ------------------------------------------------------------------

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseChecks:
        import os

        token = os.environ.get("GITHUB_TOKEN")
        client = get_client()
        resp = await client.get(f"{_PUB_API}/packages/{package}/versions/{version}", timeout=15.0)
        if resp.status_code != 200:
            return ReleaseChecks()

        data = resp.json()
        pubspec = data.get("pubspec") or {}
        source_url = pubspec.get("repository") or pubspec.get("homepage") or ""

        vcs = parse_vcs_repo(source_url)
        if not vcs:
            return ReleaseChecks()
        platform, owner_repo = vcs

        registry_time: datetime | None = None
        raw = data.get("published", "")
        if raw:
            try:
                registry_time = parse_upload_time(raw)
            except Exception:  # noqa: BLE001
                pass

        owner, repo = owner_repo.split("/", 1)
        release, new_sig, old_sig, ci_days = await asyncio.gather(
            fetch_vcs_release(platform, owner, repo, version, token),
            fetch_vcs_tag_signature(platform, owner, repo, version, token),
            fetch_vcs_tag_signature(platform, owner, repo, old_version, token),
            fetch_vcs_ci_workflow_changes(platform, owner, repo),
        )
        extra: dict = {"metadata_repo": owner_repo}
        if ci_days is not None:
            extra["ci_workflow_changed_days_ago"] = ci_days
        if release:
            return build_release_checks(release, registry_time, new_sig, old_sig).model_copy(
                update=extra
            )
        return ReleaseChecks(**extra)
