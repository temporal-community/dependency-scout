# PR Actions

PR side-effect functions called by `PRActionWorkflow` after a triage verdict is reached. These talk to the GitHub or GitLab API to act on the PR.

All functions live in `actions.py` and are registered as Temporal activities with the `activities.platform.*` name prefix.

## Actions

| Activity name | What it does |
|---|---|
| `activities.platform.comment` | Posts the verdict comment on the PR |
| `activities.platform.merge_pr` | Auto-merges the PR |
| `activities.platform.close_pr` | Closes the PR with a reason |
| `activities.platform.label` | Adds a label to the PR |
| `activities.platform.request_review` | Requests review from configured reviewers |
| `activities.platform.check_pr_files` | Checks whether the PR touches unexpected files (CI scripts, Dockerfiles) |
| `activities.platform.fetch_repo_config` | Fetches `.github/dependency-scout.yml` from the target repo |

These are thin wrappers around `PlatformClient` (from `platforms/`) that give each operation a stable, platform-neutral activity name. The actual platform (GitHub, GitLab, ...) is determined at runtime from `pr.platform`.
