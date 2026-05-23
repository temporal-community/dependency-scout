from __future__ import annotations

import asyncio
import io
import tarfile
from datetime import datetime, timezone

import httpx
from temporalio.exceptions import ApplicationError

from activities.ecosystems import (
    build_release_signals,
    fetch_github_release,
    fetch_tag_signature,
    is_major,
    parse_github_repo,
    parse_upload_time,
    validate_archive_url,
)
from activities.models import AttestationSignals, MaintainerSignals, PyPISignals, ReleaseAgeSignals, ReleaseSignals


class RubyGemsProvider:
    osv_name = "RubyGems"

    # ------------------------------------------------------------------
    # fetch_metadata
    # ------------------------------------------------------------------

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> PyPISignals:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"https://rubygems.org/api/v1/gems/{package}.json")
            if resp.status_code == 404:
                raise ApplicationError(
                    f"{package} not found on RubyGems",
                    type="PackageNotFound",
                    non_retryable=True,
                )
            resp.raise_for_status()
            data = resp.json()

        summary = (data.get("info") or "")[:500] or None
        # RubyGems has no weekly-downloads endpoint; total downloads is the best
        # popularity proxy available from the public API.
        total_downloads = data.get("downloads")

        return PyPISignals(
            weekly_downloads=total_downloads,
            is_major_bump=is_major(old_version, new_version),
            package_description=summary,
        )

    # ------------------------------------------------------------------
    # fetch_release_age
    # ------------------------------------------------------------------

    async def fetch_release_age(self, package: str, new_version: str) -> ReleaseAgeSignals:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"https://rubygems.org/api/v1/versions/{package}.json")
            if resp.status_code == 404:
                raise ApplicationError(
                    f"{package} not found on RubyGems",
                    type="PackageNotFound",
                    non_retryable=True,
                )
            resp.raise_for_status()
            versions = resp.json()

        for v in versions:
            if v.get("number") == new_version:
                raw = v.get("created_at", "")
                if not raw:
                    return ReleaseAgeSignals(release_age_hours=None)
                upload_time = parse_upload_time(raw)
                hours = (datetime.now(timezone.utc) - upload_time).total_seconds() / 3600
                return ReleaseAgeSignals(release_age_hours=max(0.0, hours))

        return ReleaseAgeSignals(release_age_hours=None)

    # ------------------------------------------------------------------
    # fetch_maintainer
    # ------------------------------------------------------------------

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerSignals:
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(f"https://rubygems.org/api/v1/versions/{package}.json")
                if resp.status_code != 200:
                    return MaintainerSignals(maintainer_changed=False)
                versions = resp.json()
            except Exception:
                return MaintainerSignals(maintainer_changed=False)

        old_authors: set[str] = set()
        new_authors: set[str] = set()
        for v in versions:
            num = v.get("number", "")
            if num == old_version:
                old_authors = _author_set(v)
            elif num == new_version:
                new_authors = _author_set(v)

        if not old_authors or not new_authors:
            return MaintainerSignals(maintainer_changed=False)

        return MaintainerSignals(maintainer_changed=bool(new_authors - old_authors))

    # ------------------------------------------------------------------
    # get_archive_url
    # ------------------------------------------------------------------

    async def get_archive_url(
        self, client: httpx.AsyncClient, package: str, version: str
    ) -> tuple[str, str, str] | None:
        resp = await client.get(f"https://rubygems.org/api/v1/versions/{package}.json")
        if resp.status_code == 404:
            raise ApplicationError(
                f"{package} not found on RubyGems",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()
        versions = resp.json()

        for v in versions:
            if v.get("number") == version:
                sha256 = v.get("sha", "")
                filename = f"{package}-{version}.gem"
                url = f"https://rubygems.org/gems/{filename}"
                validate_archive_url(url)
                return url, filename, sha256

        return None

    # ------------------------------------------------------------------
    # extract_archive
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # fetch_attestations
    # ------------------------------------------------------------------

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationSignals:
        # RubyGems does not yet support SLSA provenance or Sigstore attestations.
        return AttestationSignals(has_attestation=False)

    # ------------------------------------------------------------------
    # fetch_release
    # ------------------------------------------------------------------

    async def fetch_release(self, package: str, old_version: str, version: str) -> ReleaseSignals:
        import os
        token = os.environ.get("GITHUB_TOKEN")
        async with httpx.AsyncClient(timeout=15.0) as client:
            gem_resp, versions_resp = await asyncio.gather(
                client.get(f"https://rubygems.org/api/v1/gems/{package}.json"),
                client.get(f"https://rubygems.org/api/v1/versions/{package}.json"),
            )

        if gem_resp.status_code != 200:
            return ReleaseSignals()
        gem_data = gem_resp.json()

        source_url = gem_data.get("source_code_uri") or gem_data.get("homepage_uri") or ""
        owner_repo = parse_github_repo(source_url)
        if not owner_repo:
            return ReleaseSignals()

        # Registry timestamp for skew calculation
        registry_time = None
        if versions_resp.status_code == 200:
            for v in versions_resp.json():
                if v.get("number") == version:
                    raw = v.get("created_at", "")
                    if raw:
                        try:
                            registry_time = parse_upload_time(raw)
                        except Exception:  # noqa: BLE001
                            pass
                    break

        owner, repo = owner_repo.split("/", 1)
        release, new_sig, old_sig = await asyncio.gather(
            fetch_github_release(owner, repo, version, token),
            fetch_tag_signature(owner, repo, version, token),
            fetch_tag_signature(owner, repo, old_version, token),
        )
        return (
            build_release_signals(release, registry_time, new_sig, old_sig)
            if release else ReleaseSignals()
        )

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None:
        """Extract a RubyGems .gem file (outer tar → data.tar.gz → source tree)."""
        buf = io.BytesIO(archive_bytes)
        with tarfile.open(fileobj=buf) as outer:
            data_member = next(
                (m for m in outer.getmembers() if m.name == "data.tar.gz"), None
            )
            if data_member is None:
                raise ValueError("No data.tar.gz found in .gem archive")
            data_fobj = outer.extractfile(data_member)
            if data_fobj is None:
                raise ValueError("Could not read data.tar.gz from .gem archive")
            inner_buf = io.BytesIO(data_fobj.read())

        with tarfile.open(fileobj=inner_buf, mode="r:gz") as inner:
            inner.extractall(dest, filter="data")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _author_set(version_data: dict) -> set[str]:
    # "authors" is a comma-separated string like "Alice, Bob"
    raw = (version_data.get("authors") or "").lower()
    return {a.strip() for a in raw.split(",") if a.strip()}
