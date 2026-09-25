"""
Evidence: what happened after an unattended merge, and what it costs the grant.

Two kinds of evidence are acted on, and they are treated differently on
purpose:

  * **A revert.** Someone merged a PR undoing one of Tenure's merges. That's a
    person's judgement that the change was wrong, so the grant is revoked, and
    only a new grant — a fresh decision — restores it.
  * **A red build on the default branch, at the exact commit Tenure's merge
    created.** Builds on main fail for reasons that aren't the change: flaky
    tests, an outage. So the grant is suspended, not revoked. A person looks
    and renews it if the build was noise; until then it doesn't hold.

A revert is recognised from GitHub's own wording. The revert button writes
"Reverts owner/repo#123" in the body. A hand-written revert usually keeps the
title "Revert \"<original title>\"", which is matched against the ledger's
titles only when exactly one merge carries that title. A revert of someone
else's PR, or of another repository's, is not evidence about Tenure.

Nothing here can add autonomy. That asymmetry is the design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .grants import GrantStore
from .ledger import Ledger, Merge

_REVERTS = re.compile(r"\bReverts\s+([\w.-]+/[\w.-]+)#(\d+)\b")
_REVERT_TITLE = re.compile(r'^Revert\s+"(.+)"\s*$')
_FAILED = frozenset({"failure", "timed_out", "startup_failure"})


@dataclass(frozen=True)
class Evidence:
    kind: str               # reverted | broke_main
    merge: Merge
    detail: str


def reverted(repo: str, title: str, body: str | None, ledger: Ledger) -> Evidence | None:
    """A merged PR that reverts one of Tenure's merges, or None."""
    for m in _REVERTS.finditer(body or ""):
        if m.group(1).lower() == repo.lower():
            hit = ledger.by_pr(repo, int(m.group(2)))
            if hit:
                return Evidence("reverted", hit, f"#{hit.pr} was reverted")
    t = _REVERT_TITLE.match(title or "")
    if t:
        hits = ledger.by_title(repo, t.group(1))
        if len(hits) == 1:
            return Evidence("reverted", hits[0], f"#{hits[0].pr} was reverted")
    return None


def broke_main(repo: str, head_branch: str | None, default_branch: str, head_sha: str,
               conclusion: str | None, ledger: Ledger) -> Evidence | None:
    """A failed build on the default branch at a commit Tenure's merge created."""
    if conclusion not in _FAILED or head_branch != default_branch:
        return None
    hit = ledger.by_merge_sha(repo, head_sha)
    if hit is None:
        return None
    return Evidence("broke_main", hit, f"the build failed on {default_branch} right after #{hit.pr} merged")


def apply(evidence: Evidence, store: GrantStore, *, now: datetime) -> str:
    """Take the autonomy away. Returns what happened, for the audit and a comment."""
    repo, agent, cls = evidence.merge.grant_scope
    if evidence.kind == "reverted":
        done = store.revoke(repo, agent, cls, by="tenure", reason=evidence.detail, now=now)
        return (f"Revoked {agent}'s {cls} grant: {evidence.detail}." if done
                else f"{agent}'s {cls} grant was already gone; {evidence.detail}.")
    grant = store.get(repo, agent, cls)
    if grant is None:
        return f"{agent}'s {cls} grant was already gone; {evidence.detail}."
    store.suspend(grant, reason=evidence.detail, now=now)
    return (f"Suspended {agent}'s {cls} grant: {evidence.detail}. If the build failure wasn't the change, "
            f"a maintainer can renew it with /tenure grant.")
