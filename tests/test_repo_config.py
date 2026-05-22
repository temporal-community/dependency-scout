import base64

import httpx
import respx
from temporalio.testing import ActivityEnvironment

from activities.models import PRContext, RepoConfig
from activities.repo_config import fetch

GITHUB_CONTENTS_URL = "https://api.github.com/repos/owner/repo/contents/.github/triage-agent.yml"

PR = PRContext(
    repo="owner/repo",
    pr_number=1,
    pr_author="dependabot[bot]",
    installation_id=0,
    ecosystem="pip",
    package_name="requests",
    old_version="2.31.0",
    new_version="2.32.0",
)


def _contents_response(yaml_text: str) -> dict:
    return {"content": base64.b64encode(yaml_text.encode()).decode() + "\n"}


@respx.mock
async def test_missing_config_returns_defaults():
    respx.get(GITHUB_CONTENTS_URL).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    result = await env.run(fetch, PR)
    assert result == RepoConfig()
    assert result.auto_merge_enabled is False
    assert result.reviewers == []


@respx.mock
async def test_config_loaded_and_parsed():
    yaml_text = "auto_merge_enabled: true\nreviewers: [alice, bob]\nmin_release_age_hours: 48\n"
    respx.get(GITHUB_CONTENTS_URL).mock(
        return_value=httpx.Response(200, json=_contents_response(yaml_text))
    )
    env = ActivityEnvironment()
    result = await env.run(fetch, PR)
    assert result.auto_merge_enabled is True
    assert result.reviewers == ["alice", "bob"]
    assert result.min_release_age_hours == 48


@respx.mock
async def test_unknown_fields_ignored():
    yaml_text = "auto_merge_enabled: true\nunknown_field: whatever\n"
    respx.get(GITHUB_CONTENTS_URL).mock(
        return_value=httpx.Response(200, json=_contents_response(yaml_text))
    )
    env = ActivityEnvironment()
    result = await env.run(fetch, PR)
    assert result.auto_merge_enabled is True


@respx.mock
async def test_empty_config_returns_defaults():
    respx.get(GITHUB_CONTENTS_URL).mock(
        return_value=httpx.Response(200, json=_contents_response(""))
    )
    env = ActivityEnvironment()
    result = await env.run(fetch, PR)
    assert result == RepoConfig()


@respx.mock
async def test_force_auto_merge_override(monkeypatch):
    monkeypatch.setenv("FORCE_AUTO_MERGE", "true")
    respx.get(GITHUB_CONTENTS_URL).mock(return_value=httpx.Response(404))
    env = ActivityEnvironment()
    result = await env.run(fetch, PR)
    assert result.auto_merge_enabled is True
