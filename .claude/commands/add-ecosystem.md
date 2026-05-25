Add a new package ecosystem provider to dependency-scout.

If the user provided arguments ($ARGUMENTS), treat them as the ecosystem name or description. Otherwise ask: "What ecosystem are you adding? (e.g. 'PyPI', 'Hackage', 'Pub/Flutter')"

Then collect any other details you need (registry URL, OSV ecosystem name, Dependabot slug) by looking at existing providers as reference, asking the user only for things you can't infer.

---

## What you're building

A single file `ecosystems/{name}.py` that implements the `EcosystemProvider` protocol. The provider is auto-discovered at startup — no registry rows, no worker changes, no fixture regeneration needed.

---

## Step 1 — Create `ecosystems/{name}.py`

Copy `ecosystems/cargo.py` as the closest structural template. The provider must have:

**Four class attributes:**
```python
ecosystem_name  = "myeco"        # key used everywhere in the codebase
osv_name        = "MyEco"        # must match api.osv.dev ecosystem name exactly
dependabot_slug = "myeco"        # Dependabot's internal branch prefix (e.g. "npm_and_yarn",
                                 # "pip", "cargo", "bundler", "maven", "nuget", "composer",
                                 # "go_modules") — check github.com/dependabot/dependabot-core
                                 # if unsure; used to parse Dependabot PR branch names
name_re         = re.compile(r"^[a-z0-9_-]+$")  # package name allowlist for the webhook
```

**Seven async methods** (all must be present; return degraded defaults on failure, never raise):
- `fetch_metadata(package, old_version, new_version) -> PyPIChecks` — download stats, description, major-bump flag
- `fetch_release_age(package, new_version) -> ReleaseAgeChecks` — registry publish timestamp
- `fetch_maintainer(package, old_version, new_version) -> MaintainerChecks` — who published each version
- `get_archive_url(client, package, version) -> tuple[str, str, str] | None` — returns `(url, filename, integrity_hash)`; call `validate_archive_url(url)` before returning
- `extract_archive(archive_bytes, filename, dest) -> None` — unpack the archive into `dest`
- `fetch_attestations(package, old_version, new_version) -> AttestationChecks` — SLSA/Sigstore provenance; return `AttestationChecks(has_attestation=False)` if registry doesn't support it
- `fetch_release(package, old_version, version) -> ReleaseChecks` — GitHub release checks; use the `fetch_vcs_release`, `fetch_vcs_tag_signature`, `fetch_vcs_ci_workflow_changes`, and `build_release_signals` helpers from `ecosystems/__init__.py` if the registry exposes a source repo URL

Imports to start with:
```python
from models import (
    AttestationChecks, MaintainerChecks, PyPIChecks,
    ReleaseAgeChecks, ReleaseChecks,
)
from ecosystems import (
    build_release_signals, fetch_vcs_release, fetch_vcs_tag_signature,
    fetch_vcs_ci_workflow_changes, is_major, parse_upload_time,
    parse_vcs_repo, validate_archive_url,
)
from helpers.http import get_client
```

## Step 2 — Add the CDN host

Open `ecosystems/__init__.py` and add the registry's download CDN hostname to `ALLOWED_CDN_HOSTS`. This is enforced before any archive download — without it the diff check silently degrades.

Example: if archives are served from `files.example-registry.org`, add that string to the frozenset.

## Step 3 — Write tests

Create `tests/test_{name}.py`. Use `tests/test_cargo.py` as the template — it covers all seven methods and is the cleanest example. Minimum required tests per method:

- `fetch_metadata`: success case, 404/not-found case
- `fetch_release_age`: success (recent release), success (old release), missing upload_time
- `fetch_maintainer`: same publisher, changed publisher, API failure degrades gracefully
- `get_archive_url`: returns valid tuple, CDN host is in ALLOWED_CDN_HOSTS
- `extract_archive`: round-trips (create archive in test, extract, verify contents)
- `fetch_attestations`: no-attestation case (if registry doesn't support it, just test it returns `has_attestation=False`)
- `fetch_release`: no linked GitHub repo case, linked repo with release

Mock HTTP with `respx`. Run activities inside `ActivityEnvironment()` from `temporalio.testing`.

Keep coverage above 95% (`uv run pytest --cov=ecosystems --cov-report=term-missing`).

## Step 4 — Run the full suite

```bash
uv run ruff format .
uv run ruff check .
uv run mypy .
uv run pytest -x -q
```

The `test_check_wiring.py` tests will catch registration problems automatically. If they fail, check that `ecosystem_name` is set as a class attribute (not instance attribute) and that the file is directly inside `ecosystems/` (not a subdirectory).

## Step 5 — Smoke test with a real package

```bash
uv run python -m start_workflow \
  --ecosystem myeco \
  --repo owner/some-repo \
  --package some-package \
  --old-version 1.0.0 \
  --new-version 1.1.0 \
  --pr-number 1
```

Watch the Temporal UI at http://localhost:8233 to confirm all activities complete (green checkmarks, not orange retries).

---

## Common pitfalls

- **`dependabot_slug` wrong** — Dependabot PRs for your ecosystem won't be parsed. Check the Dependabot source or look at real PR branch names: `dependabot/{slug}/{package}-{version}`.
- **`osv_name` wrong** — OSV vulnerability lookups return no results silently. Cross-check at https://api.osv.dev/v1/query with your ecosystem string.
- **CDN host not in `ALLOWED_CDN_HOSTS`** — archive diff degrades to empty with no error logged at the activity level. Always add the host before testing.
- **Raising from a method** — methods should catch their own exceptions and return degraded defaults. Only use `ApplicationError(..., non_retryable=True)` for permanent failures like 404 or auth errors.
- **`parse_upload_time`** — use this helper for registry timestamps rather than `datetime.fromisoformat`; it handles the format variations across registries.
