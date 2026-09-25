"""
Part B: would earned trust have let the right agent PRs through? A replay of
real merges and real reverts.

    python eval/aidev_classes.py     # writes data/aidev/pr_classes.json
    GH_TOKEN=$(gh auth token) python eval/aidev_reverts.py
    python eval/aidev_replay.py

For the repositories aidev_reverts.py covered, every merged agent PR is
replayed in time order under each policy in replay.py. A PR counts as reverted
if a revert landed within 30 days of its merge; that revert is known to the
policies only from the moment it happened.

Two numbers matter per policy: **unattended**, the share of merged PRs that
needed no person, and **leak rate**, the share of those that were later
reverted. Merge-when-green bounds throughput from above; never-auto bounds
leaks from below.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from replay import MergedPR, Outcome, Tenure, replay  # noqa: E402

DATA = ROOT / "data" / "aidev"
OUT = ROOT / "eval" / "results"
WINDOW = timedelta(days=30)
EARNABLE = ("docs", "tests", "deps_patch")
ALL_BUT_NEVER = ("docs", "tests", "deps_patch", "deps", "code")

POLICIES: list[tuple[str, str | Tenure]] = [
    ("merge when green", "merge_when_green"),
    ("path rules only", "path_rules"),
    ("Tenure: n=5, docs/tests/patch bumps, per agent", Tenure(5, timedelta(days=7), EARNABLE, True)),
    ("Tenure: n=5, same, trust pooled across agents", Tenure(5, timedelta(days=7), EARNABLE, False)),
    ("Tenure: n=3, docs/tests/patch bumps, per agent", Tenure(3, timedelta(days=7), EARNABLE, True)),
    ("Tenure: n=10, docs/tests/patch bumps, per agent", Tenure(10, timedelta(days=7), EARNABLE, True)),
    ("Tenure: n=5, code too (needs the verifier), per agent", Tenure(5, timedelta(days=7), ALL_BUT_NEVER, True)),
    ("Tenure: n=5, code too, trust pooled across agents", Tenure(5, timedelta(days=7), ALL_BUT_NEVER, False)),
    ("never auto-merge", "never"),
]


def load() -> tuple[dict[str, list[MergedPR]], dict]:
    reverts = json.loads((DATA / "reverts.json").read_text())
    classes = {int(k): v for k, v in json.loads((DATA / "pr_classes.json").read_text()).items()}
    covered = set(reverts["repos"]) - set(reverts["unreachable"])
    reverted = {(r["repo"], r["number"]): pd.Timestamp(r["reverted_at"]).to_pydatetime()
                for r in reverts["reverts"]}

    pr = pd.read_parquet(DATA / "pull_request.parquet", columns=["id", "number", "agent", "repo_url", "merged_at"])
    pr = pr[pr.merged_at.notna()].copy()
    pr["repo"] = pr.repo_url.str.replace("https://api.github.com/repos/", "", regex=False)
    pr = pr[pr.repo.isin(covered)]
    by_repo: dict[str, list[MergedPR]] = {}
    unclassified = late = 0
    for r in pr.itertuples():
        cls = classes.get(int(r.id))
        if cls is None:
            unclassified += 1
            continue
        merged_at = pd.Timestamp(r.merged_at).to_pydatetime()
        rev = reverted.get((r.repo, int(r.number)))
        if rev is not None and rev - merged_at > WINDOW:
            late += 1
            rev = None
        by_repo.setdefault(r.repo, []).append(MergedPR(r.repo, int(r.number), r.agent, cls, merged_at, rev))
    return by_repo, {"repos": len(by_repo), "unclassified": unclassified, "reverted_after_window": late,
                     "matched_by": reverts["matched_by"]}


def run(by_repo: dict[str, list[MergedPR]]) -> list[tuple[str, Outcome]]:
    rows = []
    for name, policy in POLICIES:
        total = Outcome()
        for prs in by_repo.values():
            total.add(replay(prs, policy))
        rows.append((name, total))
    return rows


def table(rows: list[tuple[str, Outcome]]) -> str:
    lines = ["| Policy | merged unattended | later reverted among them | leak rate | reverted PRs a person saw first |",
             "|---|---:|---:|---:|---:|"]
    for name, o in rows:
        share = f"{o.unattended:,} ({100 * o.unattended / o.merged:.1f}%)"
        leak = f"{100 * o.leaked / o.unattended:.2f}%" if o.unattended else "n/a"
        seen = (f"{o.held_reverts:,} of {o.reverted:,} ({100 * o.held_reverts / o.reverted:.0f}%)"
                if o.reverted else "n/a")
        lines.append(f"| {name} | {share} | {o.leaked:,} | {leak} | {seen} |")
    return "\n".join(lines)


def rates(prs: list[MergedPR], field: str) -> str:
    n, r = Counter(), Counter()
    for p in prs:
        n[getattr(p, field)] += 1
        r[getattr(p, field)] += p.reverted_at is not None
    head = f"| {field.replace('_', ' ')} | merged | reverted within 30 days |\n|---|---:|---:|"
    return head + "".join(f"\n| {k} | {n[k]:,} | {r[k]:,} ({100 * r[k] / n[k]:.2f}%) |"
                          for k in sorted(n, key=lambda k: -n[k]))


def main() -> int:
    by_repo, meta = load()
    everything = [p for prs in by_repo.values() for p in prs]
    big = sorted(by_repo, key=lambda r: -len(by_repo[r]))[:2]
    rest = {r: prs for r, prs in by_repo.items() if r not in big}

    sections = [
        f"{meta['repos']} repositories, {len(everything):,} merged agent PRs "
        f"({meta['unclassified']:,} without file rows left out; {meta['reverted_after_window']:,} reverted "
        f"only after 30 days, counted as not reverted).",
        "### All repositories\n\n" + table(run(by_repo)),
        f"### Without the two largest ({', '.join(big)}: {sum(len(by_repo[r]) for r in big):,} PRs)\n\n"
        + table(run(rest)),
        "### Revert rates in this sample\n\n" + rates(everything, "agent") + "\n\n" + rates(everything, "change_class"),
    ]
    text = "\n\n".join(sections) + "\n"
    print(text)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "aidev_part_b.md").write_text(text, encoding="utf-8")
    results = {name: {k: getattr(o, k) for k in ("merged", "reverted", "unattended", "leaked", "grants",
                                                 "revocations", "leaked_prs")}
               for name, o in run(by_repo)}
    (OUT / "aidev_part_b.json").write_text(json.dumps({"meta": meta, "largest_repos": big, "policies": results},
                                                      indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
