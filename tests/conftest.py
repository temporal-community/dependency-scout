import socket as _socket

import pytest
from helpers.batcher import reset_all_batchers
from helpers.cache import clear_all_caches
from models import (
    AttestationChecks,
    PackageDiffChecks,
    OSVChecks,
    PackageChecks,
    MetadataChecks,
    ReleaseAgeChecks,
    SocketChecks,
)

# Loopback only — real HTTP must be mocked with respx, but the embedded Temporal
# dev server (and any local fixture server) talks over localhost and must work.
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Fail loudly if a test opens a real (non-loopback) network connection.

    Every external HTTP call in the suite must go through a respx mock; respx
    intercepts at the httpx transport layer, above the socket, so mocked calls
    never reach here. This guard only bites when a call escapes mocking and would
    otherwise hit — and hammer — a real, rate-limited, possibly paid API
    (Socket.dev, OSV, GitHub). Block at the socket layer so it catches both sync
    and async clients regardless of how the request is built.
    """
    real_connect = _socket.socket.connect

    def _host_of(address) -> str:
        return address[0] if isinstance(address, tuple) else str(address)

    def _guard(self, address, *args, **kwargs):
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            raise RuntimeError(
                f"Real network connection blocked in tests: {host}\n"
                "Mock the endpoint with @respx.mock (see tests/test_socket.py)."
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(_socket.socket, "connect", _guard)


@pytest.fixture(autouse=True)
def reset_activity_caches():
    """Clear all ActivityCache instances before each test.

    Prevents cache hits from earlier tests from masking expected HTTP calls
    or error paths in later tests.
    """
    clear_all_caches()
    reset_all_batchers()
    yield
    clear_all_caches()
    reset_all_batchers()


@pytest.fixture
def base_signals():
    return PackageChecks(
        ecosystem="pip",
        package_name="requests",
        old_version="2.31.0",
        new_version="2.32.0",
        metadata=MetadataChecks(weekly_downloads=5_000_000, is_major_bump=False),
        socket=SocketChecks(socket_score=80, socket_alerts=[]),
        osv=OSVChecks(osv_vulnerabilities=[]),
        diff=PackageDiffChecks(diff_summary="Minor internal refactor.", diff_size_bytes=512),
        age=ReleaseAgeChecks(release_age_hours=200.0),
        attestation=AttestationChecks(publisher_account_age_days=1800),
    )
