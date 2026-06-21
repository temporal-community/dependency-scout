"""
Remediators — apply a dependency fix by regenerating a project's lockfile.

A `Remediator` knows how, for one ecosystem, to move a (possibly transitive) package
to a safe version *script-safely* — i.e. by resolving and rewriting the lockfile only,
never executing install/lifecycle scripts. This is Scout's one repo-mutating capability,
so it is opt-in and degrades gracefully: ecosystems without a remediator simply escalate
(label + @-mention + close) as before.

This is a pluggable extension point alongside `ecosystems/`, `platforms/`, and
`classifiers/`. Phase 1 ships uv/pip; more ecosystems register here over time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class RemediationResult:
    """Outcome of attempting to move `package` to `target_version` in one project."""

    project_dir: str  # path of the project within the repo (relative)
    package: str
    old_version: str  # locked version before
    new_version: str  # locked version after the resolve
    target_version: str  # the safe version we were trying to reach
    changed: bool  # did the lockfile actually move
    reached_target: bool  # is new_version >= target_version (the fix landed)
    lockfile: str  # lockfile filename that changed (e.g. "uv.lock")
    message: str  # human-readable note — especially the blocker reason when not reached


class Remediator(Protocol):
    """Per-ecosystem lockfile remediation. Implementations MUST be script-safe (resolve and
    rewrite the lockfile only — never run install/lifecycle scripts)."""

    lockfile_name: str  # the lockfile this remediator regenerates, e.g. "uv.lock"

    def affects(self, lockfile_text: str, package: str) -> bool:
        """True if `package` appears in this lockfile (so the project is worth remediating)."""
        ...

    def remediate(self, project_dir: Path, package: str, target_version: str) -> RemediationResult:
        """Resolve `package` up to a safe version in `project_dir`'s lockfile, in place."""
        ...


def get_remediator(ecosystem: str) -> Remediator | None:
    """Return the remediator for an ecosystem, or None if remediation isn't supported yet
    (caller falls back to escalate-only)."""
    if ecosystem in ("pip", "uv"):
        from remediators.uv import UvRemediator

        return UvRemediator()
    return None
