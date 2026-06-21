import json
import os

from temporalio import activity
from temporalio.exceptions import ApplicationError

from models import SocketChecks
from helpers.batcher import CoalescingBatcher
from helpers.cache import ActivityCache
from helpers.http import get_client

_cache: ActivityCache = ActivityCache(ttl_seconds=3600)  # scores can be updated; refresh hourly

_ECOSYSTEM_MAP = {
    "pip": "pypi",
    "npm": "npm",
    "rubygems": "gem",
    "cargo": "cargo",
    "nuget": "nuget",
}
_INCLUDE_SEVERITIES = {"critical", "high"}

# Alert types significant enough to surface even at medium severity.
# Socket sometimes rates malware/obfuscation as "medium" on first detection.
_MEDIUM_INCLUDE_TYPES = {
    "malware",
    "protestware",
    "obfuscatedCode",
    "shellAccess",
    "networkAccess",
    "envVars",
    "installScripts",
    "dynamicRequire",
    "binScriptConfusion",
    "changedAuthor",
}


def _parse_components(body: str) -> list[dict]:
    """Parse the Socket /v0/purl response body into a list of component objects.

    The endpoint streams **NDJSON** — one component object per line — so
    ``resp.json()`` raises ``JSONDecodeError: Extra data`` on any multi-component
    (or multi-line) response. Parse line by line instead. Blank lines are
    skipped; a line wrapping its results as ``{"packages": [...]}`` is unwrapped,
    so a single non-streamed JSON document is tolerated too.
    """
    components: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and "packages" in obj:
            components.extend(obj["packages"])
        elif isinstance(obj, dict):
            components.append(obj)
    return components


@activity.defn(name="activities.socket.score")
async def score(ecosystem: str, package: str, old_version: str, new_version: str) -> SocketChecks:
    """Query the Socket.dev API for the new version's supply chain risk score and any specific alerts such as install scripts, obfuscated code, or network access.

    Returns a ``SocketChecks`` with a 0–100 score and a filtered list of high/critical alert messages; requires a ``SOCKET_API_KEY`` environment variable."""
    api_key = os.environ.get("SOCKET_API_KEY")
    if not api_key:
        activity.logger.info(
            "No SOCKET_API_KEY — skipping Socket score (treated as yellow indicator)"
        )
        return SocketChecks(socket_score=None, socket_alerts=[])

    if ecosystem not in _ECOSYSTEM_MAP:
        # Socket scores packages from specific registries. For ecosystems we don't map
        # (GitHub Actions, Go, Maven, …) the old code silently queried Socket as if the
        # name were a PyPI package — wrong (a same-named PyPI package returns bogus data)
        # and wasteful (each /v0/purl call costs ~100 quota units). Skip instead.
        activity.logger.info(
            "Socket has no mapping for ecosystem %r — skipping Socket signal for %s",
            ecosystem,
            package,
        )
        return SocketChecks(socket_score=None, socket_alerts=[])

    # Coalesced across the sweep: many concurrent score() calls become ONE /v0/purl
    # request (see _socket_batch). The cache still dedups identical bumps across windows.
    key = (ecosystem, package, new_version)
    return await _cache.get_or_compute(key, lambda: _socket_batcher.load(key))


def _component_to_checks(pkg: dict) -> SocketChecks:
    """Build a SocketChecks from one Socket component object (score + filtered alerts)."""
    depscore = pkg.get("score", {}).get("depscore")
    socket_score = round(depscore * 100) if depscore is not None else None

    def _include(a: dict) -> bool:
        sev = a.get("severity", "")
        typ = a.get("type", "")
        return sev in _INCLUDE_SEVERITIES or (sev == "medium" and typ in _MEDIUM_INCLUDE_TYPES)

    included = [a for a in pkg.get("alerts", []) if _include(a)]
    alerts = [
        f"[{a['severity']}] {a.get('type', 'unknown')}: {a.get('message', '').strip()}"
        for a in included
    ]
    alert_types = list({a.get("type", "unknown") for a in included})
    return SocketChecks(
        socket_score=socket_score, socket_alerts=alerts, socket_alert_types=alert_types
    )


async def _socket_batch(
    keys: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], SocketChecks]:
    """Score every (ecosystem, package, version) key in a SINGLE /v0/purl request.

    Coalesced by _socket_batcher so a whole sweep makes one Socket call instead of one per
    package — Socket meters /v0/purl against a quota window, so per-package calls exhaust it
    fast. On a batch-wide failure (429 quota / network) every key degrades to an empty
    result rather than failing the triage; auth errors still surface."""
    api_key = os.environ.get("SOCKET_API_KEY", "")
    components = [
        {"purl": f"pkg:{_ECOSYSTEM_MAP[eco]}/{package}@{version}"}
        for (eco, package, version) in keys
    ]
    resp = await get_client().post(
        "https://api.socket.dev/v0/purl",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"components": components},
        params={"alerts": "true"},
        timeout=20.0,
    )

    empty = {k: SocketChecks(socket_score=None, socket_alerts=[]) for k in keys}

    if resp.status_code == 401:
        raise ApplicationError("Socket API auth failed — check SOCKET_API_KEY", non_retryable=True)
    if resp.status_code == 429:
        # Socket meters /v0/purl against a quota window (GET /v0/quota); a 429 is almost
        # always quota exhaustion that won't clear within a retry budget. Degrade (not retry)
        # and log Socket's own reason — it distinguishes per-key quota from a per-IP limit
        # (the latter common on shared GitHub-hosted runner IPs even when quota is fine).
        activity.logger.warning(
            "Socket API 429 for %d package(s) (retry-after=%s) — skipping Socket signal. Reason: %s",
            len(keys),
            resp.headers.get("retry-after") or "unset",
            resp.text[:300] or "(empty body)",
        )
        return empty
    if resp.status_code == 404:
        return empty

    resp.raise_for_status()
    parsed = _parse_components(resp.text)

    # If we asked for one and got one back, it's unambiguous. Otherwise map each component
    # to its key by (type, lowercased name, version) regardless of response order; a miss
    # degrades to empty (a miss, never a wrong package's data).
    if len(keys) == 1 and len(parsed) == 1:
        return {keys[0]: _component_to_checks(parsed[0])}

    by_id: dict[tuple[str, str, str], dict] = {
        (obj.get("type", ""), str(obj.get("name", "")).lower(), str(obj.get("version", ""))): obj
        for obj in parsed
    }
    out: dict[tuple[str, str, str], SocketChecks] = {}
    for eco, package, version in keys:
        obj = by_id.get((_ECOSYSTEM_MAP[eco], package.lower(), version))
        out[(eco, package, version)] = (
            _component_to_checks(obj) if obj else empty[(eco, package, version)]
        )
    return out


# Coalesce concurrent per-package score() calls into one Socket request per ~150ms window.
_socket_batcher = CoalescingBatcher(_socket_batch, max_batch=100, window_seconds=0.15)
