"""
Activity: compute a security-focused diff between two PyPI package versions.
Downloads both sdists, extracts them, and returns a DiffSignals model.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import io
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from activities.models import DiffSignals

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_EXTRACT_BYTES = 100 * 1024 * 1024  # 100 MB — zip bomb guard
MAX_DIFF_BYTES = 100 * 1024  # 100 KB

NOISE_DIRS = {".dist-info", "__pycache__", ".egg-info"}
NOISE_SUFFIXES = {".pyc", ".pyo"}
NOISE_FILENAMES = {"RECORD", "WHEEL", "METADATA", "INSTALLER"}

HIGH_SIGNAL_NAMES = {
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "__init__.py",
    "package.json",
    "install.js",
    "postinstall.js",
    "preinstall.js",
}
HIGH_SIGNAL_SUFFIXES = {".pth"}  # Python path config files — silent code-exec vector


# ---------------------------------------------------------------------------
# Activity entry point
# ---------------------------------------------------------------------------

@activity.defn(name="activities.package_diff.compute")
async def compute(ecosystem: str, package: str, old_version: str, new_version: str) -> DiffSignals:
    activity.logger.info(
        f"Computing package diff for {package} {old_version} -> {new_version}"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        old_info, new_info = await asyncio.gather(
            _get_sdist_url(client, package, old_version),
            _get_sdist_url(client, package, new_version),
        )

        if old_info is None or new_info is None:
            return DiffSignals(diff_summary="[sdist not available]", diff_size_bytes=0)

        old_url, old_filename, old_sha256 = old_info
        new_url, new_filename, new_sha256 = new_info

        old_bytes, new_bytes = await asyncio.gather(
            _download(client, old_url, old_sha256),
            _download(client, new_url, new_sha256),
        )

    if old_bytes is None or new_bytes is None:
        return DiffSignals(
            diff_summary="[download aborted: archive exceeds 20 MB size limit]",
            diff_size_bytes=0,
        )

    # Extraction and diff are CPU/blocking I/O — run in a thread.
    diff_summary = await asyncio.to_thread(
        _extract_and_diff, old_bytes, old_filename, new_bytes, new_filename
    )

    return DiffSignals(
        diff_summary=diff_summary,
        diff_size_bytes=len(diff_summary.encode()),
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get_sdist_url(
    client: httpx.AsyncClient, package: str, version: str
) -> tuple[str, str, str] | None:
    """Return (url, filename, sha256) for the best available archive, or None."""
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    resp = await client.get(url)
    if resp.status_code == 404:
        raise ApplicationError(
            f"{package}=={version} not found on PyPI",
            type="PackageNotFound",
            non_retryable=True,
        )
    resp.raise_for_status()
    data = resp.json()
    urls: list[dict] = data.get("urls", [])

    # Prefer sdist, fall back to wheel
    for pkg_type in ("sdist", "bdist_wheel"):
        for entry in urls:
            if entry.get("packagetype") == pkg_type:
                return entry["url"], entry["filename"], entry.get("digests", {}).get("sha256", "")

    return None


async def _download(client: httpx.AsyncClient, url: str, expected_sha256: str) -> bytes | None:
    """Download *url*, verify SHA256, return bytes or None if oversized."""
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes(chunk_size=65536):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                return None
            chunks.append(chunk)
    data = b"".join(chunks)

    if expected_sha256:
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise ApplicationError(
                f"SHA256 mismatch for {url}: expected {expected_sha256}, got {actual}",
                non_retryable=True,
            )

    return data


# ---------------------------------------------------------------------------
# Synchronous extraction + diff (runs in asyncio.to_thread)
# ---------------------------------------------------------------------------

def _extract_and_diff(
    old_bytes: bytes,
    old_filename: str,
    new_bytes: bytes,
    new_filename: str,
) -> str:
    try:
        with tempfile.TemporaryDirectory() as old_dir, tempfile.TemporaryDirectory() as new_dir:
            _extract_to_dir(old_bytes, old_filename, old_dir)
            _extract_to_dir(new_bytes, new_filename, new_dir)
            old_map = _get_file_map(old_dir)
            new_map = _get_file_map(new_dir)
            return _build_diff(old_map, new_map)
    except Exception as exc:  # noqa: BLE001
        return f"[extraction error: {exc}]"


def _extract_to_dir(archive_bytes: bytes, filename: str, dest: str) -> None:
    """Extract *archive_bytes* (named *filename*) into *dest*."""
    buf = io.BytesIO(archive_bytes)
    dest_path = Path(dest).resolve()
    lower = filename.lower()
    if lower.endswith((".tar.gz", ".tar.bz2", ".tgz")):
        with tarfile.open(fileobj=buf) as tf:
            tf.extractall(dest, filter="data")  # filter="data" blocks path traversal
    elif lower.endswith((".whl", ".zip")):
        with zipfile.ZipFile(buf) as zf:
            _safe_zip_extractall(zf, dest_path)
    else:
        raise ValueError(f"Unsupported archive format: {filename}")


def _safe_zip_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip file with path traversal protection and extraction size cap."""
    total_extracted = 0
    for member in zf.infolist():
        # Normalize and check for path traversal
        member_path = (dest / member.filename).resolve()
        if not str(member_path).startswith(str(dest)):
            raise ApplicationError(
                f"Zip path traversal attempt: {member.filename}",
                non_retryable=True,
            )
        # Guard against zip bombs
        total_extracted += member.file_size
        if total_extracted > MAX_EXTRACT_BYTES:
            raise ApplicationError(
                "Zip extraction size limit exceeded (possible zip bomb)",
                non_retryable=True,
            )
        zf.extract(member, dest)


def _is_noise(rel: str) -> bool:
    """Return True if this path should be excluded from the diff."""
    parts = Path(rel).parts
    # Check directory components for noise patterns
    for part in parts[:-1]:
        if part in NOISE_DIRS:
            return True
        if part.endswith(".egg-info") or part.endswith(".dist-info"):
            return True
    # Check filename
    name = parts[-1] if parts else ""
    if name in NOISE_FILENAMES:
        return True
    if Path(name).suffix in NOISE_SUFFIXES:
        return True
    if Path(name).suffix in HIGH_SIGNAL_SUFFIXES:
        return False  # explicitly keep high-signal suffixes like .pth
    return False


def _get_file_map(base_dir: str) -> dict[str, Path]:
    """
    Walk *base_dir* and return {relative_path_str: absolute_Path}.

    For sdists the top-level directory (e.g. ``requests-2.32.0/``) is stripped
    so that paths are comparable across versions.
    """
    base = Path(base_dir)
    result: dict[str, Path] = {}

    all_files = list(base.rglob("*"))
    # Detect top-level directory to strip (sdist convention)
    top_level_dirs = {p.relative_to(base).parts[0] for p in all_files if p.relative_to(base).parts}
    strip_top = len(top_level_dirs) == 1  # single top-level dir → strip it

    for path in all_files:
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        parts = rel.parts
        if strip_top and len(parts) > 1:
            rel_str = str(Path(*parts[1:]))
        elif strip_top and len(parts) == 1:
            # File directly in the top-level dir — skip (rare)
            continue
        else:
            rel_str = str(rel)

        if _is_noise(rel_str):
            continue
        result[rel_str] = path

    return result


def _build_diff(old_map: dict[str, Path], new_map: dict[str, Path]) -> str:
    old_keys = set(old_map)
    new_keys = set(new_map)

    new_files = sorted(new_keys - old_keys)
    changed = sorted(old_keys & new_keys)

    high_signal_changed: list[tuple[str, str]] = []
    other_changed: list[str] = []

    for rel in changed:
        old_text = _read_text(old_map[rel])
        new_text = _read_text(new_map[rel])
        if old_text == new_text:
            continue
        name = Path(rel).name
        if name in HIGH_SIGNAL_NAMES or Path(name).suffix in HIGH_SIGNAL_SUFFIXES:
            patch = _unified_diff(old_text, new_text, rel)
            high_signal_changed.append((rel, patch))
        else:
            other_changed.append(rel)

    # New files — treat all as high-signal
    new_file_lines: list[str] = []
    for rel in new_files:
        new_file_lines.append(f"+ {rel}")

    sections: list[str] = []

    if new_file_lines:
        sections.append("=== NEW FILES ===\n" + "\n".join(new_file_lines))

    if high_signal_changed:
        parts = []
        for rel, patch in high_signal_changed:
            parts.append(patch)
        sections.append("=== CHANGED (high-signal) ===\n" + "\n".join(parts))

    if other_changed:
        sections.append("=== CHANGED (other) ===\n" + ", ".join(other_changed))

    if not sections:
        return "[no significant changes detected]"

    result = "\n\n".join(sections)

    total_bytes = len(result.encode())
    if total_bytes > MAX_DIFF_BYTES:
        truncated = result.encode()[:MAX_DIFF_BYTES].decode(errors="replace")
        result = truncated + f"\n[diff truncated at 100KB — {total_bytes} bytes total]"

    return result


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _unified_diff(old_text: str, new_text: str, filename: str) -> str:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"{filename} (old)",
        tofile=f"{filename} (new)",
    )
    return "".join(diff)
