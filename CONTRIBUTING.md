# Contributing

## Adding a new ecosystem

The plugin architecture makes this straightforward. Adding Composer, NuGet, or any other ecosystem is approximately 150 lines in one file.

**Step 1 — Create `activities/ecosystems/{name}.py`**

Implement the `EcosystemProvider` Protocol. Copy an existing provider (e.g. `rubygems.py`) as a starting point. Set `ecosystem_name` to the canonical key used everywhere else — `get_provider()` discovers it automatically:

```python
from activities.ecosystems import is_major, parse_upload_time, validate_archive_url
from activities.models import AttestationSignals, MaintainerSignals, PyPISignals, ReleaseAgeSignals

class ComposerProvider:
    ecosystem_name = "composer"  # auto-discovered by get_provider()
    osv_name = "Packagist"       # must match the ecosystem name used by api.osv.dev

    async def fetch_metadata(self, package, old_version, new_version) -> PyPISignals: ...
    async def fetch_release_age(self, package, new_version) -> ReleaseAgeSignals: ...
    async def fetch_maintainer(self, package, old_version, new_version) -> MaintainerSignals: ...
    async def get_archive_url(self, client, package, version) -> tuple[str, str, str] | None: ...
    def extract_archive(self, archive_bytes, filename, dest) -> None: ...
    async def fetch_attestations(self, package, old_version, new_version) -> AttestationSignals: ...
```

`get_archive_url` returns `(url, filename, integrity_string)`. Call `validate_archive_url(url)` before returning — this enforces the CDN allowlist. Add your registry's CDN host to `ALLOWED_CDN_HOSTS` in `activities/ecosystems/__init__.py`.

**Step 2 — Three small one-line changes**

- `activities/models.py` — add the name to the `Literal["pip", "npm", ...]` type in both `PRContext.ecosystem` and `PackageSignals.ecosystem`
- `helpers/pr_parser.py` — add the Dependabot branch slug to `_DEPENDABOT_ECOSYSTEM_MAP` (e.g. `"composer": "composer"`)
- `api/webhook.py` — add a package name validation regex to `_NAME_RE_BY_ECOSYSTEM`

**Step 3 — Tests**

Add a test section for your ecosystem following the existing npm/rubygems patterns in `tests/test_activities.py`. Each method needs at minimum: success case, 404/not-found case, and (for attestations) a no-attestation case. Aim to keep overall coverage above 95%.

---

## Adding a new signal

Each signal is a parallel activity that gathers one category of supply chain data and returns a typed sub-model. All signals run at the same time; a failing signal gets degraded defaults rather than crashing the workflow.

**Step 1 — Define the sub-model in `activities/models.py`**

```python
class MyNewSignals(BaseModel):
    some_flag: bool = False
    some_score: int | None = None
```

All fields need defaults so the model can be instantiated as a zero/degraded state when the activity fails. Then add it as a nested field in `PackageSignals`:

```python
class PackageSignals(BaseModel):
    ...
    my_new: MyNewSignals = Field(default_factory=MyNewSignals)
```

**Step 2 — Create `activities/my_new_signal.py`**

```python
from temporalio import activity
from activities.models import MyNewSignals

@activity.defn(name="activities.my_new_signal.check")
async def check(ecosystem: str, package: str, old_version: str, new_version: str) -> MyNewSignals:
    try:
        # fetch data from an external API
        return MyNewSignals(some_flag=True, some_score=42)
    except Exception:
        return MyNewSignals()   # degraded defaults — never raise from a signal activity
```

The activity name string is what the workflow references. It must match exactly.

**Step 3 — Add to `_SIGNAL_REGISTRY` in `workflows/package_triage_workflow.py`**

**Append** a row to `_SIGNAL_REGISTRY` — do not insert mid-list, as this changes Temporal's command sequence and breaks replay of existing histories:

```python
_SIGNAL_REGISTRY: list[tuple[str, str, type, bool]] = [
    ...
    ("my_new", "activities.my_new_signal.check", MyNewSignals, False),
    #           ^activity name string             ^result type  ^True = 2-min timeout
]
```

The first element (`"my_new"`) must match the field name you added to `PackageSignals`.

**Step 4 — Use the signal in `activities/classifier.py`**

Add logic to `_rule_based` using `signals.my_new.some_flag`, and/or let the LLM see it — it already appears in the trusted JSON block via `PackageSignals.model_dump()`. Forgetting this step will cause a test failure in `tests/test_signal_wiring.py`.

**Step 5 — Regenerate replay fixtures**

Any change to the workflow's gather list changes its Temporal command sequence. Regenerate the determinism fixtures:

```bash
uv run python tests/generate_fixtures.py
```

Commit the updated files in `tests/fixtures/`.

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

If you change `workflows/package_triage_workflow.py` or `workflows/pr_action_workflow.py`, regenerate the replay fixtures:

```bash
uv run python tests/generate_fixtures.py
```

Commit the updated files in `tests/fixtures/`. The CI `pytest` run will catch any determinism regression.

---

## Design principles

- **Graceful degradation** — missing API keys or upstream errors produce degraded signal defaults, not a crash. This is enforced at two levels: each activity catches its own errors and returns a zero-state model; the workflow's `asyncio.gather` uses `return_exceptions=True` so a single failing activity never discards the other ten results.
- **Attacker-controlled data stays sandboxed** — package descriptions, socket alert strings, release notes, and diff content go into clearly-labelled XML tags in the LLM prompt and are explicitly named in the system prompt as untrusted.
- **No silent fallbacks** — use `ApplicationError(..., non_retryable=True)` for permanent errors (404, auth failure) so Temporal doesn't retry endlessly.
- **Archive URLs are validated** before any HTTP request — add new CDN hosts to `ALLOWED_CDN_HOSTS`, never skip the `validate_archive_url()` call.
- **Signals are nested, not flat** — `PackageSignals` holds typed sub-models (`signals.age.release_age_hours`, not `signals.release_age_hours`). Field name collisions between signals are structurally impossible.
- **The registry is the source of truth** — `_SIGNAL_REGISTRY` in `package_triage_workflow.py` drives the gather order, the `SIGNAL_ACTIVITY_NAMES` constant, and the worker registration test. When adding a signal, the registry row is the one required workflow-layer edit.
- **Worker registration is automatic** — `worker.py` scans `activities/*.py` at startup for `@activity.defn`-decorated functions. Creating a new activity file is sufficient; no manual entry in `ACTIVITIES` is needed.
- **Ecosystem provider registration is automatic** — `get_provider()` scans `activities/ecosystems/*.py` for classes with an `ecosystem_name` attribute. Creating a new provider file with `ecosystem_name = "..."` is sufficient; no manual entry in `get_provider()` is needed.
