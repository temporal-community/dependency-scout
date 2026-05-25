# Signatures

**When do you need to edit a file here?** When you want to add detection coverage for a new attack pattern — a new HTTP client library seen in a supply chain attack, a new persistence mechanism, a new obfuscation technique. No Python required.

These YAML files are the pattern store for `checks/package_diff.py`. They define every regex the diff check uses to flag suspicious code in package archives. `__init__.py` loads all four files at startup, compiles the regexes, and exports them as typed constants.

## Files

| File | What it covers | Structure |
|---|---|---|
| `net_calls.yaml` | Outbound network calls in library code, keyed by file extension | `{".py": [{pattern, desc}, ...], ".js": [...], ...}` |
| `obfuscation.yaml` | Encoded payloads, zero-width Unicode tricks, gzip+base64 blobs | Nested: `patterns` (by extension), `gzip_b64`, `zero_width` |
| `persistence.yaml` | OS persistence mechanisms, npm worm propagation | `patterns` list + `worm_propagation` compound rule |
| `file_types.yaml` | Suspicious filenames, dangerous binary extensions, install hook names | `suspicious_filenames`, `suspicious_path_prefixes`, `dangerous_binary_suffixes`, `install_hook_names`, `npm_install_scripts` |

## Adding a pattern

Use the `/add-detection` skill in Claude Code for a guided walkthrough. Or edit directly:

1. Add a pattern to the appropriate YAML file using **single-quoted** strings (backslashes work as-is: `\b`, `\s`, `\.`).
2. Run `uv run pytest tests/test_signatures.py -v` to confirm the YAML loads and your pattern compiles.
3. Run `uv run pytest -x -q` for the full suite.

**Single-quote pitfall:** `''` inside a single-quoted YAML string is an escaped `'`. But `'''` closes the string at the third quote, leaving the rest as bare YAML. If your regex must match a literal `'`, use `\S` or `[^\s]` instead of a character class that mixes `'` and `"`.

## Plugin extension points

Third-party packages can contribute additional signatures without modifying these files:

| Entry point group | How it works |
|---|---|
| `dependency_scout.signatures` | Callable returns a `Path` to a directory of YAML files in the same format as this directory. Patterns are merged at import time. |
| `dependency_scout.signature_providers` | Callable returns a `SignatureContribution` dataclass with raw regex strings. For dynamically generated patterns (threat feeds, CVE APIs). |

See [docs/extending.md](../../docs/extending.md) and the `/add-signature-plugin` skill for details.
