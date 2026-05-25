"""
Lightweight metadata for built-in and plugin ecosystem providers.

This module has stdlib-only imports so it can be read without pulling in
httpx, temporalio, or any provider implementation.  EcosystemMeta is the
single source of truth for slug/osv_name/name_re — provider classes no
longer need to repeat these as class attributes (though they may for
self-documentation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EcosystemMeta:
    """Lightweight descriptor for one ecosystem.

    Built-in providers are listed in BUILTIN_ECOSYSTEMS below.
    Plugin providers register an instance of this class via the
    ``dependency_scout.ecosystems`` entry point group — no full provider
    class needed at metadata-load time.

    Fields
    ------
    name            Scout ecosystem key, e.g. "pip", "npm"
    dependabot_slug Dependabot branch prefix, e.g. "npm_and_yarn"
    osv_name        OSV ecosystem string, e.g. "PyPI", "npm"
    name_re         Package-name validation regex (used by the webhook allowlist)
    module          Dotted import path of the provider module, e.g. "ecosystems.pip"
    class_name      Class within that module, e.g. "PipProvider"
    """

    name: str
    dependabot_slug: str
    osv_name: str
    name_re: re.Pattern
    module: str
    class_name: str


BUILTIN_ECOSYSTEMS: list[EcosystemMeta] = [
    EcosystemMeta(
        name="pip",
        dependabot_slug="pip",
        osv_name="PyPI",
        name_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,213}$"),
        module="ecosystems.pip",
        class_name="PipProvider",
    ),
    EcosystemMeta(
        name="npm",
        dependabot_slug="npm_and_yarn",
        osv_name="npm",
        name_re=re.compile(r"^(@[A-Za-z0-9._-]+/)?[A-Za-z0-9][A-Za-z0-9._-]{0,213}$"),
        module="ecosystems.npm",
        class_name="NpmProvider",
    ),
    EcosystemMeta(
        name="cargo",
        dependabot_slug="cargo",
        osv_name="crates.io",
        name_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"),
        module="ecosystems.cargo",
        class_name="CargoProvider",
    ),
    EcosystemMeta(
        name="rubygems",
        dependabot_slug="bundler",
        osv_name="RubyGems",
        name_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,213}$"),
        module="ecosystems.rubygems",
        class_name="RubyGemsProvider",
    ),
    EcosystemMeta(
        name="maven",
        dependabot_slug="maven",
        osv_name="Maven",
        name_re=re.compile(
            r"^[A-Za-z0-9][A-Za-z0-9._-]{0,213}:[A-Za-z0-9][A-Za-z0-9._-]{0,213}$"
        ),
        module="ecosystems.maven",
        class_name="MavenProvider",
    ),
    EcosystemMeta(
        name="nuget",
        dependabot_slug="nuget",
        osv_name="NuGet",
        name_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,213}$"),
        module="ecosystems.nuget",
        class_name="NuGetProvider",
    ),
    EcosystemMeta(
        name="go",
        dependabot_slug="go_modules",
        osv_name="Go",
        name_re=re.compile(r"^(?!.*\.\.)[a-zA-Z0-9][a-zA-Z0-9._/\-~]{0,499}$"),
        module="ecosystems.gomod",
        class_name="GoModulesProvider",
    ),
    EcosystemMeta(
        name="composer",
        dependabot_slug="composer",
        osv_name="Packagist",
        name_re=re.compile(
            r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
        ),
        module="ecosystems.composer",
        class_name="ComposerProvider",
    ),
]
