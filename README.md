# Dependabot Supply Chain Scout

You have 47 unreviewed Dependabot PRs. You're going to merge most of them anyway. So did the maintainers of [XZ Utils](https://en.wikipedia.org/wiki/XZ_Utils_backdoor), [event-stream](https://blog.npmjs.org/post/180565383195/details-about-the-event-stream-incident), and dozens of other projects before a malicious update slipped through.

**This bot gives every dependency PR a real second opinion before it merges.**

It checks six independent signals in parallel — CVEs, supply chain score, package diff, release age, maintainer changes, download trends — classifies the risk as green/yellow/red, and posts a verdict comment to the PR. You decide what happens next.

> **Status:** Experimental — works locally and with personal GitHub App installs. Public deployment coming soon.

---

## What it actually does

When a Dependabot or Renovate PR opens, the Scout:

1. **Fetches signals** from public APIs (PyPI/npm, OSV, Socket.dev, pypistats) — no API keys required for most signals
2. **Downloads and diffs** the package archive to see what code actually changed
3. **Classifies risk** as GREEN, YELLOW, or RED using Claude (or a rule-based fallback if you don't have an API key)
4. **Posts a verdict comment** to the PR explaining its reasoning
5. **Takes action** based on how you've configured it — or does nothing if you haven't

**RED** means something looks wrong: a new binary `.so`/`.node` file, obfuscated code, a maintainer account that appeared last week, exec/eval on dynamic strings, network calls added to install scripts.

**YELLOW** means "worth a look": major version bump, package released less than 7 days ago, new maintainer, unusually large diff for a patch bump, low download count.

**GREEN** means: patch or minor bump, well-established package, no CVEs, no red flags in the diff, release has been out for at least a week.

### Safe by default

**If you don't configure anything, the Scout only posts comments.** It never merges, closes, or requests review unless you explicitly enable it in `.github/triage-agent.yml`. This means installing it on a repo you haven't thought about yet is harmless.

---

## Try it in 5 minutes

You need Python 3.10+, [`uv`](https://docs.astral.sh/uv/), and the [Temporal CLI](https://docs.temporal.io/cli).

```bash
# Clone and install
git clone https://github.com/temporal-community/dependabot-supply-chain-scout
cd dependabot-supply-chain-scout
uv sync

# Start Temporal (separate terminal)
temporal server start-dev

# Start the worker (separate terminal)
uv run python -m worker

# Run a triage against a real public PR
uv run python -m start_workflow \
  --repo temporalio/ai-cookbook \
  --package idna \
  --old-version 3.11 \
  --new-version 3.15 \
  --pr-number 122
```

Open **http://localhost:8233** to watch the workflow run in the Temporal UI. No API keys needed — it'll use the rule-based classifier and log what it would do without touching the actual PR.

### Add keys to unlock more

| Keys configured | What changes |
|---|---|
| _(none)_ | Rule-based classifier, log-only output |
| `ANTHROPIC_API_KEY` | Claude classifies instead of rule-based thresholds |
| + `GITHUB_TOKEN` or GitHub App | Posts real PR comments |
| + `ENABLE_PR_ACTIONS=true` | Can auto-merge green PRs if you've configured it |
| + `SOCKET_API_KEY` | Adds Socket.dev supply chain score to signals |

Copy `.env.example` to `.env` and fill in what you have.

---

## Configuring your repo

Add `.github/triage-agent.yml` to any repo where you want the Scout to do more than comment:

```yaml
# .github/triage-agent.yml
auto_merge_enabled: true
auto_merge_classifications: [green]   # auto-merge green verdicts
reviewers: [alice, bob]               # request review on yellow
min_release_age_hours: 168            # never merge anything < 7 days old
block_classifications: [red]          # add a label + block merge on red
```

All fields are optional. Any field you omit stays at its safe default (no auto-merge, no review requests).

---

## Roadmap

- [x] PyPI + npm ecosystem support
- [x] Six parallel signal sources (PyPI/npm, OSV, Socket.dev, diff, release age, maintainer history)
- [x] LLM classifier with rule-based fallback
- [x] GitHub App auth
- [x] FastAPI webhook receiver
- [x] Per-repo config via `.github/triage-agent.yml`
- [x] Observe-only safe default
- [x] Replay test fixtures (workflow determinism guarantee)
- [ ] Public GitHub App registration
- [ ] npm support (next — currently PyPI only in production)
- [ ] Rubygems, Composer, and other ecosystems

---

## How it works under the hood

See [ARCHITECTURE.md](ARCHITECTURE.md) for the two-workflow Temporal design, signal sources, LLM classifier, security hardening, and how to run it against live GitHub webhooks.

---

*A [Temporal Community](https://temporal.io/community) project.*
