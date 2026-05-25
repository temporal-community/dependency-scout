# Ecosystem Providers

Each ecosystem provider translates a package registry's APIs into the seven signal methods that `PackageTriageWorkflow` uses to produce a triage verdict.

## Coverage

| Ecosystem | `ecosystem_name` | Language(s) | Registry | Attestations | Notes |
|---|---|---|---|---|---|
| pip (PyPI) | `pip` | Python | pypi.org | Yes | Cryptographic proof of where the package was built, verified via Sigstore |
| npm | `npm` | JavaScript / TypeScript / Node.js | registry.npmjs.org | Yes | Same provenance scheme as pip |
| Cargo | `cargo` | Rust | crates.io | No | |
| RubyGems | `rubygems` | Ruby | rubygems.org | No | Archive is nested `.gem` → `data.tar.gz` |
| Go modules | `go` | Go | proxy.golang.org | No | GOPROXY URL encoding (`!` escaping for uppercase) |
| Maven | `maven` | Java / Kotlin / Scala / JVM | repo1.maven.org | No | Coordinate format: `groupId:artifactId` |
| NuGet | `nuget` | C# / .NET / F# | api.nuget.org | No | Registration pages may be paginated |
| Composer | `composer` | PHP | packagist.org | No | Archives fetched from GitHub codeload |

All eight providers implement all seven signal methods. "Attestations: No" means `fetch_attestations` returns `AttestationChecks(has_attestation=False)` — it doesn't fail, it just signals absence.

## Signal methods

Each provider must implement:

| Method | What it returns | Primary data source |
|---|---|---|
| `fetch_metadata` | `PyPIChecks` — weekly downloads, major-bump flag, description | Registry API |
| `fetch_release_age` | `ReleaseAgeChecks` — hours since the version was published | Registry upload timestamp |
| `fetch_maintainer` | `MaintainerChecks` — whether a new maintainer was added for this version | Registry maintainer list |
| `get_archive_url` | `(url, filename, sha256)` or `None` | Registry file index |
| `extract_archive` | _(void)_ — extracts bytes to a dest dir | Archive bytes from `get_archive_url` |
| `fetch_attestations` | `AttestationChecks` — SLSA/Sigstore provenance | Registry provenance endpoint |
| `fetch_release` | `ReleaseChecks` — GitHub release, tag signature, timing skew | GitHub/GitLab API |

## Adding a new built-in ecosystem

**Step 1 — create the provider module**

```python
# ecosystems/myecosystem.py
import re
from ecosystems import EcosystemProviderBase, validate_archive_url, ...
from models import AttestationChecks, MaintainerChecks, PyPIChecks, ReleaseAgeChecks, ReleaseChecks

class MyEcosystemProvider(EcosystemProviderBase):
    ecosystem_name  = "myecosystem"          # must be unique
    osv_name        = "MyEcosystem"          # OSV ecosystem name for CVE lookups
    dependabot_slug = "my_ecosystem"         # Dependabot branch prefix
    name_re         = re.compile(r"^[a-z0-9_-]+$")  # package name allowlist

    async def fetch_metadata(self, package, old_version, new_version) -> PyPIChecks: ...
    async def fetch_release_age(self, package, new_version) -> ReleaseAgeChecks: ...
    async def fetch_maintainer(self, package, old_version, new_version) -> MaintainerChecks: ...
    async def get_archive_url(self, client, package, version) -> tuple[str, str, str] | None: ...
    def extract_archive(self, archive_bytes, filename, dest) -> None: ...
    async def fetch_attestations(self, package, old_version, new_version) -> AttestationChecks: ...
    async def fetch_release(self, package, old_version, version) -> ReleaseChecks: ...
```

The module is auto-discovered via `pkgutil` — no registration needed.

**Step 2 — add the ecosystem to the type model**

In `models/__init__.py`, add `"myecosystem"` to the `Literal[...]` types for ecosystem names.

**Step 3 — wire up Dependabot branch parsing**

In `helpers/pr_parser.py`, add the `dependabot_slug` → `ecosystem_name` mapping to `_DEPENDABOT_ECOSYSTEM_MAP`.

**Step 4 — add package name validation**

In `api/webhook.py`, add a `name_re` entry to `_NAME_RE_BY_ECOSYSTEM` (or rely on `get_name_re()` from `ecosystems/__init__.py` if the webhook already calls that).

**Step 5 — write tests**

Add a test file under `tests/` following the patterns in `tests/test_pip_*.py` or `tests/test_npm_*.py`. Use `respx` for HTTP mocking and `ActivityEnvironment` for activity harness.

**Step 6 — regenerate replay fixtures**

If you changed any workflow code (unlikely for a new ecosystem, but possible if you added a new activity call):

```bash
uv run python tests/generate_fixtures.py
```

## Adding an external plugin ecosystem

For non-Python registries or third-party providers, use the entry point plugin path instead of adding a built-in module:

```toml
# pyproject.toml of your plugin package
[project.entry-points."dependency_scout.ecosystems"]
myecosystem = "my_package:MyEcosystemProvider"
```

Inherit from `EcosystemProviderBase` and set `ecosystem_name`, `osv_name`, `dependabot_slug`, and `name_re`. For providers hosted in another language, inherit from `ecosystems.remote.RemoteEcosystemProvider` and set `remote_base_url` — it delegates all signal fetching to HTTP POST endpoints on your service.

Built-in providers take precedence over plugins with the same `ecosystem_name`.
