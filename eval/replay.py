"""
Replay merge policies over a repository's history of agent PRs.

Each merged agent PR is an event: at its merge time some policy either lets it
merge unattended or sends it to a person. A later revert is a second event, at
the time it happened, so a policy only knows about a revert once it has
occurred. That is what keeps the replay honest: Tenure can't use a revert from
next month to hold a PR today.

Policies:

  * **merge when green**: every PR merges unattended (what "merge as soon as
    GitHub allows" does, taking every merged PR to have been green).
  * **path rules only**: every PR outside Tenure's never-auto paths merges
    unattended. This is static rules without earned trust.
  * **Tenure**: a (repo, agent, class) key earns a grant after `n` merges that
    are at least `wait` old with no revert in the key since. A merge under a
    grant is unattended. A revert of any PR in the key revokes the grant and
    resets its record, so the key has to earn it again. Only `earnable`
    classes can be granted. With `per_agent=False` trust is pooled across
    agents per (repo, class), the "one rule for AI PRs" alternative.
  * **never**: nothing merges unattended (the baseline for leaks).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class MergedPR:
    repo: str
    number: int
    agent: str
    change_class: str
    merged_at: datetime
    reverted_at: datetime | None = None      # already limited to the outcome window


@dataclass(frozen=True)
class Tenure:
    n: int = 5
    wait: timedelta = timedelta(days=7)
    earnable: tuple[str, ...] = ("docs", "tests", "deps_patch")
    per_agent: bool = True


@dataclass
class Outcome:
    merged: int = 0
    reverted: int = 0
    unattended: int = 0
    leaked: int = 0                          # unattended, then reverted
    grants: int = 0
    revocations: int = 0
    leaked_prs: list[str] = field(default_factory=list)

    def add(self, other: Outcome) -> None:
        for name in ("merged", "reverted", "unattended", "leaked", "grants", "revocations"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.leaked_prs += other.leaked_prs

    @property
    def held_reverts(self) -> int:
        """Reverted PRs that a person saw before they merged."""
        return self.reverted - self.leaked


def replay(prs: list[MergedPR], policy: str | Tenure) -> Outcome:
    out = Outcome(merged=len(prs), reverted=sum(p.reverted_at is not None for p in prs))

    if policy == "never":
        return out
    if policy in ("merge_when_green", "path_rules"):
        for p in prs:
            if policy == "merge_when_green" or p.change_class != "never":
                _unattended(out, p)
        return out
    assert isinstance(policy, Tenure), policy

    def key(p: MergedPR) -> tuple:
        return (p.repo, p.agent, p.change_class) if policy.per_agent else (p.repo, p.change_class)

    # Reverts sort before merges at the same instant: the policy has seen it.
    events = [(p.merged_at, 1, i) for i, p in enumerate(prs)]
    events += [(p.reverted_at, 0, i) for i, p in enumerate(prs) if p.reverted_at is not None]
    record: dict[tuple, list[datetime]] = defaultdict(list)    # clean merges since the last revert
    granted: set[tuple] = set()

    for at, kind, i in sorted(events):
        p = prs[i]
        k = key(p)
        if kind == 0:                                          # a revert becomes known
            if k in granted:
                granted.discard(k)
                out.revocations += 1
            record[k].clear()
            continue
        if p.change_class in policy.earnable:
            if k not in granted and sum(t <= at - policy.wait for t in record[k]) >= policy.n:
                granted.add(k)                                 # proposal, accepted by a maintainer
                out.grants += 1
            if k in granted:
                _unattended(out, p)
            record[k].append(at)
    return out


def _unattended(out: Outcome, p: MergedPR) -> None:
    out.unattended += 1
    if p.reverted_at is not None:
        out.leaked += 1
        out.leaked_prs.append(f"{p.repo}#{p.number}")
