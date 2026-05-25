# Workflows

**When do you need to touch this directory?** When the triage or action *logic* changes — what checks run, how verdicts are acted on, or how the two workflows hand off to each other. For adding new checks or platform actions without changing control flow, see [`checks/`](../checks/README.md) and [`pr_actions/`](../pr_actions/README.md) instead.

Two Temporal workflows live here. Each has a strict determinism requirement: the same event history must produce the same execution path when replayed, because Temporal uses replay to recover from crashes.

## `PackageTriageWorkflow`

**Workflow ID:** `package-triage-{ecosystem}-{package}-{old_version}-{new_version}`

Runs all 11 check activities in parallel, collects their results into a `PackageChecks`, and runs the classifier to produce a `Verdict`. That's it — it doesn't touch the PR. Child workflows or `PRActionWorkflow` consume the verdict.

The check registry (`_CHECK_REGISTRY`) is a data structure mapping field names to activity string names and result types. Adding a new check means adding a row there, not modifying control flow.

## `PRActionWorkflow`

**Workflow ID:** `pr-action-{repo}-{pr_number}`

Per-PR orchestrator. Fetches repo config (`.github/dependency-scout.yml`), starts or attaches to `PackageTriageWorkflow`, then acts based on verdict + config:

| Verdict | Default action |
|---|---|
| GREEN | Auto-merge (if `auto_merge: true` in config) |
| YELLOW | Request human review |
| RED | Close the PR with a comment |

Also posts a verdict comment, applies labels, and checks for unexpected files in the diff (CI scripts, Dockerfiles) regardless of verdict.

## Determinism rules

- All non-deterministic I/O (HTTP calls, LLM calls, timestamps, randomness) happens inside *activities* — never directly in workflow code.
- Activities are referenced by **string name**, never imported directly into workflow files. This is required for Temporal's deterministic replay.
- Workflow-unsafe imports (anything that does I/O at import time) go inside `with workflow.unsafe.imports_passed_through():` at the top of the file.

A replay failure = a non-deterministic change slipped into workflow code. After any intentional workflow change, regenerate the replay fixtures:

```bash
uv run python tests/generate_fixtures.py
```

See `tests/test_workflow_replay.py` and [CLAUDE.md](../CLAUDE.md) for more on the replay test setup.
