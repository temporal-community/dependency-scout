import asyncio
import os

from temporalio import activity
from temporalio.exceptions import ApplicationError

from models import SocketChecks
from helpers.cache import ActivityCache
from helpers.http import get_client

_cache: ActivityCache = ActivityCache(ttl_seconds=3600)  # scores can be updated; refresh hourly

# Cap concurrent Socket API calls to stay within rate limits.
# 25 parallel workflows would otherwise fire 25 simultaneous requests.
_semaphore = asyncio.Semaphore(5)

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

    key = (ecosystem, package, new_version)
    return await _cache.get_or_compute(
        key,
        lambda: _fetch_socket(api_key, ecosystem, package, new_version),
    )


async def _fetch_socket(api_key: str, ecosystem: str, package: str, version: str) -> SocketChecks:
    ecosystem_slug = _ECOSYSTEM_MAP.get(ecosystem, "pypi")
    purl = f"pkg:{ecosystem_slug}/{package}@{version}"
    client = get_client()

    async with _semaphore:
        resp = await client.post(
            "https://api.socket.dev/v0/purl",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"components": [{"purl": purl}]},
            params={"alerts": "true"},
            timeout=15.0,
        )

    if resp.status_code == 401:
        raise ApplicationError(
            "Socket API auth failed — check SOCKET_API_KEY",
            non_retryable=True,
        )
    if resp.status_code == 404:
        activity.logger.info(f"{package}@{version} not found in Socket database")
        return SocketChecks(socket_score=None, socket_alerts=[])
    if resp.status_code == 429:
        raise ApplicationError("Socket API rate limited", non_retryable=False)

    resp.raise_for_status()

    packages = resp.json().get("packages", [])
    if not packages:
        return SocketChecks(socket_score=None, socket_alerts=[])

    pkg = packages[0]
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

    activity.logger.info(f"Socket: {package}@{version} score={socket_score} alerts={len(alerts)}")
    return SocketChecks(
        socket_score=socket_score,
        socket_alerts=alerts,
        socket_alert_types=alert_types,
    )
