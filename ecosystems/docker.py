from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from temporalio.exceptions import ApplicationError

from ecosystems import (
    EcosystemProviderBase,
    is_major,
    parse_upload_time,
)
from models import (
    AttestationChecks,
    MaintainerChecks,
    MetadataChecks,
    ReleaseAgeChecks,
    ReleaseChecks,
)
from helpers.http import get_client

_HUB_BASE = "https://hub.docker.com/v2/repositories"


def _parse_image(package: str) -> tuple[str, str]:
    """Split a Docker image name into (namespace, repository).

    Official images like "nginx" or "ubuntu" have no slash — they live under
    the ``library`` namespace on Docker Hub.  Images like "hashicorp/terraform"
    are returned as-is.
    """
    if "/" not in package:
        return "library", package
    namespace, _, repository = package.partition("/")
    return namespace, repository


class DockerProvider(EcosystemProviderBase):
    ecosystem_name = "docker"
    osv_name = ""
    dependabot_slug = "docker"
    name_re = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")

    # ------------------------------------------------------------------
    # fetch_metadata
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> MetadataChecks:
        ns, repo = _parse_image(package)
        client = get_client()
        resp = await client.get(f"{_HUB_BASE}/{ns}/{repo}/", timeout=15.0)
        if resp.status_code == 404:
            raise ApplicationError(
                f"{package} not found on Docker Hub",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()
        data = resp.json()

        description = (data.get("description") or "")[:500] or None
        return MetadataChecks(
            # Docker Hub only exposes total pull_count, not weekly downloads.
            weekly_downloads=None,
            is_major_bump=is_major(old_version, new_version),
            package_description=description,
        )

    # ------------------------------------------------------------------
    # fetch_release_age
    # ------------------------------------------------------------------

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeChecks:
        ns, repo = _parse_image(package)
        client = get_client()
        resp = await client.get(f"{_HUB_BASE}/{ns}/{repo}/tags/{new_version}", timeout=15.0)
        if resp.status_code == 404:
            return ReleaseAgeChecks()
        if not resp.is_success:
            return ReleaseAgeChecks()
        data = resp.json()

        raw = data.get("last_updated", "")
        if not raw:
            return ReleaseAgeChecks()
        upload_time = parse_upload_time(raw)
        hours = (datetime.now(timezone.utc) - upload_time).total_seconds() / 3600
        return ReleaseAgeChecks(release_age_hours=max(0.0, hours))

    # ------------------------------------------------------------------
    # fetch_maintainer
    # ------------------------------------------------------------------

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerChecks:
        # Docker Hub does not expose per-tag publisher information via its
        # public API, so we cannot detect maintainer changes.
        return MaintainerChecks()

    # ------------------------------------------------------------------
    # get_archive_url
    # ------------------------------------------------------------------

    async def get_archive_url(
        self, client: httpx.AsyncClient, package: str, version: str
    ) -> tuple[str, str, str] | None:
        # Docker images are not distributed as downloadable source tarballs.
        # The diff check skips gracefully when this returns None.
        return None

    # ------------------------------------------------------------------
    # extract_archive
    # ------------------------------------------------------------------

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None:
        # Never called because get_archive_url returns None.
        raise NotImplementedError("Docker images do not have downloadable source archives")

    # ------------------------------------------------------------------
    # fetch_attestations
    # ------------------------------------------------------------------

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationChecks:
        # Docker Content Trust / Notary exists but requires the Docker CLI,
        # not a simple HTTP call.  SLSA attestations are not queryable via
        # the Docker Hub public API.
        return AttestationChecks()

    # ------------------------------------------------------------------
    # fetch_release
    # ------------------------------------------------------------------

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseChecks:
        # Docker images do not have GitHub-style releases.
        return ReleaseChecks()
