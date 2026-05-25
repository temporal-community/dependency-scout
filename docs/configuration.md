# Configuration Reference

The Scout is safe to install with no configuration — it posts a verdict comment on every Dependabot/Renovate PR but never merges, closes, or requests review until you tell it to.

To enable actions, add `.github/dependency-scout.yml` to any repo you want the Scout to act on. A ready-to-copy template is at [`.github/dependency-scout.yml.example`](../.github/dependency-scout.yml.example).

---

## All fields

| Field | Type | Default | What it does |
|---|---|---|---|
| `auto_merge_enabled` | `bool` | `false` | Enable auto-merge for classified PRs |
| `auto_merge_classifications` | `list[str]` | `["green"]` | Which verdicts are eligible for auto-merge |
| `auto_merge_min_confidence` | `float` (0–1) | `0.80` | Classifier confidence required before auto-merge fires |
| `min_release_age_hours` | `int` | `168` (7 days) | Never auto-merge a release newer than this, even if GREEN |
| `reviewers` | `list[str]` | `[]` | GitHub usernames to @-mention for review on YELLOW verdicts |
| `block_classifications` | `list[str]` | `["red"]` | Close the PR and add a label for these verdicts |
| `max_new_dependencies` | `int` | `5` | Flag as YELLOW when a bump adds more than this many new direct deps |
| `extra_check_activities` | `list[str]` | `[]` | Temporal activity names for custom check plugins (advanced — see below) |

---

## Field details

### `auto_merge_enabled`

Set to `true` to allow the Scout to merge PRs automatically. Auto-merge only fires when ALL of these conditions are met:
- The verdict classification is in `auto_merge_classifications`
- The classifier's confidence is at least `auto_merge_min_confidence`
- The release is older than `min_release_age_hours`

Default: `false` — safe to deploy without thinking about it.

### `auto_merge_classifications`

Which verdict classifications are eligible for auto-merge. Default is `["green"]`. Change to `["green", "yellow"]` at your own risk.

### `auto_merge_min_confidence`

Minimum classifier confidence (0.0–1.0) required before auto-merge fires. `0.80` means "the classifier must be at least 80% confident." Raise this if you want the Scout to be more conservative.

### `min_release_age_hours`

Never auto-merge a release published less than this many hours ago, even if the verdict is GREEN. Gives time for community review of fresh releases.

Default: `168` (7 days). Set to `0` to disable the age gate entirely.

Note: this is also applied as a YELLOW upgrade — even if the shared `PackageTriageWorkflow` classified a package GREEN, `PRActionWorkflow` re-checks the release age against *this repo's* threshold and upgrades to YELLOW if needed. Different repos can have different standards.

### `reviewers`

GitHub usernames to @-mention for code review when a PR is classified YELLOW. Leave empty (or omit) to skip review requests.

```yaml
reviewers: [alice, bob]
```

When reviewers are configured and a YELLOW verdict arrives, the Scout posts a comment, requests review, and then **waits indefinitely** for a human to approve or reject via the PR review interface. No polling — it wakes when the signal arrives.

### `block_classifications`

Verdicts that should trigger PR closure + label. Default is `["red"]`. Set to `[]` for fully observe-only mode (no closures at all).

### `max_new_dependencies`

Flag a bump as YELLOW when it adds more than this many new direct dependencies across all manifest files (`package.json`, `requirements.txt`, etc.). A routine patch bump adding 10 transitive packages is suspicious.

Applied as a per-repo override after the shared verdict — same as `min_release_age_hours`.

### `extra_check_activities`

Names of additional Temporal activities to call for custom signals. Each activity receives the package context and returns a JSON-serializable dict. Results appear in the LLM context as supplementary data (sandboxed — cannot override core signals).

Requires the corresponding plugin package to be installed in your worker deployment. See [extending.md](extending.md#advanced-checks-dependency_scout-activity_checks) for the plugin authoring guide.

```yaml
extra_check_activities:
  - my_company.deep_archive_scan
```

---

## Example configs

### Minimal "just auto-merge safe stuff"

```yaml
# .github/dependency-scout.yml
auto_merge_enabled: true
reviewers: [your-github-username]   # gets pinged on yellow
```

### Stricter — wait a week, block red, two reviewers on yellow

```yaml
auto_merge_enabled: true
auto_merge_min_confidence: 0.90
min_release_age_hours: 168
reviewers: [alice, bob]
block_classifications: [red]
```

### Observe-only (just get comments, never take action)

```yaml
# Empty file, or omit the file entirely — this is the default.
# Explicitly clear block_classifications if you want truly zero action:
block_classifications: []
```

---

## Environment variables

Environment variables configure the Scout worker itself (credentials, Temporal connection, classifier choice). They are set in `.env` in the Scout deployment — not in the per-repo `.github/dependency-scout.yml`.

See [architecture.md](architecture.md#environment-variables) for the full variable reference.
