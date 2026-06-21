import asyncio
import json

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from checks.socket import score

PURL_URL = "https://api.socket.dev/v0/purl"


def _component(
    depscore: float,
    alerts: list[dict],
    *,
    name: str = "requests",
    version: str = "2.32.0",
    ptype: str = "pypi",
) -> dict:
    # Mirror a real Socket component: type/name/version are what the batch mapping keys on.
    return {
        "type": ptype,
        "name": name,
        "version": version,
        "purl": f"pkg:{ptype}/{name}@{version}",
        "score": {"depscore": depscore},
        "alerts": alerts,
    }


def _socket_response(depscore: float, alerts: list[dict]) -> str:
    """Real Socket /v0/purl response: NDJSON, one component object per line."""
    return _ndjson(_component(depscore, alerts))


def _ndjson(*components: dict) -> str:
    return "\n".join(json.dumps(c) for c in components)


async def test_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("SOCKET_API_KEY", raising=False)
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert result.socket_score is None
    assert result.socket_alerts == []


@respx.mock
async def test_score_and_alerts_parsed(monkeypatch):
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(
        return_value=httpx.Response(
            200,
            content=_socket_response(
                depscore=0.72,
                alerts=[
                    {
                        "severity": "high",
                        "type": "install-scripts",
                        "message": "Runs code at install time",
                    },
                    {
                        "severity": "critical",
                        "type": "obfuscated-code",
                        "message": "Base64-encoded payload",
                    },
                    {
                        "severity": "low",
                        "type": "env-vars",
                        "message": "Reads environment variables",
                    },
                ],
            ),
        )
    )
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert result.socket_score == 72
    # Only high/critical included
    assert len(result.socket_alerts) == 2
    assert any("install-scripts" in a for a in result.socket_alerts)
    assert any("obfuscated-code" in a for a in result.socket_alerts)
    assert not any("env-vars" in a for a in result.socket_alerts)


@respx.mock
async def test_batch_coalesces_concurrent_packages_into_one_request(monkeypatch):
    """Concurrent score() calls for distinct packages coalesce into ONE /v0/purl request,
    and each package is mapped back to its own component (by type/name/version). Also
    exercises multi-line NDJSON parsing — the original resp.json() crashed on line 2."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    body = _ndjson(
        _component(0.72, [], name="requests", version="2.32.0"),
        _component(0.50, [], name="flask", version="3.0.0"),
        _component(0.90, [], name="django", version="5.0.0"),
    )
    route = respx.post(PURL_URL).mock(return_value=httpx.Response(200, content=body))

    env = ActivityEnvironment()
    results = await asyncio.gather(
        env.run(score, "pip", "requests", "2.31.0", "2.32.0"),
        env.run(score, "pip", "flask", "2.0.0", "3.0.0"),
        env.run(score, "pip", "django", "4.0.0", "5.0.0"),
    )

    assert route.call_count == 1  # one Socket request for all three packages
    sent = json.loads(route.calls[0].request.content)
    assert len(sent["components"]) == 3
    by_score = sorted(r.socket_score for r in results)
    assert by_score == [50, 72, 90]  # each package mapped to its own component


@respx.mock
async def test_empty_body_returns_empty(monkeypatch):
    """An empty (no components) NDJSON body yields an empty result, not a crash."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(return_value=httpx.Response(200, content=""))
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert result.socket_score is None
    assert result.socket_alerts == []


@respx.mock
async def test_score_converted_to_0_100(monkeypatch):
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(
        return_value=httpx.Response(200, content=_socket_response(depscore=0.856, alerts=[]))
    )
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert result.socket_score == 86  # round(0.856 * 100)


@respx.mock
async def test_package_not_found_returns_empty(monkeypatch):
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "obscure-pkg", "1.0.0", "1.0.1")
    assert result.socket_score is None
    assert result.socket_alerts == []


@respx.mock
async def test_auth_failure_raises_non_retryable(monkeypatch):
    monkeypatch.setenv("SOCKET_API_KEY", "bad-key")
    respx.post(PURL_URL).mock(return_value=httpx.Response(401))
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert exc_info.value.non_retryable is True


@respx.mock
async def test_empty_packages_list_returns_empty(monkeypatch):
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(return_value=httpx.Response(200, json={"packages": []}))
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert result.socket_score is None


@respx.mock
async def test_purl_uses_correct_ecosystem(monkeypatch):
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    route = respx.post(PURL_URL).mock(
        return_value=httpx.Response(200, content=_socket_response(0.9, []))
    )
    env = ActivityEnvironment()
    await env.run(score, "npm", "express", "4.18.1", "4.18.2")
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["components"][0]["purl"] == "pkg:npm/express@4.18.2"


@respx.mock
async def test_rate_limited_degrades_to_empty(monkeypatch):
    """A 429 (quota exhausted) degrades to no-signal rather than raising/retrying —
    a quota-window 429 won't clear within a retry budget, and retrying just hammers
    an already-limited key."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    route = respx.post(PURL_URL).mock(
        return_value=httpx.Response(429, headers={"retry-after": "3600"})
    )
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert result.socket_score is None
    assert result.socket_alerts == []
    assert route.call_count == 1  # no retry storm


@respx.mock
async def test_unmapped_ecosystem_skips_without_api_call(monkeypatch):
    """Ecosystems Socket doesn't map (github_actions, go, …) are skipped entirely — no
    API call (no wasted quota), and no mis-query as a PyPI package."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    route = respx.post(PURL_URL).mock(return_value=httpx.Response(200, content="{}"))
    env = ActivityEnvironment()
    result = await env.run(score, "github_actions", "actions/checkout", "4.3.1", "6.0.3")
    assert result.socket_score is None
    assert result.socket_alerts == []
    assert route.call_count == 0  # never hit the Socket API


@respx.mock
async def test_alert_types_captured(monkeypatch):
    """socket_alert_types is populated with the raw type names of included alerts."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(
        return_value=httpx.Response(
            200,
            content=_socket_response(
                depscore=0.5,
                alerts=[
                    {"severity": "critical", "type": "malware", "message": "Malicious code"},
                    {"severity": "high", "type": "installScripts", "message": "Install script"},
                    {"severity": "low", "type": "noReadme", "message": "No README"},
                ],
            ),
        )
    )
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert "malware" in result.socket_alert_types
    assert "installScripts" in result.socket_alert_types
    assert "noReadme" not in result.socket_alert_types  # low severity, not included


@respx.mock
async def test_medium_severity_high_signal_type_included(monkeypatch):
    """Medium-severity alerts for high-signal types (malware, obfuscatedCode, etc.) are included."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(
        return_value=httpx.Response(
            200,
            content=_socket_response(
                depscore=0.6,
                alerts=[
                    {"severity": "medium", "type": "malware", "message": "Possible malware"},
                    {"severity": "medium", "type": "networkAccess", "message": "Outbound call"},
                    {"severity": "medium", "type": "noReadme", "message": "No README"},
                ],
            ),
        )
    )
    env = ActivityEnvironment()
    result = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert any("malware" in a for a in result.socket_alerts)
    assert any("networkAccess" in a for a in result.socket_alerts)
    assert not any("noReadme" in a for a in result.socket_alerts)  # medium but not high-signal type
    assert "malware" in result.socket_alert_types


@respx.mock
async def test_rubygems_ecosystem_uses_gem_purl(monkeypatch):
    """rubygems ecosystem maps to 'gem' in the Socket purl."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    route = respx.post(PURL_URL).mock(
        return_value=httpx.Response(200, content=_socket_response(0.8, []))
    )
    env = ActivityEnvironment()
    await env.run(score, "rubygems", "rails", "7.0.0", "7.0.1")
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["components"][0]["purl"] == "pkg:gem/rails@7.0.1"


@respx.mock
async def test_cargo_ecosystem_uses_cargo_purl(monkeypatch):
    """cargo ecosystem maps to 'cargo' in the Socket purl."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    route = respx.post(PURL_URL).mock(
        return_value=httpx.Response(200, content=_socket_response(0.8, []))
    )
    env = ActivityEnvironment()
    await env.run(score, "cargo", "serde", "1.0.0", "1.0.1")
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["components"][0]["purl"] == "pkg:cargo/serde@1.0.1"


@respx.mock
async def test_nuget_ecosystem_uses_nuget_purl(monkeypatch):
    """nuget ecosystem maps to 'nuget' in the Socket purl."""
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    route = respx.post(PURL_URL).mock(
        return_value=httpx.Response(200, content=_socket_response(0.8, []))
    )
    env = ActivityEnvironment()
    await env.run(score, "nuget", "Newtonsoft.Json", "13.0.0", "13.0.1")
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["components"][0]["purl"] == "pkg:nuget/Newtonsoft.Json@13.0.1"


@respx.mock
async def test_socket_cache_hit(monkeypatch):
    monkeypatch.setenv("SOCKET_API_KEY", "test-key")
    respx.post(PURL_URL).mock(return_value=httpx.Response(200, content=_socket_response(0.8, [])))
    env = ActivityEnvironment()
    result1 = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    result2 = await env.run(score, "pip", "requests", "2.31.0", "2.32.0")
    assert result1 == result2
