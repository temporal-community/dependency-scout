"""Query the NIST National Vulnerability Database (NVD) for CVEs affecting the new version.

NVD frequently publishes (and analyzes) a CVE days before it propagates into OSV's
aggregated feeds. Querying NVD directly catches those freshly-disclosed vulnerabilities
that activities.osv.check would still miss — closing the window where a known-vulnerable
bump looks clean simply because OSV hasn't ingested the advisory yet.

Matching is by CPE product name (the package name); NVD does the version-range matching
server-side via ``virtualMatchString``. Package-name → CPE-product is approximate (the
vendor is wildcarded and names don't always line up), so a non-match yields an empty result
— a miss, never a false positive.
"""

import os

import httpx
from temporalio import activity

from models import NVDChecks
from helpers.cache import ActivityCache
from helpers.http import get_client

_cache: ActivityCache = ActivityCache(ttl_seconds=3600)  # new CVEs can appear; refresh hourly

_NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"


@activity.defn(name="activities.nvd.check")
async def check(ecosystem: str, package: str, old_version: str, new_version: str) -> NVDChecks:
    """Return CVE IDs from NVD that affect ``new_version`` of this package.

    Caches per (package, new_version) — the NVD query depends only on those, not on the
    ecosystem. An ``NVD_API_KEY`` environment variable is optional but raises NVD's rate
    limit from 5 to 50 requests / 30s.

    NVD is supplementary to OSV (activities.osv.check), and NVD's API is "as-is, as-
    available" (per its Terms of Use) — frequently slow or rate-limited, especially without
    a key on shared CI IPs. So any network/HTTP/parse error degrades to "no CVEs found"
    rather than failing the whole triage — a miss, never a false positive. The failure is
    not cached, so a later call retries."""
    key = (package, new_version)
    try:
        return await _cache.get_or_compute(key, lambda: _do_check(package, new_version))
    except (httpx.HTTPError, ValueError) as exc:
        activity.logger.warning(
            "nvd: lookup failed for %s@%s (%s) — skipping NVD signal. "
            "Set NVD_API_KEY to raise NVD's rate limit (5→50 req/30s).",
            package,
            new_version,
            type(exc).__name__,
        )
        return NVDChecks(nvd_vulnerabilities=[])


async def _do_check(package: str, new_version: str) -> NVDChecks:
    # NVD CPE product names are lowercase; vendor is wildcarded since we can't map it reliably.
    match = f"cpe:2.3:a:*:{package.lower()}:{new_version}:*:*:*:*:*:*:*"
    headers: dict[str, str] = {}
    if api_key := os.environ.get("NVD_API_KEY"):
        headers["apiKey"] = api_key

    client = get_client()
    resp = await client.get(
        _NVD_API,
        params={"virtualMatchString": match},
        headers=headers,
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()

    cve_ids: list[str] = []
    for item in data.get("vulnerabilities", []):
        cve_id = item.get("cve", {}).get("id")
        if cve_id:
            cve_ids.append(cve_id)

    if cve_ids:
        activity.logger.info(
            "nvd: %s@%s matched %d CVE(s): %s",
            package,
            new_version,
            len(cve_ids),
            ", ".join(cve_ids[:5]),
        )
    return NVDChecks(nvd_vulnerabilities=cve_ids)
