# Platforms

**When do you need a new platform?** When the code host where your PRs live isn't GitHub or GitLab — for example Gitea, Bitbucket, or an on-premise Forgejo instance.

A platform client handles all PR-management operations for one code-hosting platform. It's the layer that translates the workflow's abstract actions ("post a comment", "merge this PR") into API calls for a specific host. For the check functions that *produce* the verdict the workflow acts on, see [`checks/`](../checks/README.md).

## Built-in platforms

| File | Class | Platform | Auth mechanism |
|---|---|---|---|
| `github.py` | `GitHubPlatformClient` | GitHub (cloud + GHES) | GitHub App installation token (via `helpers/github_app.py`) |
| `gitlab.py` | `GitLabPlatformClient` | GitLab (cloud + self-hosted) | Personal access token or project token (`GITLAB_TOKEN`) |

## The `PlatformClient` protocol

Every platform client implements the same six async methods:

| Method | What it does |
|---|---|
| `comment(pr, verdict)` | Posts a formatted verdict comment on the PR/MR |
| `merge_pr(pr)` | Merges the PR |
| `close_pr(pr, reason, ignore_bot)` | Closes the PR with a reason comment |
| `label(pr, label_name)` | Adds a label to the PR |
| `request_review(pr, reviewers)` | Requests review from specified usernames |
| `check_pr_files(pr)` | Returns `PRFilesChecks` — whether the PR touches unexpected files |

The factory function `get_platform_client(pr)` in `__init__.py` selects the right implementation at runtime from `pr.platform`.

## Pluggability

Platforms are pluggable via the `dependency_scout.platforms` entry point group. Third-party clients register a factory function:

```toml
[project.entry-points."dependency_scout.platforms"]
gitea = "my_package.platform:create_client"
```

The factory must accept `(pr: PRContext)` and return a `PlatformClient`. Entry points are checked before the built-in fallbacks, so they can also override built-in platforms.

See [docs/extending.md](../docs/extending.md) for a full worked example.
