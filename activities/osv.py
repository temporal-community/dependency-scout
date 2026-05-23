import httpx
from temporalio import activity

from activities.models import OSVSignals

_ECOSYSTEM_MAP = {"pip": "PyPI", "npm": "npm", "rubygems": "RubyGems"}


@activity.defn(name="activities.osv.check")
async def check(ecosystem: str, package: str, old_version: str, new_version: str) -> OSVSignals:
    osv_ecosystem = _ECOSYSTEM_MAP.get(ecosystem, "PyPI")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.osv.dev/v1/query",
            json={
                "package": {"name": package, "ecosystem": osv_ecosystem},
                "version": new_version,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    vuln_ids: list[str] = []
    for vuln in data.get("vulns", []):
        cves = [a for a in vuln.get("aliases", []) if a.startswith("CVE-")]
        vuln_ids.extend(cves if cves else [vuln["id"]])

    return OSVSignals(osv_vulnerabilities=vuln_ids)
