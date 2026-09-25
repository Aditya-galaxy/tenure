"""
Every merge Tenure made without a person, and the grant that allowed it.

Evidence arrives later and indirectly — a revert PR naming #123, a red build on
a commit hash — and has to be traced back to the grant that let #123 merge.
This ledger is that trace. It is append-only; every entry also goes into the
audit chain.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .audit import AuditLog


@dataclass(frozen=True)
class Merge:
    repo: str
    pr: int
    title: str
    head_sha: str           # the commit that was verified
    merge_sha: str          # the commit the merge created on the base branch
    agent: str
    change_class: str
    grant_scope: tuple[str, str, str]
    at: str


class Ledger:
    def __init__(self, path: str | Path, audit: AuditLog) -> None:
        self.path = Path(path)
        self.audit = audit
        self._lock = threading.Lock()

    def record(self, merge: Merge) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(merge)) + "\n")
            self.audit.record("merged", asdict(merge))

    def all(self) -> list[Merge]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                d["grant_scope"] = tuple(d["grant_scope"])
                out.append(Merge(**d))
        return out

    def by_pr(self, repo: str, pr: int) -> Merge | None:
        return next((m for m in self.all() if m.repo == repo and m.pr == pr), None)

    def by_merge_sha(self, repo: str, sha: str) -> Merge | None:
        return next((m for m in self.all() if m.repo == repo and m.merge_sha == sha), None)

    def by_title(self, repo: str, title: str) -> list[Merge]:
        return [m for m in self.all() if m.repo == repo and m.title == title]
