"""
The decision: merge unattended, hold for a person, or never.

`decide()` is a pure function. It takes facts that have already been
established — who wrote the PR, what kind of change it is, what the sandbox
proved about the exact head commit, which grants exist — and returns a
verdict with every reason behind it. It makes no calls and reads no model
output directly: a model's opinion can only reach it as a stricter
classification (classify.tighten) or as a flag, and both can only hold a PR.

Everything that is missing counts against a merge. No verification, a
verification of a different commit, an owner whose access can't be confirmed,
a policy file that didn't parse — each one holds.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .classify import Classification
from .config import RepoPolicy
from .grants import Grant


class Verdict(StrEnum):
    AUTO_MERGE = "auto_merge"
    HOLD = "hold"
    NEVER = "never"
    IGNORE = "ignore"          # not an agent's PR: Tenure stays out of it


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    head_sha: str


@dataclass(frozen=True)
class Verification:
    """What the sandbox established about one exact commit."""
    head_sha: str
    tests_passed: bool
    tests_before: int
    tests_after: int
    reversible: bool           # the revert applied cleanly and the tests matched the baseline
    problem: str | None = None  # why the sandbox couldn't establish a result; always holds
    brief: str = ""            # for the reviewer's check run; decide() never reads it


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reasons: tuple[str, ...]
    agent: str | None
    change_class: str
    head_sha: str
    grant_scope: tuple[str, str, str] | None = None


def _access(check: Callable[[str], bool], login: str) -> bool:
    try:
        return bool(check(login))
    except Exception:          # noqa: BLE001 — an unanswerable question is a "no"
        return False


def decide(pr: PullRequest, classification: Classification, *, agent: str | None,
           grants: list[Grant], policy: RepoPolicy, now: datetime, halted: bool,
           verification: Verification | None, flags: tuple[str, ...] = (),
           owner_has_access: Callable[[str], bool]) -> Decision:
    """`grants` are the ones covering this repo, agent and class, narrowest
    first (GrantStore.covering). `owner_has_access(login)` answers whether a
    grant's owner can still maintain the repository."""
    cls = classification.change_class

    def verdict(v: Verdict, reasons: list[str] | tuple[str, ...],
                scope: tuple[str, str, str] | None = None) -> Decision:
        return Decision(v, tuple(reasons), agent, cls, pr.head_sha, scope)

    if agent is None:
        return verdict(Verdict.IGNORE, ["not written by a known agent; people review people"])
    if cls == "never":
        return verdict(Verdict.NEVER, classification.reasons)

    held: list[str] = []
    if halted:
        held.append("Tenure is halted in this repository")
    held.extend(f"flagged: {f}" for f in flags)

    if verification is None:
        held.append("not verified in a sandbox")
    elif verification.problem:
        held.append(f"couldn't verify in a sandbox: {verification.problem}")
    else:
        if verification.head_sha != pr.head_sha:
            held.append(f"the sandbox verified {verification.head_sha[:7]}, but the PR is now at "
                        f"{pr.head_sha[:7]}")
        if not verification.tests_passed:
            held.append("the base branch's tests fail with this change")
        if verification.tests_after < verification.tests_before:
            held.append(f"the change reduces the test count from {verification.tests_before} "
                        f"to {verification.tests_after}")
        if not verification.reversible:
            held.append("reverting it doesn't restore the tests' baseline, so it can't be shown "
                        "to be undoable")

    chosen: Grant | None = None
    if not grants:
        held.append(f"no grant lets {agent} merge {cls} changes here "
                    f"(a maintainer can: /tenure grant {agent} {cls})")
    else:
        why_not = []
        for g in grants:
            ok, why = g.holds(rule_version=classification.rule_version, now=now,
                              max_days=policy.max_grant_days,
                              owner_has_access=_access(owner_has_access, g.owner))
            if ok:
                chosen = g
                break
            why_not.append(f"grant for {g.change_class}: {why}")
        if chosen is None:
            held.extend(why_not)

    if held:
        return verdict(Verdict.HOLD, held)
    assert chosen is not None
    _, why = chosen.holds(rule_version=classification.rule_version, now=now,
                          max_days=policy.max_grant_days, owner_has_access=True)
    return verdict(Verdict.AUTO_MERGE,
                   [f"{agent} holds a {chosen.change_class} grant: {why}",
                    f"verified in a sandbox at {pr.head_sha[:7]}: tests pass and the change reverts cleanly"],
                   chosen.scope)
