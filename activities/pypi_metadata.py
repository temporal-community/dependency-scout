import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from activities.models import PyPISignals


@activity.defn(name="activities.pypi_metadata.fetch")
async def fetch(ecosystem: str, package: str, old_version: str, new_version: str) -> PyPISignals:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"https://pypi.org/pypi/{package}/{new_version}/json")
        if resp.status_code == 404:
            raise ApplicationError(
                f"{package}=={new_version} not found on PyPI",
                type="PackageNotFound",
                non_retryable=True,
            )
        resp.raise_for_status()

        weekly_downloads = await _fetch_weekly_downloads(client, package)

    return PyPISignals(
        weekly_downloads=weekly_downloads,
        publish_account_age_days=None,  # PyPI API doesn't expose account creation dates
        is_major_bump=_is_major(old_version, new_version),
    )


async def _fetch_weekly_downloads(client: httpx.AsyncClient, package: str) -> int | None:
    try:
        resp = await client.get(
            f"https://pypistats.org/api/packages/{package.lower()}/recent",
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 200:
            return resp.json()["data"]["last_week"]
    except Exception:
        pass
    return None


def _is_major(old: str, new: str) -> bool:
    try:
        return int(new.split(".")[0]) > int(old.split(".")[0])
    except (ValueError, IndexError):
        return False
