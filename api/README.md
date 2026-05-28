# API

**When do you need to touch this directory?** When adding support for a new webhook source (a new bot, a new event type, or a new code host's webhook format). For adding new triage logic, see [`checks/`](../checks/README.md) or [`workflows/`](../workflows/README.md) instead.

This is the production entry point: a FastAPI application that receives webhooks from GitHub and GitLab, verifies their authenticity, parses the PR/MR metadata, and starts `PRActionWorkflow` asynchronously.

## Endpoints

| Endpoint | Platform | Event types handled |
|---|---|---|
| `POST /webhook` | GitHub | `pull_request`, `pull_request_review` |
| `POST /webhook/github` | GitHub | Same (alias) |
| `POST /webhook/gitlab` | GitLab | Merge Request Hook |

## What happens on each request

1. **Verify** — GitHub: HMAC-SHA256 signature check against `GITHUB_WEBHOOK_SECRET`. GitLab: token comparison against `GITLAB_WEBHOOK_SECRET`. Requests that fail verification get a 401.
2. **Filter** — only Dependabot and Renovate bot events are processed; everything else returns 200 immediately.
3. **Parse** — ecosystem, package name, old version, and new version are extracted from the PR title/body/branch name via `helpers/pr_parser.py`.
4. **Start workflow** — `PRActionWorkflow` is started (or a signal is sent if already running) via the Temporal client. The HTTP response returns 200 immediately; the workflow runs asynchronously.

## Running locally

```bash
uv run uvicorn api.webhook:app --reload
```

Requires a running Temporal server (`temporal server start-dev`) and the worker (`uv run python -m worker`). For end-to-end local testing without a real webhook, use `uv run dependency-scout triage <PR URL>` directly.
