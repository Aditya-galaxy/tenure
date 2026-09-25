"""
Which merged agent PRs were later reverted? AIDev holds only agent PRs, and
most reverts are made by people, so this asks GitHub.

    GH_TOKEN=$(gh auth token) python eval/aidev_reverts.py [--min-merged 20]

For every repository with at least --min-merged merged agent PRs, two searches:

  * merged PRs with "Revert" in the title (GitHub's revert button writes
    `Revert "<title>"` and "Reverts owner/repo#N" in the body);
  * default-branch commits saying "This reverts commit <sha>" (git revert).

A revert is tied to the PR it undoes by, in order: "Reverts owner/repo#N";
`(#N)` in the reverted title (squash merges); the reverted sha appearing in
one of the PR's own commits (AIDev pr_commits); an exact title match; and last,
GitHub's commit → pull request lookup. Only reverts of merged agent PRs are
kept, earliest first. Responses are cached under data/aidev/github/ so a rerun
costs nothing; the output is data/aidev/reverts.json.

What this misses, stated with the results: fixes that undo a change without
the word "revert", partial reverts written by hand, and repos renamed or
deleted since the dataset was collected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "aidev"
CACHE = DATA / "github"
API = "https://api.github.com"

_REVERTS_REF = re.compile(r"Reverts\s+([\w.-]+/[\w.-]+)#(\d+)")
_THIS_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_REVERTED_TITLE = re.compile(r'^\s*Revert\s+"(.+)"\s*$', re.S)
_PR_REF = re.compile(r"\(#(\d+)\)\s*$")


class GitHub:
    def __init__(self) -> None:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("set GH_TOKEN, e.g. GH_TOKEN=$(gh auth token) python eval/aidev_reverts.py")
        self.http = httpx.Client(base_url=API, timeout=30, follow_redirects=True, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        self.last_search = 0.0

    def get(self, path: str, params: dict | None = None, *, search: bool = False) -> dict | list | None:
        key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()[:24]
        cached = CACHE / f"{key}.json"
        if cached.exists():
            return json.loads(cached.read_text())
        for attempt in range(6):
            if search:                                   # 30 searches a minute
                time.sleep(max(0.0, self.last_search + 2.1 - time.monotonic()))
                self.last_search = time.monotonic()
            r = self.http.get(path, params=params)
            if r.status_code in (403, 429) and ("rate limit" in r.text.lower() or r.status_code == 429):
                reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
                time.sleep(max(5, reset - time.time() + 2) if attempt < 5 else 5)
                continue
            if r.status_code in (404, 409, 410, 422, 451):
                body = None
            else:
                r.raise_for_status()
                body = r.json()
            CACHE.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(body))
            return body
        raise RuntimeError(f"gave up on {path} after repeated rate limits")

    def search_all(self, kind: str, q: str) -> list[dict] | None:
        items: list[dict] = []
        for page in range(1, 11):                        # search returns at most 1,000
            body = self.get(f"/search/{kind}", {"q": q, "per_page": 100, "page": page}, search=True)
            if body is None:
                return None if page == 1 else items
            items += body.get("items", [])
            if len(items) >= body.get("total_count", 0) or not body.get("items"):
                break
        return items


def _strip_ref(title: str) -> str:
    return _PR_REF.sub("", title).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-merged", type=int, default=20)
    args = ap.parse_args()

    pr = pd.read_parquet(DATA / "pull_request.parquet", columns=["id", "number", "title", "repo_url", "merged_at"])
    merged = pr[pr.merged_at.notna()].copy()
    merged["repo"] = merged.repo_url.str.replace(f"{API}/repos/", "", regex=False)
    counts = merged.groupby("repo").size()
    repos = sorted(counts[counts >= args.min_merged].index)
    merged = merged[merged.repo.isin(repos)]
    commits = pd.read_parquet(DATA / "pr_commits.parquet", columns=["sha", "pr_id"])
    by_number = {(r.repo, int(r.number)): r for r in merged.itertuples()}
    sha_to_pr = dict(zip(commits.sha, commits.pr_id, strict=True))
    id_to_key = {int(r.id): (r.repo, int(r.number)) for r in merged.itertuples()}
    own_commits: dict[tuple[str, int], set[str]] = {}
    for sha, pid in zip(commits.sha, commits.pr_id, strict=True):
        if pid in id_to_key:
            own_commits.setdefault(id_to_key[pid], set()).add(sha[:10])
    print(f"  {len(repos)} repos, {len(merged):,} merged agent PRs", flush=True)

    gh = GitHub()
    found: dict[str, dict] = {}                          # "repo#N" -> earliest revert
    unreachable: list[str] = []

    def record(repo: str, number: int, at: str, via: str, evidence: str) -> None:
        if (repo, number) not in by_number or not at:
            return                                       # not a merged agent PR
        when = pd.Timestamp(at).tz_convert("UTC")                      # commit dates carry offsets
        # An agent undoing its own step inside the PR lands on main with the
        # merge (a rebase merge stamps it with the merge time); that isn't a
        # revert of the merged change, so the revert must come clearly after.
        if when <= pd.Timestamp(by_number[(repo, number)].merged_at) + pd.Timedelta(minutes=1):
            return
        if evidence in own_commits.get((repo, number), ()):
            return
        at = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        k = f"{repo}#{number}"
        if k not in found or at < found[k]["reverted_at"]:
            found[k] = {"repo": repo, "number": number, "reverted_at": at, "via": via, "evidence": evidence}

    for i, repo in enumerate(repos, 1):
        titles: dict[str, list[int]] = {}
        here = merged[merged.repo == repo]
        # A revert can only count if it lands after an agent merge and within
        # the 30-day outcome window of one; others aren't worth a lookup.
        since = pd.Timestamp(here.merged_at.min())
        until = pd.Timestamp(here.merged_at.max()) + pd.Timedelta(days=31)
        for r in here.itertuples():
            titles.setdefault(_strip_ref(r.title or ""), []).append(int(r.number))

        def by_title(title: str, _titles: dict[str, list[int]] = titles) -> int | None:
            hits = _titles.get(_strip_ref(title), [])
            return hits[0] if len(hits) == 1 else None

        # A repo renamed since collection answers search only under its new name.
        info = gh.get(f"/repos/{repo}")
        if not info:
            unreachable.append(repo)
            continue
        name = info["full_name"]
        prs = gh.search_all("issues", f'repo:{name} is:pr is:merged in:title "Revert"') or []
        for item in prs:
            at = (item.get("pull_request") or {}).get("merged_at") or item.get("closed_at")
            body, title = item.get("body") or "", item.get("title") or ""
            ref = _REVERTS_REF.search(body)
            m = _REVERTED_TITLE.match(title)
            if ref and ref.group(1).lower() in (repo.lower(), name.lower()):
                record(repo, int(ref.group(2)), at, "reverts-ref", f"PR #{item['number']}")
            elif m and _PR_REF.search(m.group(1)):
                record(repo, int(_PR_REF.search(m.group(1)).group(1)), at, "title-ref", f"PR #{item['number']}")
            elif m and (n := by_title(m.group(1))):
                record(repo, n, at, "title-match", f"PR #{item['number']}")

        for item in gh.search_all("commits", f'repo:{name} "This reverts commit"') or []:
            msg = item["commit"]["message"]
            at = item["commit"]["committer"]["date"]
            if not since <= pd.Timestamp(at).tz_convert("UTC") <= until:
                continue
            first = msg.splitlines()[0] if msg else ""
            m = _REVERTED_TITLE.match(first)
            for sha in _THIS_REVERTS.findall(msg):
                if m and _PR_REF.search(m.group(1)):
                    record(repo, int(_PR_REF.search(m.group(1)).group(1)), at, "title-ref", item["sha"][:10])
                elif (pid := sha_to_pr.get(sha)) and pid in id_to_key and id_to_key[pid][0] == repo:
                    record(repo, id_to_key[pid][1], at, "pr-commit-sha", item["sha"][:10])
                elif m and (n := by_title(m.group(1))):
                    record(repo, n, at, "title-match", item["sha"][:10])
                elif len(sha) == 40:
                    pulls = gh.get(f"/repos/{name}/commits/{sha}/pulls") or []
                    merged_pulls = [p for p in pulls if p.get("merged_at")]
                    if len(merged_pulls) == 1:
                        record(repo, merged_pulls[0]["number"], at, "commit-lookup", item["sha"][:10])
        if i % 20 == 0 or i == len(repos):
            print(f"  {i}/{len(repos)} repos, {len(found)} reverted agent PRs so far", flush=True)

    how = dict(Counter(r["via"] for r in found.values()))
    out = {"repos": repos, "unreachable": unreachable, "min_merged": args.min_merged,
           "matched_by": how, "reverts": sorted(found.values(), key=lambda r: (r["repo"], r["number"]))}
    (DATA / "reverts.json").write_text(json.dumps(out, indent=1))
    print(f"\n  {len(found)} reverted agent PRs in {len(repos) - len(unreachable)} repos "
          f"({len(unreachable)} unreachable); matched by {how}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
