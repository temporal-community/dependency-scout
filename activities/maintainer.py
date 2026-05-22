import asyncio

import httpx
from temporalio import activity

from activities.models import MaintainerSignals


@activity.defn(name="activities.maintainer.history")
async def history(ecosystem: str, package: str, old_version: str, new_version: str) -> MaintainerSignals:
    async with httpx.AsyncClient(timeout=15.0) as client:
        old_info, new_info = await asyncio.gather(
            _fetch_info(client, package, old_version),
            _fetch_info(client, package, new_version),
        )

    if old_info is None or new_info is None:
        return MaintainerSignals(maintainer_changed=False)

    old_set = _maintainer_set(old_info)
    new_set = _maintainer_set(new_info)
    return MaintainerSignals(maintainer_changed=bool(new_set - old_set))


async def _fetch_info(client: httpx.AsyncClient, package: str, version: str) -> dict | None:
    try:
        resp = await client.get(f"https://pypi.org/pypi/{package}/{version}/json")
        if resp.status_code == 200:
            return resp.json().get("info", {})
    except Exception:
        pass
    return None


def _maintainer_set(info: dict) -> set[str]:
    result = set()
    for field in ("author", "maintainer", "author_email", "maintainer_email"):
        val = (info.get(field) or "").strip().lower()
        if val and val not in ("none", "unknown", ""):
            result.add(val)
    return result
