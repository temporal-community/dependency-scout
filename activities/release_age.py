from datetime import datetime, timezone

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from activities.models import ReleaseAgeSignals


@activity.defn(name="activities.release_age.check")
async def check(ecosystem: str, package: str, old_version: str, new_version: str) -> ReleaseAgeSignals:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"https://pypi.org/pypi/{package}/{new_version}/json")
        if resp.status_code == 404:
            raise ApplicationError(
                f"{package}=={new_version} not found on PyPI",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()
        data = resp.json()

    urls = data.get("urls", [])
    if not urls:
        return ReleaseAgeSignals(release_age_hours=0.0)

    raw = urls[0].get("upload_time_iso_8601") or urls[0].get("upload_time", "")
    if not raw:
        return ReleaseAgeSignals(release_age_hours=0.0)

    upload_time = _parse_upload_time(raw)
    hours = (datetime.now(timezone.utc) - upload_time).total_seconds() / 3600
    return ReleaseAgeSignals(release_age_hours=max(0.0, hours))


def _parse_upload_time(raw: str) -> datetime:
    raw = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
