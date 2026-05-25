# Helpers

**When do you need to add something here?** When you have a utility that's shared across multiple packages — authentication, HTTP, caching, formatting — and it doesn't belong in any of the domain packages (`checks/`, `ecosystems/`, `platforms/`, etc.).

These are internal utilities. Nothing in `helpers/` is part of the public plugin API — plugin authors import from `models`, `checks.signatures`, or the platform/classifier protocols instead.

## Files

| File | What it provides |
|---|---|
| `github_app.py` | GitHub App JWT generation and installation token exchange. Tokens are cached and refreshed automatically before expiry. |
| `http.py` | Shared `httpx.AsyncClient` with connection pooling. All check activities use this rather than creating per-call clients, reusing TCP connections and TLS sessions. Timeouts are specified per-request. |
| `cache.py` | Simple in-process TTL cache for activity results. Avoids redundant network calls when multiple repos bump the same package in the same worker session. Thread-safe for asyncio. Contents are lost on worker restart — that's intentional. |
| `comment_formatter.py` | Formats a `Verdict` into the Markdown comment posted to PRs. Handles GREEN/YELLOW/RED styling, flag lists, and the human-approval action buttons. |
| `config_provider.py` | `ConfigProvider` protocol and implementations. `GitHubConfigProvider` reads `.github/dependency-scout.yml` from the target repo via the GitHub API. |
| `notification.py` | `NotificationChannel` protocol. Default implementation posts verdict comments to the PR via the platform client. |
| `pr_parser.py` | Extracts ecosystem, package name, old version, and new version from Dependabot and Renovate PR titles, bodies, and branch names. |
| `bot_parsers.py` | `BotParser` protocol and built-in parsers for `dependabot[bot]` and `renovate[bot]`. Determines whether an incoming webhook event should be triaged. |
| `prompts.py` | The system prompt for LLM classifiers (`CLASSIFIER_SYSTEM`). Centralised here so all three LLM classifier implementations (Claude, OpenAI, Ollama) share the same prompt. |
