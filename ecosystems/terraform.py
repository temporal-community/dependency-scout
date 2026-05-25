"""Ecosystem provider for the Terraform Registry (registry.terraform.io).

Package names come in two forms from Dependabot:
  Providers: ``registry.terraform.io/hashicorp/aws`` or ``hashicorp/aws``
  Modules:   ``hashicorp/consul/aws``

After stripping the ``registry.terraform.io/`` prefix:
  - 1 slash → provider  (namespace/type)
  - 2 slashes → module  (namespace/module/provider)
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

_API_BASE = "https://registry.terraform.io/v1"
_REGISTRY_PREFIX = "registry.terraform.io/"


def _parse_package(package: str) -> tuple[str, str, str | None]:
    """Parse a Terraform package name into (namespace, name, subname_or_none).

    Strips the ``registry.terraform.io/`` prefix if present, then:
      ``hashicorp/aws``            → ("hashicorp", "aws", None)       — provider
      ``hashicorp/consul/aws``     → ("hashicorp", "consul", "aws")   — module
    """
    if package.startswith(_REGISTRY_PREFIX):
        package = package[len(_REGISTRY_PREFIX) :]

    parts = package.split("/")
    if len(parts) == 2:
        return parts[0], parts[1], None
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ApplicationError(
        f"Invalid Terraform package format: {package!r} — expected namespace/type or namespace/module/provider",
        non_retryable=True,
    )


class TerraformProvider(EcosystemProviderBase):
    ecosystem_name = "terraform"
    osv_name = ""
    dependabot_slug = "terraform"
    name_re = re.compile(r"^[a-z0-9][a-z0-9-_./]{0,127}$")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_provider_info(
        self, client: httpx.AsyncClient, namespace: str, name: str
    ) -> dict:
        resp = await client.get(f"{_API_BASE}/providers/{namespace}/{name}", timeout=15.0)
        if resp.status_code == 404:
            raise ApplicationError(
                f"PackageNotFound: {namespace}/{name} not found on Terraform Registry",
                non_retryable=True,
            )
        resp.raise_for_status()
        return resp.json()

    async def _get_module_info(
        self, client: httpx.AsyncClient, namespace: str, name: str, subname: str
    ) -> dict:
        resp = await client.get(f"{_API_BASE}/modules/{namespace}/{name}/{subname}", timeout=15.0)
        if resp.status_code == 404:
            raise ApplicationError(
                f"PackageNotFound: {namespace}/{name}/{subname} not found on Terraform Registry",
                non_retryable=True,
            )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # fetch_metadata
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> MetadataChecks:
        namespace, name, subname = _parse_package(package)
        client = get_client()

        if subname is None:
            # Provider
            data = await self._get_provider_info(client, namespace, name)
            description = (data.get("description") or "")[:500] or None
        else:
            # Module
            data = await self._get_module_info(client, namespace, name, subname)
            description = (data.get("description") or "")[:500] or None

        return MetadataChecks(
            weekly_downloads=None,
            is_major_bump=is_major(old_version, new_version),
            package_description=description,
        )

    # ------------------------------------------------------------------
    # fetch_release_age
    # ------------------------------------------------------------------

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeChecks:
        namespace, name, subname = _parse_package(package)
        client = get_client()

        if subname is None:
            resp = await client.get(
                f"{_API_BASE}/providers/{namespace}/{name}/{new_version}", timeout=15.0
            )
        else:
            resp = await client.get(
                f"{_API_BASE}/modules/{namespace}/{name}/{subname}/{new_version}", timeout=15.0
            )

        if resp.status_code == 404:
            raise ApplicationError(
                f"PackageNotFound: {package}@{new_version} not found on Terraform Registry",
                non_retryable=True,
            )
        resp.raise_for_status()
        data = resp.json()

        raw = data.get("published_at", "")
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
        # Terraform Registry has no per-version maintainer history.
        return MaintainerChecks()

    # ------------------------------------------------------------------
    # get_archive_url
    # ------------------------------------------------------------------

    async def get_archive_url(
        self, client: httpx.AsyncClient, package: str, version: str
    ) -> tuple[str, str, str] | None:
        namespace, name, subname = _parse_package(package)

        if subname is None:
            resp = await client.get(
                f"{_API_BASE}/providers/{namespace}/{name}/{version}", timeout=15.0
            )
        else:
            resp = await client.get(
                f"{_API_BASE}/modules/{namespace}/{name}/{subname}/{version}", timeout=15.0
            )

        if resp.status_code != 200:
            return None
        data = resp.json()

        # For providers the field is "source_repo"; for modules it is "source"
        source_url = data.get("source_repo") or data.get("source") or ""
        vcs = parse_vcs_repo(source_url)
        if not vcs:
            return None

        platform, owner_repo = vcs
        if platform != "github":
            return None

        owner, repo = owner_repo.split("/", 1)
        filename = f"{name}-{version}.tar.gz"
        for tag in (f"v{version}", version):
            url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/tags/{tag}"
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
        """Extract a .tar.gz to dest."""
        buf = io.BytesIO(archive_bytes)
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            safe_tar_extractall(tf, dest)

    # ------------------------------------------------------------------
    # fetch_attestations
    # ------------------------------------------------------------------

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationChecks:
        namespace, name, subname = _parse_package(package)
        client = get_client()

        if subname is None:
            resp = await client.get(
                f"{_API_BASE}/providers/{namespace}/{name}/{new_version}", timeout=15.0
            )
        else:
            resp = await client.get(
                f"{_API_BASE}/modules/{namespace}/{name}/{subname}/{new_version}", timeout=15.0
            )

        if resp.status_code != 200:
            return AttestationChecks()

        data = resp.json()
        source_url = data.get("source_repo") or data.get("source") or ""
        vcs = parse_vcs_repo(source_url)
        if not vcs:
            return AttestationChecks()

        platform, owner_repo = vcs
        owner = owner_repo.split("/")[0]

        new_sig, age_days = await asyncio.gather(
            fetch_vcs_tag_signature(
                platform, owner, owner_repo.split("/", 1)[1], new_version, None
            ),
            fetch_vcs_account_age(platform, owner),
        )

        return AttestationChecks(
            has_attestation=(new_sig is True),
            publisher_account_age_days=age_days,
        )

    # ------------------------------------------------------------------
    # fetch_release
    # ------------------------------------------------------------------

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseChecks:
        import os

        token = os.environ.get("GITHUB_TOKEN")
        namespace, name, subname = _parse_package(package)
        client = get_client()

        if subname is None:
            resp = await client.get(
                f"{_API_BASE}/providers/{namespace}/{name}/{version}", timeout=15.0
            )
        else:
            resp = await client.get(
                f"{_API_BASE}/modules/{namespace}/{name}/{subname}/{version}", timeout=15.0
            )

        if resp.status_code != 200:
            return ReleaseChecks()

        data = resp.json()
        source_url = data.get("source_repo") or data.get("source") or ""
        vcs = parse_vcs_repo(source_url)
        if not vcs:
            return ReleaseChecks()

        platform, owner_repo = vcs
        owner, repo = owner_repo.split("/", 1)

        registry_time: datetime | None = None
        raw = data.get("published_at", "")
        if raw:
            try:
                registry_time = parse_upload_time(raw)
            except Exception:  # noqa: BLE001
                pass

        release, new_sig, old_sig = await asyncio.gather(
            fetch_vcs_release(platform, owner, repo, version, token),
            fetch_vcs_tag_signature(platform, owner, repo, version, token),
            fetch_vcs_tag_signature(platform, owner, repo, old_version, token),
        )

        if release:
            return build_release_checks(release, registry_time, new_sig, old_sig).model_copy(
                update={"metadata_repo": owner_repo}
            )
        return ReleaseChecks(metadata_repo=owner_repo)
