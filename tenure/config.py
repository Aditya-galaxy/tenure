"""
A repository's policy, read from `.tenure.yml` on its default branch.

The file is read from the default branch only, never from the PR under review
— otherwise a PR could rewrite the rules it is judged by. For the same reason
a PR that touches `.tenure.yml` is never merged unattended (it is in
ALWAYS_NEVER below).

A repository can *add* never-auto paths and *shorten* the longest grant it
will accept. It cannot remove a built-in never-auto path or lengthen the cap:
configuration can make Tenure stricter, never looser.

An unreadable or invalid file raises ConfigError, and the caller holds every
PR until it is fixed — a broken policy is not a permissive one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import yaml

RULES_VERSION = "2026-09-25.1"
HARD_MAX_GRANT_DAYS = 180

# Paths no grant can cover, in every repository.
ALWAYS_NEVER: tuple[str, ...] = (
    # The policy, and who reviews what.
    ".tenure.yml", "CODEOWNERS",
    # CI: an agent that edits CI can change which checks its own PR must pass.
    ".github/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile", ".buildkite/**",
    "azure-pipelines.yml", ".drone.yml",
    # Supply chain and licensing.
    ".gitmodules", "LICENSE*", "COPYING*", "NOTICE*",
    # Instructions to coding agents. Changing these writes the prompt the next
    # agent will follow — markdown that must not pass as harmless docs.
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".cursor/**", ".claude/**",
    ".windsurfrules", ".clinerules",
)

DOC_PATTERNS: tuple[str, ...] = ("docs/**", "doc/**", "*.md", "*.rst", "*.adoc")

TEST_PATTERNS: tuple[str, ...] = (
    "tests/**", "test/**", "spec/**", "**/__tests__/**",
    "*_test.*", "test_*.py", "*.test.*", "*.spec.*", "*_spec.rb",
)

MANIFESTS: tuple[str, ...] = (
    "package.json", "Cargo.toml", "pyproject.toml", "requirements*.txt", "go.mod",
    "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Pipfile",
)

LOCKFILES: tuple[str, ...] = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "poetry.lock",
    "uv.lock", "go.sum", "Gemfile.lock", "composer.lock", "Pipfile.lock",
)

_KNOWN_KEYS = {"never_auto", "max_grant_days", "agents"}


class ConfigError(ValueError):
    """The policy file can't be trusted, so nothing merges unattended."""


@dataclass(frozen=True)
class RepoPolicy:
    never_auto: tuple[str, ...] = ALWAYS_NEVER
    max_grant_days: int = 90
    extra_agents: dict[int, str] = field(default_factory=dict)

    @property
    def rule_version(self) -> str:
        """A fingerprint of everything that decides how a change is classified.
        A grant records it; if the rules change, the grant stops holding until a
        person renews it against the new rules."""
        material = json.dumps({
            "rules": RULES_VERSION,
            "never_auto": sorted(self.never_auto),
            "docs": DOC_PATTERNS, "tests": TEST_PATTERNS,
            "manifests": MANIFESTS, "lockfiles": LOCKFILES,
        }, sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest()[:16]


def parse(text: str | None) -> RepoPolicy:
    """Policy from the file's text; None or empty means the defaults."""
    if not text or not text.strip():
        return RepoPolicy()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f".tenure.yml is not valid YAML: {exc}") from None
    if data is None:
        return RepoPolicy()
    if not isinstance(data, dict):
        raise ConfigError(".tenure.yml must be a mapping")
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        # A misspelt key ("never-auto") would otherwise be ignored silently and
        # the paths it meant to protect would not be protected.
        raise ConfigError(f".tenure.yml has unknown keys: {', '.join(sorted(unknown))}")

    extra = data.get("never_auto", []) or []
    if not isinstance(extra, list) or not all(isinstance(p, str) and p.strip() for p in extra):
        raise ConfigError("never_auto must be a list of path patterns")

    days = data.get("max_grant_days", 90)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= HARD_MAX_GRANT_DAYS:
        raise ConfigError(f"max_grant_days must be a whole number from 1 to {HARD_MAX_GRANT_DAYS}")

    agents = data.get("agents", {}) or {}
    if not isinstance(agents, dict) or not all(
            isinstance(k, int) and not isinstance(k, bool) and isinstance(v, str) and v.strip()
            for k, v in agents.items()):
        raise ConfigError("agents must map numeric bot user IDs to names")

    return RepoPolicy(never_auto=ALWAYS_NEVER + tuple(p.strip() for p in extra),
                      max_grant_days=days, extra_agents=dict(agents))
