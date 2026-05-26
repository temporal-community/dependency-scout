from temporalio import activity

from ecosystems import get_provider
from helpers.cache import ActivityCache
from models import ReleaseChecks

_cache: ActivityCache = ActivityCache(
    ttl_seconds=3600
)  # VCS releases are immutable; 1h TTL lets late-published releases through on retry


@activity.defn(name="activities.release_notes.check")
async def check(ecosystem: str, package: str, old_version: str, new_version: str) -> ReleaseChecks:
    """Fetch the GitHub or GitLab release for the new version and check whether the release tag is cryptographically signed and whether the publish time matches the registry upload time.

    Returns a ``ReleaseChecks`` with the release notes text and integrity flags."""
    key = (ecosystem, package, old_version, new_version)
    return await _cache.get_or_compute(
        key,
        lambda: get_provider(ecosystem).fetch_release(package, old_version, new_version),
    )
