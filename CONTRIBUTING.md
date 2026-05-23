# Contributing

## Adding a new ecosystem

The plugin architecture makes this straightforward. Adding Composer, Maven, NuGet, or any other ecosystem is approximately 150 lines in one file.

**Step 1 — Create `activities/ecosystems/{name}.py`**

Implement the `EcosystemProvider` Protocol. Copy an existing provider (e.g. `rubygems.py`) as a starting point:

```python
from activities.ecosystems import is_major, parse_upload_time, validate_archive_url
from activities.models import AttestationSignals, MaintainerSignals, PyPISignals, ReleaseAgeSignals

class MavenProvider:
    osv_name = "Maven"   # must match the ecosystem name used by api.osv.dev

    async def fetch_metadata(self, package, old_version, new_version) -> PyPISignals: ...
    async def fetch_release_age(self, package, new_version) -> ReleaseAgeSignals: ...
    async def fetch_maintainer(self, package, old_version, new_version) -> MaintainerSignals: ...
    async def get_archive_url(self, client, package, version) -> tuple[str, str, str] | None: ...
    def extract_archive(self, archive_bytes, filename, dest) -> None: ...
    async def fetch_attestations(self, package, old_version, new_version) -> AttestationSignals: ...
```

`get_archive_url` returns `(url, filename, integrity_string)`. Call `validate_archive_url(url)` before returning — this enforces the CDN allowlist. Add your registry's CDN host to `ALLOWED_CDN_HOSTS` in `activities/ecosystems/__init__.py`.

**Step 2 — Register it**

Add one line to `get_provider()` in `activities/ecosystems/__init__.py`:

```python
"maven": MavenProvider(),
```

**Step 3 — Wire the rest**

Four small changes, each one line:

- `activities/models.py` — add the name to the `Literal["pip", "npm", "rubygems"]` type in `PRContext` and `PackageSignals`
- `helpers/pr_parser.py` — add the Dependabot branch slug to `_DEPENDABOT_ECOSYSTEM_MAP` (e.g. `"maven": "maven"`)
- `api/webhook.py` — add a package name regex to `_NAME_RE_BY_ECOSYSTEM`
- `worker.py` — already registers all activities dynamically; no change needed

**Step 4 — Tests**

Add a `tests/test_activities.py` section for your ecosystem following the existing npm/rubygems patterns. Each method needs at minimum: success case, 404 case, and (for attestations) a no-attestation case. Aim to keep overall coverage above 95%.

---

## Running locally

```bash
uv sync
uv run ruff format .          # format
uv run ruff check .           # lint
uv run mypy .                 # type check
uv run pytest                 # tests
```

Or with Docker (no local Python required):

```bash
cp .env.example .env          # fill in what you have
docker compose up
```

The Temporal UI will be at http://localhost:8233.

---

## Workflow changes and replay tests

If you change `workflows/package_triage_workflow.py` or `workflows/pr_action_workflow.py`, you must regenerate the replay fixtures:

```bash
uv run python tests/generate_fixtures.py
```

Commit the updated files in `tests/fixtures/`. The CI `pytest` run will catch any determinism regression.

---

## Design principles

- **Graceful degradation** — missing API keys or upstream errors produce a YELLOW signal, not a crash. Never fail a workflow because a single data source is unavailable.
- **Attacker-controlled data stays sandboxed** — package descriptions and diff content go into clearly-labelled XML tags in the LLM prompt and are explicitly named in the system prompt as untrusted.
- **No silent fallbacks** — `non_retryable=True` on permanent errors (404, auth failure) so Temporal doesn't retry endlessly.
- **Archive URLs are validated** before any HTTP request — add new CDN hosts to `ALLOWED_CDN_HOSTS`, never skip the check.
