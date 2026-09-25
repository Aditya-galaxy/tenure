"""
What kinds of change do coding agents actually make? Tenure's classifier over
~72,000 real agent pull requests.

    python eval/aidev_classes.py            # needs data/aidev/ (see below)

Data: AIDev (Li, Zhang and Hassan, 2026; huggingface.co/datasets/hao-li/AIDev,
tag v4), the pull requests Codex, Copilot, Devin, Cursor, Claude Code and Jules
opened in GitHub repositories with more than 100 stars, with every commit's
file-level changes. Download pull_request.parquet and pr_commit_details.parquet
into data/aidev/ (gitignored).

For each PR this gathers the files its commits touched and asks Tenure's
classifier what kind of change it is — docs, tests, a patch-level dependency
bump, other dependencies, code, or never-auto — using the default policy. It
also counts PRs that edit agent instruction files or MCP configs, and runs
Detonate's static scan over those edits.

Caveats, printed with the results:
  * The dataset has no previous filename for renames, so a rename out of a
    sensitive path is judged by its new name only.
  * A PR's files are the union across its commits; a file added then removed
    counts by its last status.
  * Diffs are kept only for manifests, lockfiles and instruction files — the
    only files whose class depends on the diff.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tenure import config, detonate  # noqa: E402
from tenure.classify import ORDER, ChangedFile, classify  # noqa: E402
from tenure.globs import any_match  # noqa: E402

DATA = ROOT / "data" / "aidev"
OUT = ROOT / "eval" / "results"
CI = (".github/workflows/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile", "azure-pipelines.yml")
NEEDS_DIFF = config.MANIFESTS + config.LOCKFILES


def _needs_diff(path: str) -> bool:
    return (any_match(path, NEEDS_DIFF, sensitive=False) is not None) or detonate.is_instruction_file(path)


def gather() -> dict[int, dict[str, list]]:
    """pr_id -> filename -> [status, additions, deletions, patch parts]."""
    files: dict[int, dict[str, list]] = defaultdict(dict)
    table = pq.ParquetFile(DATA / "pr_commit_details.parquet")
    cols = ["pr_id", "filename", "status", "additions", "deletions", "patch"]
    for batch in table.iter_batches(batch_size=50_000, columns=cols):
        d = batch.to_pydict()
        for pr_id, name, status, add, dele, patch in zip(d["pr_id"], d["filename"], d["status"], d["additions"],
                                                         d["deletions"], d["patch"], strict=True):
            if not name:
                continue
            entry = files[pr_id].setdefault(name, [status, 0, 0, []])
            entry[0] = status
            entry[1] += int(add or 0)
            entry[2] += int(dele or 0)
            if patch and _needs_diff(name):
                entry[3].append(patch)
    return files


def main() -> int:
    prs = pq.read_table(DATA / "pull_request.parquet", columns=["id", "agent", "merged_at"]).to_pydict()
    agent_of = dict(zip(prs["id"], prs["agent"], strict=True))
    merged = {i for i, m in zip(prs["id"], prs["merged_at"], strict=True) if m}
    print(f"  {len(agent_of):,} agent PRs; reading file-level changes…", flush=True)
    by_pr = gather()
    policy = config.RepoPolicy()

    classes: dict[str, Counter] = defaultdict(Counter)
    merged_classes: dict[str, Counter] = defaultdict(Counter)
    touches: dict[str, Counter] = defaultdict(Counter)
    detonate_kinds: Counter = Counter()
    detonate_prs: Counter = Counter()          # PRs by the most severe finding
    examples: dict[str, list[str]] = defaultdict(list)
    without_files = 0

    for pr_id, agent in agent_of.items():
        f = by_pr.get(pr_id)
        if not f:
            without_files += 1
            continue
        changed = [ChangedFile(name, status or "modified", add, dele, "\n".join(parts) if parts else None)
                   for name, (status, add, dele, parts) in f.items()]
        cls = classify(changed, policy).change_class
        classes[agent][cls] += 1
        if pr_id in merged:
            merged_classes[agent][cls] += 1
        paths = [c.path for c in changed]
        if any(any_match(p, CI, sensitive=True) for p in paths):
            touches[agent]["CI config"] += 1
        instr = [c for c in changed if detonate.is_instruction_file(c.path)]
        if instr:
            touches[agent]["agent instructions / MCP"] += 1
            if len(examples[agent]) < 5:
                examples[agent].append(f"{pr_id}: {', '.join(c.path for c in instr)[:120]}")
            found = detonate.static_scan(instr)
            for finding in found:
                detonate_kinds[f"{finding.severity} {finding.kind}"] += 1
            worst = min((f.severity for f in found), key=["high", "medium", "info"].index, default="clean")
            detonate_prs[worst] += 1

    agents = sorted(classes, key=lambda a: -sum(classes[a].values()))
    total = Counter()
    for a in agents:
        total.update(classes[a])

    lines = ["| Agent | PRs | " + " | ".join(ORDER) + " | touch CI | touch agent instructions |",
             "|---|---:|" + "---:|" * len(ORDER) + "---:|---:|"]
    for a in [*agents, "all"]:
        c = total if a == "all" else classes[a]
        n = sum(c.values())
        t = Counter() if a == "all" else touches[a]
        if a == "all":
            for x in agents:
                t.update(touches[x])
        cells = " | ".join(f"{100 * c[k] / n:.1f}%" for k in ORDER)
        lines.append(f"| {a} | {n:,} | {cells} | {100 * t['CI config'] / n:.1f}% | "
                     f"{100 * t['agent instructions / MCP'] / n:.2f}% |")
    table = "\n".join(lines)

    print("\n" + table)
    print(f"\n  {without_files:,} PRs had no file-level rows in the dataset and are left out.")
    print(f"  Detonate static findings on instruction-file edits: {dict(detonate_kinds) or 'none'}")
    print(f"  instruction-file PRs by most severe finding: {dict(detonate_prs)}")
    for a in agents:
        if examples[a]:
            print(f"  e.g. {a}: {examples[a][0]}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "aidev_part_a.json").write_text(json.dumps({
        "dataset": "hao-li/AIDev v4 (pull_request, pr_commit_details)",
        "classes": {a: dict(classes[a]) for a in agents},
        "merged_classes": {a: dict(merged_classes[a]) for a in agents},
        "touches": {a: dict(touches[a]) for a in agents},
        "detonate_findings": dict(detonate_kinds),
        "detonate_prs_by_worst_finding": dict(detonate_prs),
        "without_file_rows": without_files,
        "examples": dict(examples),
    }, indent=1), encoding="utf-8")
    (OUT / "aidev_part_a.md").write_text(table + "\n", encoding="utf-8")
    print(f"\n  written: {OUT.relative_to(ROOT)}/aidev_part_a.json and .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
