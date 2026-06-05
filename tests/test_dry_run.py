"""Regression test for the --dry-run safety guarantee.

Reproduces the bug where PRActionWorkflow ignored pr.dry_run and performed real
platform actions (it auto-merged live PRs during a run the operator believed was a
no-op rehearsal). A dry-run must run all read-only analysis but call none of the
mutating platform activities (comment, merge, label, close, request review).
"""

import uuid

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from models import PRContext, RepoConfig
from workflows.package_triage_workflow import PackageTriageWorkflow
from workflows.pr_action_workflow import PRActionWorkflow
from tests.generate_fixtures import (
    _attestation,
    _check_actions_usage,
    _check_pr_files,
    _classifier,
    _custom_checks,
    _depsdev,
    _diff,
    _maintainer,
    _osv,
    _pypi,
    _release_age,
    _release_notes,
    _repo_config,
    _scorecard,
    _security_advisory,
    _socket,
    _version_lineage,
)

# Platform activities that mutate the PR — none of these may run under --dry-run.
MUTATING = [
    "activities.platform.comment",
    "activities.platform.merge_pr",
    "activities.platform.request_review",
    "activities.platform.label",
    "activities.platform.close_pr",
]


def _spy(name: str, recorder: list[str], result=None):
    @activity.defn(name=name)
    async def fn(*_):
        recorder.append(name)
        return result

    return fn


async def _run(dry_run: bool, classification: str, config: RepoConfig) -> tuple[str, list[str]]:
    called: list[str] = []
    # comment returns a URL string; the others return None.
    spies = [_spy("activities.platform.comment", called, "")] + [
        _spy(name, called) for name in MUTATING if name != "activities.platform.comment"
    ]
    acts = [
        _pypi(),
        _socket(),
        _osv(),
        _diff(),
        _maintainer(),
        _release_age(),
        _attestation(),
        _release_notes(),
        _version_lineage(),
        _depsdev(),
        _scorecard(),
        _security_advisory(),
        _custom_checks(),
        _classifier(classification),
        _repo_config(config),
        _check_pr_files(),
        _check_actions_usage(),
        *spies,
    ]
    pr = PRContext(
        repo="example/repo",
        pr_number=7,
        pr_author="dependabot[bot]",
        ecosystem="pip",
        package_name="requests",
        old_version="2.31.0",
        new_version="2.32.0",
        dry_run=dry_run,
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="dry-run-test",
            workflows=[PRActionWorkflow, PackageTriageWorkflow],
            activities=acts,
        ):
            result = await env.client.execute_workflow(
                PRActionWorkflow.run,
                pr,
                id=f"pr-action-test-{uuid.uuid4()}",
                task_queue="dry-run-test",
            )
    return result, called


@pytest.mark.parametrize(
    "config",
    [
        # The exact dangerous case from the incident: auto-merge enabled.
        RepoConfig(auto_merge_enabled=True, auto_merge_classifications=["green"]),
        RepoConfig(reviewers=["alice"]),
        RepoConfig(block_classifications=["green"]),
        RepoConfig(),  # observe-only
    ],
)
async def test_dry_run_performs_no_mutating_actions(config: RepoConfig):
    result, called = await _run(dry_run=True, classification="green", config=config)
    assert result.startswith("dry-run-"), result
    assert called == [], f"dry-run must not call mutating activities, but called: {called}"


async def test_dry_run_reports_would_auto_merge():
    config = RepoConfig(auto_merge_enabled=True, auto_merge_classifications=["green"])
    result, _ = await _run(dry_run=True, classification="green", config=config)
    assert result == "dry-run-green-auto-merge"


async def test_non_dry_run_still_comments():
    """Control: with dry_run off, the comment activity is called as before."""
    result, called = await _run(dry_run=False, classification="green", config=RepoConfig())
    assert "activities.platform.comment" in called
    assert not result.startswith("dry-run-")
