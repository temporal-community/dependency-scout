"""
Ecosystem abstraction layer.

To add a new ecosystem:
  1. Create activities/ecosystems/{name}.py implementing EcosystemProvider
  2. Add one entry to the registry in get_provider()
  3. Add the ecosystem name to the Literal types in activities/models.py
  4. Add the branch slug to helpers/pr_parser.py's _DEPENDABOT_ECOSYSTEM_MAP
  5. Add a name-validation regex entry in api/webhook.py's _NAME_RE_BY_ECOSYSTEM
"""
from __future__ import annotations

import os
import re
import stat
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx
from temporalio.exceptions import ApplicationError

from activities.models import (
    AttestationSignals,
    MaintainerSignals,
    PyPISignals,
    ReleaseAgeSignals,
    ReleaseSignals,
)

MAX_EXTRACT_BYTES = 100 * 1024 * 1024  # zip bomb guard


class EcosystemProvider(Protocol):
    osv_name: str

    async def fetch_metadata(
        self, package: str, old_version: str, new_version: str
    ) -> PyPISignals: ...

    async def fetch_release_age(
        self, package: str, new_version: str
    ) -> ReleaseAgeSignals: ...

    async def fetch_maintainer(
        self, package: str, old_version: str, new_version: str
    ) -> MaintainerSignals: ...

    async def get_archive_url(
        self, client: httpx.AsyncClient, package: str, version: str
    ) -> tuple[str, str, str] | None: ...

    def extract_archive(self, archive_bytes: bytes, filename: str, dest: str) -> None: ...

    async def fetch_attestations(
        self, package: str, old_version: str, new_version: str
    ) -> AttestationSignals: ...

    async def fetch_release(
        self, package: str, version: str
    ) -> ReleaseSignals: ...


def get_provider(ecosystem: str) -> EcosystemProvider:
    from activities.ecosystems.npm import NpmProvider
    from activities.ecosystems.pip import PipProvider
    from activities.ecosystems.rubygems import RubyGemsProvider

    providers: dict[str, EcosystemProvider] = {
        "pip": PipProvider(),
        "npm": NpmProvider(),
        "rubygems": RubyGemsProvider(),
    }
    if ecosystem not in providers:
        raise ValueError(f"Unknown ecosystem: {ecosystem!r}")
    return providers[ecosystem]


# ---------------------------------------------------------------------------
# Shared utilities used by multiple providers
# ---------------------------------------------------------------------------

ALLOWED_CDN_HOSTS: frozenset[str] = frozenset({
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "rubygems.org",
})


def validate_archive_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ApplicationError(
            f"Insecure archive URL scheme '{parsed.scheme}' — only https is allowed",
            non_retryable=True,
        )
    if parsed.netloc not in ALLOWED_CDN_HOSTS:
        raise ApplicationError(
            f"Untrusted archive host '{parsed.netloc}' — "
            f"expected one of {sorted(ALLOWED_CDN_HOSTS)}",
            non_retryable=True,
        )


def is_major(old: str, new: str) -> bool:
    try:
        return int(new.split(".")[0]) > int(old.split(".")[0])
    except (ValueError, IndexError):
        return False


def parse_upload_time(raw: str) -> datetime:
    raw = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_GITHUB_RE = re.compile(
    r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?(?:[/?#]|$)"
)


def parse_github_repo(url: str) -> str | None:
    """Extract 'owner/repo' from any GitHub URL variant, or None."""
    if not url:
        return None
    m = _GITHUB_RE.search(url)
    return m.group(1) if m else None


async def fetch_github_release(
    owner: str, repo: str, version: str, token: str | None
) -> dict | None:
    """Return the GitHub release JSON for the given version, trying v-prefixed tag first."""
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for tag in (f"v{version}", version):
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    return resp.json()
    except Exception:  # noqa: BLE001
        pass
    return None


def build_release_signals(
    release: dict, registry_time: datetime | None = None
) -> ReleaseSignals:
    """Convert a GitHub release API response into structured ReleaseSignals."""
    author_login: str = (release.get("author") or {}).get("login") or ""
    release_is_automated = "[bot]" in author_login

    skew_minutes: float | None = None
    created_at = release.get("created_at", "")
    if created_at and registry_time is not None:
        try:
            gh_time = parse_upload_time(created_at)
            skew_minutes = round(abs((gh_time - registry_time).total_seconds()) / 60, 1)
        except Exception:  # noqa: BLE001
            pass

    possible_rerelease = False
    published_at = release.get("published_at", "")
    if created_at and published_at and created_at != published_at:
        try:
            delta = (parse_upload_time(published_at) - parse_upload_time(created_at)).total_seconds()
            possible_rerelease = delta > 86_400  # drafted >24h before publishing
        except Exception:  # noqa: BLE001
            pass

    raw_body = release.get("body") or ""
    body: str | None = None
    if raw_body:
        body = (raw_body[:3000] + "\n[release notes truncated]") if len(raw_body) > 3000 else raw_body

    return ReleaseSignals(
        github_release_exists=True,
        release_author=author_login or None,
        release_is_automated=release_is_automated,
        timestamp_skew_minutes=skew_minutes,
        possible_rerelease=possible_rerelease,
        release_body=body,
    )


async def fetch_github_account_age(owner: str) -> int | None:
    """Return age in days of a GitHub user/org account, or None if unavailable.

    Requires GITHUB_TOKEN — skipped without one to avoid unauthenticated rate limits.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://api.github.com/users/{owner}", headers=headers)
            if resp.status_code == 200:
                created_at = resp.json().get("created_at", "")
                if created_at:
                    created = parse_upload_time(created_at)
                    return max(0, (datetime.now(timezone.utc) - created).days)
    except Exception:  # noqa: BLE001
        pass
    return None


def safe_zip_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip with path-traversal, symlink, and zip-bomb protection."""
    total_extracted = 0
    for member in zf.infolist():
        unix_mode = (member.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(unix_mode):
            raise ApplicationError(
                f"Zip contains symlink entry: {member.filename}",
                non_retryable=True,
            )
        member_path = (dest / member.filename).resolve()
        if not str(member_path).startswith(str(dest)):
            raise ApplicationError(
                f"Zip path traversal attempt: {member.filename}",
                non_retryable=True,
            )
        total_extracted += member.file_size
        if total_extracted > MAX_EXTRACT_BYTES:
            raise ApplicationError(
                "Zip extraction size limit exceeded (possible zip bomb)",
                non_retryable=True,
            )
        zf.extract(member, dest)
