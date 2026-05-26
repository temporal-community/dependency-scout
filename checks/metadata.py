from ecosystems import get_provider
from helpers.cache import ActivityCache
from models import MetadataChecks
from temporalio import activity

_cache: ActivityCache = ActivityCache(ttl_seconds=3600)  # weekly downloads refresh daily; 1h TTL


@activity.defn(name="activities.metadata.fetch")
async def fetch(ecosystem: str, package: str, old_version: str, new_version: str) -> MetadataChecks:
    """Fetch registry metadata for the package, including weekly download counts, whether the bump is a major version change, and the package description.

    Returns a ``MetadataChecks`` populated from the ecosystem registry (e.g. PyPI, npm)."""
    key = (ecosystem, package, old_version, new_version)
    return await _cache.get_or_compute(
        key,
        lambda: get_provider(ecosystem).fetch_metadata(package, old_version, new_version),
    )
