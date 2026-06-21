# Running Scout as a GitHub App

A GitHub App is the recommended identity for Scout: short-lived (1-hour) tokens, least-privilege
scopes, an org-owned bot identity (`…[bot]`) instead of a personal PAT, and — crucially for orgs
that enforce **SAML SSO** — App installation tokens are **not** subject to the per-user SSO
authorization dance that blocks user PATs.

You only need this for *acting* on a repo (comment / label / close / push / open PR). Read-only
or dry-run use works with any token. Scout already supports App auth (`helpers/github_app.py`);
this is a setup task, not a code change.

---

## Which account should own the App?

Where an App is **owned** (registered) and where it's **installed** are independent. Pick based on
who has admin where:

| Variant | Owner | "Where can this app be installed?" | Trade-off |
|---|---|---|---|
| **A — community-owned** | `@temporal-community` (where you have admin + where Scout lives) | **Any account** | Publicly *installable* (gets a public listing page). Safe: each install is sandboxed with its own tokens — a stranger installing it on *their* repo can't reach your repos, tokens, or private key. But you must set "Any account" to install it on a *different* org (`temporalio`). |
| **B — temporalio-owned** | `@temporalio` (needs an org owner to create it) | **Only on this account** | Most locked-down — not publicly installable at all. Requires a `temporalio` org owner to do the ~5-minute creation. |

Either way, **installing** it on `temporalio/ai-cookbook` needs a `temporalio` repo admin / org owner
to approve. Variant A lets you create it yourself; Variant B keeps it private but needs an owner up front.

> **On "Any account" and security:** it controls who can *install* the App, **not** who can use your
> tokens. There is no shared token pool — GitHub mints per-installation tokens scoped to that
> installer's repos, and the private key never leaves the owner. With the webhook off (below), a
> third-party install is inert. The blast radius stays "where *you* install it × the permissions
> you grant."

---

## Create the App

**Owner org → Settings → Developer settings → GitHub Apps → New GitHub App.**

Most of the form is for use cases Scout doesn't need (user login, webhook server). Fill only these:

| Field | Value | Notes |
|---|---|---|
| **GitHub App name** | e.g. `temporal-dependency-scout` | must be globally unique |
| **Homepage URL** | `https://github.com/temporal-community/dependency-scout` | any URL; just a link |
| **Callback URL** | *blank* | only for "Login with GitHub" user OAuth — not used |
| **Setup URL** | *blank* | optional post-install redirect — not used |
| **Request user authorization (OAuth)** | unchecked | no user auth |
| **Enable Device Flow** | unchecked | — |
| **Webhook → Active** | **UNCHECK** | ⬅️ see below |
| **Webhook URL / secret** | *blank* | only for the persistent webhook-server deployment |
| **Subscribe to events** | none | only matters if the webhook is active |
| **Permissions** | Contents **R/W**, Pull requests **R/W**, Issues **R/W**, Metadata Read | what Scout uses |
| **Where can this app be installed?** | **Any account** (Variant A) / **Only on this account** (Variant B) | ⬅️ see below |

### The two settings that matter most

1. **Uncheck "Webhook → Active."** The webhook URL / secret / event subscriptions are *only* for
   Scout's persistent webhook-server mode (the FastAPI receiver that triages PRs the instant they
   open). The CI / `--local` path and `scout remediate` are **invoked by GitHub Actions or run
   manually** — they don't *receive* webhooks. Leave it off; flip it on only if you later stand up
   the server.
2. **Installation scope** — Variant A needs **"Any account"** to install on a different org;
   Variant B uses **"Only on this account."** See the table above.

### Why these permissions

- **Pull requests: R/W** — comment, request review, close, merge.
- **Issues: R/W** — applying **labels** to a PR goes through the Issues permission (labels are an
  issues feature even on PRs). The `scout:` verdict labels need this.
- **Contents: R/W** — read manifests/CODEOWNERS, **and push** the branch for `scout remediate`.
  (Triage-only, no remediation → Contents: Read is enough.)
- **Metadata: Read** — mandatory baseline.

After creating: **Generate a private key** (downloads a `.pem`) and note the **App ID**. Treat the
`.pem` as the crown jewel — anyone holding it can mint installation tokens. Store it as a secret,
never commit it, and rotate it freely (you can add/revoke keys without recreating the App).

---

## Install it

From the App's settings → **Install App** → choose **`temporalio/ai-cookbook`** (and only the repos
you want — narrow scope = small blast radius). If you lack admin on that repo/org, "Install" creates
a **request** that a `temporalio` org owner approves.

---

## Wire it into Scout

### CI (recommended) — mint a token per run

Mint an installation token in the workflow and hand Scout a normal `GITHUB_TOKEN`, so the private
key never enters Scout's environment:

```yaml
- uses: actions/create-github-app-token@v2
  id: app-token
  with:
    app-id: ${{ vars.SCOUT_APP_ID }}
    private-key: ${{ secrets.SCOUT_APP_PRIVATE_KEY }}
- run: uvx 'dependency-scout>=0.8.0' triage "${{ github.event.pull_request.html_url }}" --local
  env:
    GITHUB_TOKEN: ${{ steps.app-token.outputs.token }}
```

The same `GITHUB_TOKEN` works for `scout remediate`. Scout uses it transparently — no App-specific
code path needed.

### Local / scripted

Either pass an App-minted token as `GITHUB_TOKEN` (as above), or let Scout mint internally:

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY="$(cat scout-app.private-key.pem)"   # or GITHUB_APP_PRIVATE_KEY_PATH=…
```

`helpers/github_app.py` exchanges these for a short-lived installation token (cached, auto-refreshed).
The persistent webhook deployment uses this path, sourcing the installation ID from the webhook payload.

---

## Tracking usage

**What the App is doing (the legitimate work):**
- **GitHub Actions run history** — the triage workflow's runs: the primary "is it running / what did
  it decide" view.
- **The artifacts** — `scout:` labels, verdict comments, and remediation PRs are a visible trail on
  each PR.
- **Org audit log** (`temporalio` → Settings → Audit log) — every bot action (open PR, push, comment,
  label, close) recorded and filterable by the App's actor (`…[bot]`). The authoritative "what/when."
- **Temporal UI** — against a real Temporal (not `--local`), each triage/remediation is a workflow
  with full history.

**Watching for unexpected installs (relevant only to Variant A / "Any account"):**
- The App settings page lists every **installation** — glance at it periodically; anything other than
  your expected orgs is a flag.
- For active alerting on new installs, subscribe to just the `installation` webhook event (requires
  turning the webhook on + a listener) — otherwise the manual check is fine.

**Quota:** each installation has its own rate limit; `GET /rate_limit` with the installation token
shows remaining quota.

---

## Security recap

- **Private key = the crown jewel.** Store as a secret, never commit, rotate freely.
- **Install narrowly** (specific repos, not "all repos").
- **Least privilege** — drop `Contents: write` if you're not using `scout remediate`.
- **Branch protection still applies** to the App — it can't merge anything humans wouldn't let it.
- **"Any account" exposes installability, not access** — per-installation token isolation means a
  third-party install can't reach your repos, tokens, or key.
