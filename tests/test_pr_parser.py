import pytest
from helpers.pr_parser import parse_pr


@pytest.mark.parametrize("title,pkg,old,new", [
    (
        "Bump requests from 2.31.0 to 2.32.0",
        "requests", "2.31.0", "2.32.0",
    ),
    (
        "Bump requests from 2.31.0 to 2.32.0 in /foundations/hello_world",
        "requests", "2.31.0", "2.32.0",
    ),
    (
        "build(deps): bump litellm from 1.30.1 to 1.30.2",
        "litellm", "1.30.1", "1.30.2",
    ),
    (
        "Bump actions/checkout from 3 to 4",
        "actions/checkout", "3", "4",
    ),
])
def test_dependabot_titles(title, pkg, old, new):
    result = parse_pr(title)
    assert result is not None
    assert result.package == pkg
    assert result.old_version == old
    assert result.new_version == new


@pytest.mark.parametrize("title,pkg,new", [
    ("Update dependency requests to v2.32.0", "requests", "2.32.0"),
    ("chore(deps): update dependency litellm to 1.30.2", "litellm", "1.30.2"),
    ("Update dependency numpy to v2.0.0", "numpy", "2.0.0"),
])
def test_renovate_titles(title, pkg, new):
    result = parse_pr(title)
    assert result is not None
    assert result.package == pkg
    assert result.new_version == new


def test_unknown_title_returns_none():
    assert parse_pr("Fix typo in README") is None
    assert parse_pr("chore: update CI config") is None


def test_renovate_extracts_old_version_from_body():
    title = "Update dependency requests to v2.32.0"
    body = "| requests | from `2.31.0` to `2.32.0` |"
    result = parse_pr(title, body)
    assert result is not None
    assert result.old_version == "2.31.0"


def test_renovate_uses_unknown_when_body_has_no_match():
    title = "Update dependency requests to v2.32.0"
    result = parse_pr(title, "No version info here.")
    assert result is not None
    assert result.old_version == "unknown"


# ---------------------------------------------------------------------------
# npm ecosystem detection
# ---------------------------------------------------------------------------

def test_dependabot_npm_branch_detected():
    result = parse_pr(
        "Bump lodash from 4.17.20 to 4.17.21",
        branch="dependabot/npm_and_yarn/lodash-4.17.21",
    )
    assert result is not None
    assert result.ecosystem == "npm"
    assert result.package == "lodash"


def test_dependabot_pip_branch_detected():
    result = parse_pr(
        "Bump requests from 2.31.0 to 2.32.0",
        branch="dependabot/pip/requests-2.32.0",
    )
    assert result is not None
    assert result.ecosystem == "pip"


def test_scoped_npm_package_detected():
    result = parse_pr("Bump @typescript-eslint/parser from 6.0.0 to 6.1.0")
    assert result is not None
    assert result.ecosystem == "npm"
    assert result.package == "@typescript-eslint/parser"


def test_scoped_npm_package_renovate():
    result = parse_pr("Update dependency @typescript-eslint/eslint-plugin to v6.1.0")
    assert result is not None
    assert result.ecosystem == "npm"
    assert result.package == "@typescript-eslint/eslint-plugin"


def test_unknown_branch_defaults_to_pip():
    result = parse_pr("Bump requests from 2.31.0 to 2.32.0", branch="feature/my-branch")
    assert result is not None
    assert result.ecosystem == "pip"


def test_dependabot_bundler_maps_to_rubygems():
    result = parse_pr(
        "Bump gem from 1.0.0 to 1.1.0",
        branch="dependabot/bundler/gem-1.1.0",
    )
    assert result is not None
    assert result.ecosystem == "rubygems"


def test_dependabot_unknown_ecosystem_slug_falls_back():
    result = parse_pr(
        "Bump some-pkg from 1.0.0 to 1.1.0",
        branch="dependabot/gradle/some-pkg-1.1.0",  # gradle not in the map
    )
    assert result is not None
    assert result.ecosystem == "pip"  # unknown slug → default


def test_dependabot_cargo_detected():
    result = parse_pr(
        "Bump serde from 1.0.100 to 1.0.200",
        branch="dependabot/cargo/serde-1.0.200",
    )
    assert result is not None
    assert result.ecosystem == "cargo"
