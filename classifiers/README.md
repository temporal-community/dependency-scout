# Classifiers

**When do you need a new classifier?** When you want to use a different LLM or decision engine to turn the collected checks into a verdict — for example, to use a model your company already has a contract with, or to run fully offline.

A classifier takes all the checks gathered by `PackageTriageWorkflow` and produces a **GREEN / YELLOW / RED** verdict with a human-readable explanation. Swapping classifiers is how you trade cost, latency, and accuracy against each other.

## Built-in classifiers

| File | Class | What drives it | Required config |
|---|---|---|---|
| `anthropic.py` | `AnthropicClassifier` | Anthropic API | `ANTHROPIC_API_KEY`; optionally `ANTHROPIC_MODEL` (default: `claude-sonnet-4-6`) |
| `openai.py` | `OpenAIClassifier` | OpenAI API | `OPENAI_API_KEY`; optionally `OPENAI_MODEL` (default: `gpt-5.5`) |
| `ollama.py` | `OllamaClassifier` | Local [Ollama](https://ollama.com) instance — no API key needed | `OLLAMA_HOST` (default: `http://localhost:11434`) and `OLLAMA_MODEL` (default: `llama4`) |
| `_helpers.py` | `RuleBasedClassifier` (via `__init__.py`) | Deterministic threshold rules — zero API keys, zero cost | Nothing |

## How a classifier is selected

At startup, `get_classifier()` in `__init__.py` picks one in this order:

1. `CLASSIFIER` env var — matched against the `dependency_scout.classifiers` entry point group first, then built-in names (`claude`, `openai`, `ollama`, `rule_based`)
2. `ANTHROPIC_API_KEY` is set → `AnthropicClassifier`
3. Fallback → `RuleBasedClassifier`

Zero config gets you a working (rule-based) system, and adding `ANTHROPIC_API_KEY` upgrades it to LLM classification automatically.

---

To add a built-in classifier to this repo, see [docs/contributing.md](../docs/contributing.md#swapping-the-classifier). To build a classifier plugin without modifying this repo, see [docs/extending.md](../docs/extending.md#classifier-plugins).
