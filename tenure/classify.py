"""
What kind of change a pull request is, decided from its files alone.

Classes run from least to most consequential:

    docs < tests < deps_patch < deps < code < never

A PR takes the class of its most consequential file, and a grant for a class
covers everything below it: trusting an agent with dependency bumps includes
trusting it with a README fix, but not the reverse.

This is deterministic string and diff inspection, so every decision can name
the file and the rule behind it. A model may later be consulted, but only
through `tighten()`, which can move a PR up this order and never down.

What it cannot see: whether an edited test still asserts what it used to.
Test *count* and pass/fail are checked by running the base branch's test
command in a sandbox, which is separate from this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import config
from .globs import any_match

ORDER: tuple[str, ...] = ("docs", "tests", "deps_patch", "deps", "code", "never")
_RANK = {name: i for i, name in enumerate(ORDER)}


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str                       # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0
    patch: str | None = None          # unified diff hunk text; GitHub omits it for large files
    previous_path: str | None = None  # set for renames


@dataclass(frozen=True)
class Classification:
    change_class: str
    reasons: tuple[str, ...]
    rule_version: str


def stricter(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


# A dependency line that isn't only a version: sources, scripts, hooks and
# build settings are code execution or supply-chain changes, not bumps.
_NOT_A_BUMP = re.compile(
    r"script|build|run|exec|command|install|prepare|hook|url|git|path|registry|source|http|features",
    re.IGNORECASE)
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _changed_lines(patch: str) -> tuple[list[str], list[str]]:
    removed, added = [], []
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("-") and line[1:].strip():
            removed.append(line[1:])
        elif line.startswith("+") and line[1:].strip():
            added.append(line[1:])
    return removed, added


def _manifest_bump(patch: str | None) -> tuple[bool, bool, str]:
    """(is_only_versions, is_patch_level, why)."""
    if patch is None:
        return False, False, "diff not available, so it can't be shown to be only versions"
    removed, added = _changed_lines(patch)
    if not removed and not added:
        return True, True, "whitespace only"
    for line in removed + added:
        if not _VERSION.search(line):
            return False, False, f"changes a line that isn't a version: {line.strip()[:60]!r}"
        if _NOT_A_BUMP.search(line):
            return False, False, f"changes more than a version: {line.strip()[:60]!r}"
    if len(removed) != len(added):
        return True, False, "adds or removes dependencies"
    for old, new in zip(removed, added, strict=True):
        a, b = _VERSION.search(old), _VERSION.search(new)
        if not a or not b or (a.group(1), a.group(2)) != (b.group(1), b.group(2)):
            return True, False, "moves a major or minor version"
    return True, True, "patch-level version changes only"


def _file_class(f: ChangedFile) -> tuple[str, str]:
    path = f.path
    if any_match(path, config.LOCKFILES, sensitive=False):
        if f.status != "modified":
            return "code", f"{path}: lockfile {f.status}"
        return "deps", f"{path}: lockfile"
    if any_match(path, config.MANIFESTS, sensitive=False):
        if f.status != "modified":
            return "code", f"{path}: manifest {f.status}"
        only_versions, patch_level, why = _manifest_bump(f.patch)
        if not only_versions:
            return "code", f"{path}: {why}"
        return ("deps_patch" if patch_level else "deps"), f"{path}: {why}"
    if any_match(path, config.TEST_PATTERNS, sensitive=False):
        if f.status == "removed":
            return "code", f"{path}: removes a test file"
        if f.deletions > f.additions:
            return "code", f"{path}: removes more test lines than it adds"
        return "tests", f"{path}: test"
    if any_match(path, config.DOC_PATTERNS, sensitive=False):
        return "docs", f"{path}: documentation"
    return "code", f"{path}: code"


def classify(files: list[ChangedFile] | tuple[ChangedFile, ...],
             policy: config.RepoPolicy) -> Classification:
    if not files:
        return Classification("code", ("no files changed",), policy.rule_version)

    never = []
    for f in files:
        for side in (f.path, f.previous_path):
            if side:
                hit = any_match(side, policy.never_auto, sensitive=True)
                if hit:
                    never.append(f"{side}: matches never-auto pattern {hit!r}")
    if never:
        return Classification("never", tuple(never), policy.rule_version)

    per_file: list[tuple[ChangedFile, str, str]] = []
    for f in files:
        cls, why = _file_class(f)
        if f.status == "renamed" and f.previous_path:
            # Both sides count: moving a file out of the code tree into docs/
            # is a change to code, whatever it's called afterwards.
            before, why_before = _file_class(ChangedFile(f.previous_path, "modified",
                                                         f.additions, f.deletions, None))
            if _RANK[before] > _RANK[cls]:
                cls, why = before, f"{f.path}: renamed from {why_before}"
        per_file.append((f, cls, why))

    # A patch bump always drags its lockfile along — Dependabot never sends
    # one without the other — so a lockfile rides at deps_patch when the
    # manifests beside it are all patch bumps. On its own, a lockfile change
    # can carry any transitive upgrade and stays an ordinary deps change.
    manifest_classes = {cls for f, cls, _ in per_file
                        if any_match(f.path, config.MANIFESTS, sensitive=False)}
    if manifest_classes == {"deps_patch"}:
        per_file = [(f, "deps_patch" if any_match(f.path, config.LOCKFILES, sensitive=False) else cls,
                     why) for f, cls, why in per_file]

    overall = "docs"
    for _, cls, _ in per_file:
        overall = stricter(overall, cls)
    return Classification(overall, tuple(why for *_, why in per_file), policy.rule_version)


def tighten(c: Classification, proposed: str, why: str) -> Classification:
    """Apply a second opinion — a model's, say — that may only make a PR
    stricter. A looser or unrecognised proposal changes nothing it shouldn't:
    looser is ignored, unrecognised is treated as ordinary code."""
    proposed = proposed if proposed in _RANK else "code"
    if _RANK[proposed] <= _RANK[c.change_class]:
        return c
    return Classification(proposed, (*c.reasons, why), c.rule_version)
