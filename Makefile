.PHONY: bootstrap

bootstrap:
	uv sync
	uv run pre-commit install
