import asyncio
import importlib
import os
import pkgutil

import activities as _activities_pkg
from dotenv import load_dotenv
from temporalio.activity import _Definition
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from workflows.package_triage_workflow import PackageTriageWorkflow
from workflows.pr_action_workflow import PRActionWorkflow

load_dotenv()


def _discover_activities() -> list:
    """Return every @activity.defn-decorated function found in activities/*.py (non-recursive).

    Scans only top-level modules — activities/ecosystems/ contains provider helpers, not activities.
    """
    seen: set[int] = set()
    fns = []
    for mod_info in pkgutil.iter_modules(_activities_pkg.__path__, prefix="activities."):
        mod = importlib.import_module(mod_info.name)
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if callable(obj) and id(obj) not in seen and _Definition.from_callable(obj) is not None:
                seen.add(id(obj))
                fns.append(obj)
    return fns


# Auto-discovered from activities/*.py — adding a new activity file is sufficient,
# no manual registration needed. Exposed at module level for test_signal_wiring.py.
ACTIVITIES = _discover_activities()


async def main() -> None:
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
    )
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "dependency-triage")
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[PackageTriageWorkflow, PRActionWorkflow],
        activities=ACTIVITIES,
    )
    print(f"Worker started on task queue: {task_queue}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
