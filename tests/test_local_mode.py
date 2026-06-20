"""
Tests for embedded ``--local`` mode: the self-contained path that boots an
in-process Temporal server + worker for a single CLI invocation (no external
``temporal server start-dev`` and no ``dependency-scout-worker`` process).

The full workflow run is covered by the replay fixtures; these tests verify the
*wiring* — that ``build_worker`` registers the right workflows/activities and
that ``_dispatch`` routes to an embedded environment (vs. an external one)
without needing real network or a downloaded dev-server binary.
"""

import os

import scout
import worker as worker_mod
from temporalio.testing import WorkflowEnvironment
from workflows.package_triage_workflow import PackageTriageWorkflow
from workflows.pr_action_workflow import PRActionWorkflow


def test_build_worker_registers_both_workflows_and_all_activities(monkeypatch):
    """build_worker wires the client, task queue, both workflows, and ACTIVITIES."""
    captured = {}

    class FakeWorker:
        def __init__(self, client, *, task_queue, workflows, activities):
            captured.update(
                client=client, task_queue=task_queue, workflows=workflows, activities=activities
            )

    monkeypatch.setattr(worker_mod, "Worker", FakeWorker)
    sentinel_client = object()

    worker_mod.build_worker(sentinel_client, "my-queue")

    assert captured["client"] is sentinel_client
    assert captured["task_queue"] == "my-queue"
    assert set(captured["workflows"]) == {PackageTriageWorkflow, PRActionWorkflow}
    # Same discovered activity set the standalone worker uses — no drift.
    assert captured["activities"] == worker_mod.ACTIVITIES


async def test_dispatch_remote_passes_no_client():
    """Without --local, the command runs against an external server (client=None)."""
    seen = {}

    async def factory(client):
        seen["client"] = client
        return "remote-result"

    result = await scout._dispatch(False, factory)

    assert result == "remote-result"
    assert seen["client"] is None  # falls through to connect() inside the command


async def test_dispatch_local_boots_embedded_env_and_worker(monkeypatch):
    """With --local, an embedded env + worker wrap the command, which runs against env.client."""
    # setenv so monkeypatch restores these even though _run_local assigns them directly.
    monkeypatch.setenv("TEMPORAL_ADDRESS", "unused")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "unused")

    class _FakeServiceClient:
        config = type("cfg", (), {"target_host": "127.0.0.1:54321"})()

    class _FakeClient:
        namespace = "default"
        service_client = _FakeServiceClient()

    sentinel_client = _FakeClient()
    events = []

    class FakeEnv:
        client = sentinel_client

        async def __aenter__(self):
            events.append("env-enter")
            return self

        async def __aexit__(self, *exc):
            events.append("env-exit")
            return False

    async def fake_start_local(**kwargs):
        events.append("start_local")
        return FakeEnv()

    class FakeWorker:
        async def __aenter__(self):
            events.append("worker-enter")
            return self

        async def __aexit__(self, *exc):
            events.append("worker-exit")
            return False

    def fake_build_worker(client, task_queue):
        events.append(("build_worker", client, task_queue))
        return FakeWorker()

    monkeypatch.setattr(
        WorkflowEnvironment, "start_local", classmethod(lambda cls, **kw: fake_start_local(**kw))
    )
    monkeypatch.setattr(worker_mod, "build_worker", fake_build_worker)

    async def factory(client):
        events.append(("factory", client))
        return "local-result"

    result = await scout._dispatch(True, factory)

    assert result == "local-result"
    # Worker built against the embedded client, on the same task queue the
    # workflow-start uses (both read TEMPORAL_TASK_QUEUE), so they always match.
    expected_tq = os.environ.get("TEMPORAL_TASK_QUEUE", "default")
    assert ("build_worker", sentinel_client, expected_tq) in events
    # Internal connect() calls (e.g. await_triage_result) are pointed at the embedded
    # server's address, not the default localhost:7233.
    assert os.environ["TEMPORAL_ADDRESS"] == "127.0.0.1:54321"
    # Command ran against the embedded client...
    assert ("factory", sentinel_client) in events
    # ...inside both the worker and env context managers, which tear down after.
    assert (
        events.index("env-enter")
        < events.index("worker-enter")
        < events.index(("factory", sentinel_client))
        < events.index("worker-exit")
        < events.index("env-exit")
    )
