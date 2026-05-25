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
    parse_vcs_repo,
    parse_upload_time,
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

_API_BASE = "https://hex.pm/api"
_CDN_BASE = "https://repo.hex.pm/tarballs"


def _normalise_package(package: str) -> str:
    """Strip owner prefix if present (e.g. 'owner/phoenix' → 'phoenix')."""
    if "/" in package:
        return package.split("/", 1)[1]
    return package


class MixProvider(EcosystemProviderBase):
    ecosystem_name = "mix"
    osv_name = "Hex"
    dependabot_slug = "mix"
    name_re = re.compile(r"^[a-z][a-z0-9_]{0,213}$")

    # ------------------------------------------------------------------
    # fetch_metadata
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> MetadataChecks:
        package = _normalise_package(package)
        client = get_client()
        resp = await client.get(f"{_API_BASE}/packages/{package}", timeout=15.0)
        if resp.status_code == 404:
            raise ApplicationError(
                f"{package} not found on Hex.pm",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()
        data = resp.json()

        description = (data.get("description") or "")[:500] or None
        weekly_downloads = (data.get("downloads") or {}).get("week")

        return MetadataChecks(
            weekly_downloads=weekly_downloads,
            is_major_bump=is_major(old_version, new_version),
            package_description=description,
        )

    # ------------------------------------------------------------------
    # fetch_release_age
    # ------------------------------------------------------------------

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeChecks:
        package = _normalise_package(package)
        client = get_client()
        resp = await client.get(
            f"{_API_BASE}/packages/{package}/releases/{new_version}", timeout=15.0
        )
        if resp.status_code == 404:
            return ReleaseAgeChecks(release_age_hours=None)
        if resp.status_code != 200:
            return ReleaseAgeChecks(release_age_hours=None)
        data = resp.json()

        raw = data.get("inserted_at", "")
        if not raw:
            return ReleaseAgeChecks(release_age_hours=None)

        upload_time = parse_upload_time(raw)
        hours = (datetime.now(timezone.utc) - upload_time).total_seconds() / 3600
        return ReleaseAgeChecks(release_age_hours=max(0.0, hours))

    # ------------------------------------------------------------------
    # fetch_maintainer
    # ------------------------------------------------------------------

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerChecks:
        # Hex.pm does not expose per-release owner history; the owners list is
        # current-only, so we cannot detect a change between two versions.
        return MaintainerChecks()

    # ------------------------------------------------------------------
    # get_archive_url
    # ------------------------------------------------------------------

    async def get_archive_url(
        self, client: httpx.AsyncClient, package: str, version: str
    ) -> tuple[str, str, str] | None:
        package = _normalise_package(package)
        filename = f"{package}-{version}.tar"
        url = f"{_CDN_BASE}/{filename}"
        validate_archive_url(url)
        return url, filename, ""

    # ------------------------------------------------------------------
    # extract_archive
    # ------------------------------------------------------------------

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None:
        """Extract a Hex .tar archive.

        Hex tarballs are plain (non-gzipped) tar files containing a
        ``contents.tar.gz`` entry with the actual package source.
        """
        outer_buf = io.BytesIO(archive_bytes)
        with tarfile.open(fileobj=outer_buf, mode="r:") as outer_tf:
            try:
                inner_member = outer_tf.getmember("contents.tar.gz")
            except KeyError:
                raise ApplicationError(
                    f"Hex archive {filename!r} does not contain contents.tar.gz",
                    non_retryable=True,
                )
            extracted = outer_tf.extractfile(inner_member)
            if extracted is None:
                raise ApplicationError(
                    f"Could not read contents.tar.gz from {filename!r}",
                    non_retryable=True,
                )
            inner_bytes = io.BytesIO(extracted.read())

        with tarfile.open(fileobj=inner_bytes, mode="r:gz") as inner_tf:
            safe_tar_extractall(inner_tf, dest)

    # ------------------------------------------------------------------
    # fetch_attestations
    # ------------------------------------------------------------------

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationChecks:
        package = _normalise_package(package)
        client = get_client()

        try:
            resp = await client.get(f"{_API_BASE}/packages/{package}", timeout=15.0)
        except Exception:  # noqa: BLE001
            return AttestationChecks(has_attestation=False)

        if resp.status_code != 200:
            return AttestationChecks(has_attestation=False)

        data = resp.json()
        github_url = (data.get("links") or {}).get("GitHub") or ""
        vcs = parse_vcs_repo(github_url)

        age_days: int | None = None
        if vcs:
            platform, owner_repo = vcs
            owner = owner_repo.split("/", 1)[0]
            # Fetch account age for the package publisher
            age_days = await fetch_vcs_account_age(platform, owner)

        # Hex does not have Sigstore/SLSA attestations
        return AttestationChecks(
            has_attestation=False,
            publisher_account_age_days=age_days,
        )

    # ------------------------------------------------------------------
    # fetch_release
    # ------------------------------------------------------------------

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseChecks:
        import os

        package = _normalise_package(package)
        token = os.environ.get("GITHUB_TOKEN")
        client = get_client()

        try:
            resp = await client.get(f"{_API_BASE}/packages/{package}", timeout=15.0)
        except Exception:  # noqa: BLE001
            return ReleaseChecks()

        if resp.status_code != 200:
            return ReleaseChecks()

        data = resp.json()
        github_url = (data.get("links") or {}).get("GitHub") or ""
        vcs = parse_vcs_repo(github_url)
        if not vcs:
            return ReleaseChecks()

        platform, owner_repo = vcs
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
            return build_release_checks(release, None, new_sig, old_sig).model_copy(update=extra)
        return ReleaseChecks(**extra)
