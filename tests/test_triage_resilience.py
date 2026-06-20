"""
Structural guarantee against the "new paid dependency" foot-gun: a failure in ANY single
check activity must not sink the whole triage. The workflow degrades the failed signal to
its model's defaults (or {} for custom checks) and still returns a verdict.

This runs the real PackageTriageWorkflow against stub activities, failing one check per
iteration. It is parametrized over _CHECK_REGISTRY, so every check added in future —
including new paid integrations — is covered automatically with no extra test code.
"""

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from models import Verdict
from workflows.package_triage_workflow import _CHECK_REGISTRY, PackageTriageWorkflow


@pytest.fixture(autouse=True)
def block_real_network():
    """Override the conftest network guard for this module.

    This integration test runs a local Temporal test server (loopback, plus a one-time
    server-binary download) — it makes no real third-party API calls, since every activity
    is a stub. The conftest guard would otherwise block the test-server binary fetch.
    """
    yield


_BOOM = "simulated check failure"


def _check_stub(name: str, model: type, fail: bool):
    @activity.defn(name=name)
    async def act(*_args):
        if fail:
            raise RuntimeError(f"{name}: {_BOOM}")
        return model()

    return act


def _classifier_stub():
    @activity.defn(name="activities.classifier.classify")
    async def classify(_signals):
        return Verdict(classification="green", confidence=0.9, reasoning="stub", flags=[])

    return classify


def _custom_checks_stub(fail: bool):
    @activity.defn(name="activities.custom_checks.run_all")
    async def run_all(_ctx):
        if fail:
            raise RuntimeError(f"custom_checks: {_BOOM}")
        return {}

    return run_all


def _activities(failing_field: str):
    acts = [
        _check_stub(name, model, fail=(field == failing_field))
        for field, name, model, _ in _CHECK_REGISTRY
    ]
    acts.append(_custom_checks_stub(fail=(failing_field == "custom_checks")))
    acts.append(_classifier_stub())
    return acts


async def test_triage_survives_any_single_check_failure():
    """Fail each registered check in turn; the triage must still return a verdict."""
    fields = [field for field, _, _, _ in _CHECK_REGISTRY] + ["custom_checks"]
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        for field in fields:
            async with Worker(
                env.client,
                task_queue=f"tq-{field}",
                workflows=[PackageTriageWorkflow],
                activities=_activities(field),
            ):
                result = await env.client.execute_workflow(
                    PackageTriageWorkflow.run,
                    args=["pip", "requests", "2.31.0", "2.32.0"],
                    id=f"triage-resilience-{field}",
                    task_queue=f"tq-{field}",
                )
            assert result.verdict.classification == "green", (
                f"triage failed when the '{field}' check failed — it must degrade, not crash"
            )
