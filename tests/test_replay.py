"""The policy replay behind the AIDev evaluation (eval/replay.py)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from replay import MergedPR, Tenure, replay

T0 = datetime(2025, 6, 1, tzinfo=UTC)


def pr(n: int, day: float, cls: str = "docs", agent: str = "codex", reverted_day: float | None = None) -> MergedPR:
    return MergedPR("o/r", n, agent, cls, T0 + timedelta(days=day),
                    None if reverted_day is None else T0 + timedelta(days=reverted_day))


def test_baselines():
    prs = [pr(1, 0), pr(2, 1, "never", reverted_day=2), pr(3, 2, "code")]
    assert replay(prs, "never").unattended == 0
    green = replay(prs, "merge_when_green")
    assert (green.unattended, green.leaked) == (3, 1)
    assert replay(prs, "path_rules").unattended == 2


def test_trust_is_earned_from_merges_old_enough_to_judge():
    prs = [pr(i, i) for i in range(1, 6)] + [pr(6, 6), pr(7, 13)]
    out = replay(prs, Tenure(n=5, wait=timedelta(days=7)))
    # On day 6 none of the first five is a week old; by day 13 all are.
    assert out.unattended == 1 and out.grants == 1


def test_a_revert_revokes_and_the_record_starts_again():
    prs = [pr(i, i) for i in range(1, 4)] + [pr(4, 10, reverted_day=12), pr(5, 13), pr(6, 30)]
    out = replay(prs, Tenure(n=3, wait=timedelta(days=0)))
    assert out.leaked == 1 and out.revocations == 1
    # PR 5 merges after the revert is known, so the key has no record left;
    # PR 6 has one clean merge behind it, not three, so it's held too.
    assert out.unattended == 1


def test_a_revert_is_only_known_once_it_happens():
    prs = [pr(i, i) for i in range(1, 4)] + [pr(4, 5), pr(5, 6, reverted_day=40)]
    out = replay(prs, Tenure(n=3, wait=timedelta(days=0)))
    assert out.unattended == 2 and out.leaked == 1


def test_only_earnable_classes_are_granted_and_agents_are_kept_apart():
    prs = [pr(i, i, "code") for i in range(1, 8)]
    assert replay(prs, Tenure(n=3, wait=timedelta(0))).unattended == 0
    mixed = [pr(i, i, agent="codex") for i in range(1, 5)] + [pr(9, 9, agent="devin")]
    assert replay(mixed, Tenure(n=3, wait=timedelta(0))).unattended == 1          # codex's 4th only
    assert replay(mixed, Tenure(n=3, wait=timedelta(0), per_agent=False)).unattended == 2
