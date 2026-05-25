Create a standalone signature plugin package for dependency-scout.

Use this when you want to ship attack signatures as a separate installable
package rather than contributing them to the core `checks/signatures/` files —
for example, org-internal threat intel, proprietary detection rules, or a
community feed of patterns.

If the user provided arguments ($ARGUMENTS), treat them as a description of
what the plugin will detect. Otherwise ask: "What will this signature plugin
detect? (e.g. 'internal threat intel feed', 'custom persistence patterns for
our stack', 'patterns from a CVE feed')"

---

## Step 1 — Choose the right tier

Ask (or infer from context):

**Tier A — YAML directory** (`dependency_scout.signatures`)
- Patterns are static and can be written as regex strings in YAML
- No runtime dependencies or network calls needed
- Lowest barrier: YAML files + a 3-line Python shim
- Use this for: curated pattern lists, community rule sets, org-internal IOCs

**Tier B — Python provider** (`dependency_scout.signature_providers`)
- Patterns must be generated at runtime (fetched from an API, a database, generated programmatically)
- Full Python — import anything, call anything
- Use this for: threat-intel feeds, CVE APIs, patterns that change frequently

When in doubt, suggest Tier A. If the user mentions "API", "feed", "dynamic", or "generated", suggest Tier B.

---

## Step 2 — Scaffold the package

Create a directory for the plugin. If the user hasn't named it, suggest `dependency-scout-{org}-signatures` or `dependency-scout-{topic}-sigs`.

```
my-plugin/
├── pyproject.toml
└── my_plugin/
    ├── __init__.py        ← empty or minimal
    └── signatures.py      ← the entry point callable
```

For Tier A, also create:
```
    └── sigs/
        ├── net_calls.yaml      ← optional: network call patterns
        ├── persistence.yaml    ← optional: persistence/worm patterns
        ├── obfuscation.yaml    ← optional: obfuscation patterns
        └── file_types.yaml     ← optional: suspicious filenames/types
```

---

## Step 3A — Tier A: YAML directory plugin

### `my_plugin/signatures.py`

```python
from pathlib import Path

def get_signatures_dir() -> Path:
    return Path(__file__).parent / "sigs"
```

### YAML files (only create the ones you need)

Use the same format as `checks/signatures/` in the core repo.

**`sigs/net_calls.yaml`** — outbound network calls, keyed by file extension:
```yaml
.py:
- pattern: 'evil_sdk\.fetch\b'
  desc: EvilSDK HTTP client (SupplyChainCorp campaign May 2026)
.js:
- pattern: 'require\s*\(\s*[''"]evil-fetch[''"]\s*\)'
  desc: evil-fetch npm package
```

**`sigs/persistence.yaml`** — OS persistence, self-propagation:
```yaml
patterns:
- pattern: 'crontab.*attacker\.sh'
  desc: cron-based persistence dropper
```

**`sigs/obfuscation.yaml`** — encoded payloads, keyed by extension:
```yaml
patterns:
  .js:
  - pattern: '_0xdeadbeef'
    desc: javascript-obfuscator hex variable names
```

**`sigs/file_types.yaml`** — suspicious filenames and binary types:
```yaml
suspicious_filenames:
  - evil.cfg
suspicious_path_prefixes:
  - .evil/
dangerous_binary_suffixes:
  - .evil
install_hook_names:
  - evil_install.sh
npm_install_scripts:
  - evil_install
```

**Single-quote pitfall:** `\b`, `\s`, `\.` all work as-is in single-quoted YAML strings. But `'''` inside a single-quoted string closes the string at the third quote — if your regex needs to match literal `'`, use `\S` or `[^\s]` instead, or rewrite to avoid `'` and `"` in the same character class.

### `pyproject.toml`

```toml
[project]
name = "dependency-scout-my-sigs"
version = "0.1.0"
dependencies = ["pyyaml>=6.0"]

[project.entry-points."dependency_scout.signatures"]
my_sigs = "my_plugin.signatures:get_signatures_dir"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## Step 3B — Tier B: Python provider plugin

### `my_plugin/signatures.py`

```python
from checks.signatures import SignatureContribution

def get_signatures() -> SignatureContribution:
    # Fetch from API, database, or generate programmatically
    patterns = _fetch_from_threat_feed()
    return SignatureContribution(
        net_call_patterns={".py": patterns["python_net_calls"]},
        persistence_patterns=patterns["persistence"],
    )
```

Only populate the fields you are contributing — omitted fields are ignored.
All pattern strings are raw regex strings; they are compiled internally.

`SignatureContribution` fields:
| Field | Type | What it adds to |
|---|---|---|
| `net_call_patterns` | `dict[str, list[str]]` | `NET_CALL_PATTERNS` (keyed by extension) |
| `obfuscation_patterns` | `dict[str, list[str]]` | `OBFUSCATION_PATTERNS` (keyed by extension) |
| `persistence_patterns` | `list[str]` | `PERSISTENCE_PATTERNS` |
| `suspicious_package_files` | `list[str]` | `SUSPICIOUS_PACKAGE_FILES` |
| `suspicious_package_prefixes` | `list[str]` | `SUSPICIOUS_PACKAGE_PREFIXES` |
| `dangerous_binary_suffixes` | `list[str]` | `DANGEROUS_BINARY_SUFFIXES` |
| `install_hook_names` | `list[str]` | `INSTALL_HOOK_NAMES` |
| `npm_install_scripts` | `list[str]` | `NPM_INSTALL_SCRIPTS` |

### `pyproject.toml`

```toml
[project]
name = "dependency-scout-my-provider"
version = "0.1.0"
dependencies = ["dependency-scout"]  # for SignatureContribution

[project.entry-points."dependency_scout.signature_providers"]
my_provider = "my_plugin.signatures:get_signatures"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## Step 4 — Install and verify

```bash
# From the core repo, install your plugin in editable mode
uv pip install -e ../my-plugin

# Verify the entry point is discovered
python -c "
from importlib.metadata import entry_points
eps = entry_points(group='dependency_scout.signatures')  # or signature_providers
print([ep.name for ep in eps])
"

# Verify patterns are merged into the constants
python -c "
from checks.signatures import NET_CALL_PATTERNS, PERSISTENCE_PATTERNS
print('Extensions covered:', list(NET_CALL_PATTERNS.keys()))
print('Persistence pattern count:', len(PERSISTENCE_PATTERNS))
"
```

---

## Step 5 — Test your patterns

Write a test that directly imports from `checks.signatures` after installing your plugin and checks that your patterns are present:

```python
from checks.signatures import NET_CALL_PATTERNS, PERSISTENCE_PATTERNS

def test_my_pattern_is_loaded():
    patterns = NET_CALL_PATTERNS.get(".py", [])
    assert any(p.search("evil_sdk.fetch(url)") for p in patterns)
```

Run the core test suite to confirm nothing regresses:
```bash
uv run pytest -x -q
```

---

## Common pitfalls

- **Entry point not discovered** — run `uv pip install -e .` in your plugin directory; entry points only register on install.
- **`dependency_scout.signatures` vs `dependency_scout.signature_providers`** — wrong group means silent no-op; the discovery loop skips unrecognised groups entirely.
- **Tier B: exceptions crash silently** — broken providers are caught and logged as WARNING, not raised. If your patterns aren't showing up, check the logs at WARNING level.
- **Tier A: missing YAML files are skipped** — you don't need all four YAML files; only the ones present in your `sigs/` directory are merged. But a present file that fails to parse is also silently skipped (with a WARNING).
- **Pattern too broad** — test with real package diffs, not just unit tests. A pattern like `fetch` will fire on half the internet.
