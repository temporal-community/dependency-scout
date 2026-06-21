from temporalio import activity

from ecosystems import get_meta
from models import OSVChecks
from helpers.cache import ActivityCache
from helpers.http import get_client

_cache: ActivityCache = ActivityCache(ttl_seconds=3600)  # new CVEs can appear; refresh hourly


@activity.defn(name="activities.osv.check")
async def check(ecosystem: str, package: str, old_version: str, new_version: str) -> OSVChecks:
    """Query the OSV.dev API to find published vulnerabilities affecting the new version of this package.

    Returns an ``OSVChecks`` containing a list of CVE or OSV IDs for any matching advisories."""
    key = (ecosystem, package, new_version)
    return await _cache.get_or_compute(key, lambda: _do_check(ecosystem, package, new_version))


async def _do_check(ecosystem: str, package: str, new_version: str) -> OSVChecks:
    meta = get_meta(ecosystem)
    osv_ecosystem = meta.osv_name if meta else ""
    client = get_client()
    resp = await client.post(
        "https://api.osv.dev/v1/query",
        json={
            "package": {"name": package, "ecosystem": osv_ecosystem},
            "version": new_version,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()

    vuln_ids: list[str] = []
    fixed_versions: list[str] = []
    for vuln in data.get("vulns", []):
        cves = [a for a in vuln.get("aliases", []) if a.startswith("CVE-")]
        vuln_ids.extend(cves if cves else [vuln["id"]])
        # Collect the "fixed" version from each advisory's range events (for the queried
        # package) so the classifier can say "upgrade to a patched release" — no LLM needed.
        for affected in vuln.get("affected", []):
            if affected.get("package", {}).get("name", "").lower() != package.lower():
                continue
            for rng in affected.get("ranges", []):
                for event in rng.get("events", []):
                    if event.get("fixed"):
                        fixed_versions.append(event["fixed"])

    return OSVChecks(
        osv_vulnerabilities=vuln_ids,
        osv_fixed_versions=_sorted_versions(set(fixed_versions)),
    )


def _sorted_versions(versions: set[str]) -> list[str]:
    """Sort version strings low→high, tolerating non-PEP440 strings (fall back to text)."""
    from packaging.version import InvalidVersion, Version

    def key(v: str) -> tuple[int, object]:
        try:
            return (0, Version(v))
        except InvalidVersion:
            return (1, v)

    return sorted(versions, key=key)
