# Contributing

## Dev setup

```bash
uv sync
uv run pre-commit install
```

The pre-commit hooks run `ruff` (lint + format) on every commit, matching what CI checks.

## Commands

```bash
uv run ruff format .          # format
uv run ruff check .           # lint
uv run mypy .                 # type check
uv run pytest                 # all tests
uv run pytest \
  --cov=activities --cov=ecosystems --cov=platforms \
  --cov=classifiers --cov=models \
  --cov=workflows --cov=helpers --cov=api --cov=checks \
  --cov-report=term-missing   # coverage (target ≥95%)
```

## Replay tests

After intentional workflow changes, regenerate fixtures:

```bash
uv run python tests/generate_fixtures.py
```

See [docs/architecture.md](docs/architecture.md) for design details.
