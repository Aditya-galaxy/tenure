"""
GitHub webhooks: prove they came from GitHub, handle each once, and turn them
into the few actions Tenure takes.

`actions()` is a pure function from (event name, payload) to actions. The app
does the I/O. Which events matter:

  * pull_request opened / synchronize / reopened / ready_for_review → evaluate
    the PR (again: a new push means a new commit to verify).
  * pull_request closed and merged → check whether it reverts one of Tenure's
    merges.
  * issue_comment created → look for /tenure commands. Only comments: a PR's
    title and body are never read as commands.
  * workflow_run / check_suite completed → check whether a build on the
    default branch failed at a commit Tenure's merge created.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

_EVALUATE_ON = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


def signature_ok(secret: bytes, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256, compared in constant time. No header, no trust."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class Deliveries:
    """GitHub redelivers on timeouts and on request. A delivery ID seen before
    is acknowledged and dropped, so a redelivery can't post a second check run
    or run a /tenure command twice. Bounded, so it can't grow without limit."""

    def __init__(self, limit: int = 10_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def first_time(self, delivery_id: str | None) -> bool:
        if not delivery_id:
            return True
        with self._lock:
            if delivery_id in self._seen:
                return False
            self._seen[delivery_id] = None
            if len(self._seen) > self._limit:
                self._seen.popitem(last=False)
            return True


@dataclass(frozen=True)
class EvaluatePR:
    repo: str
    number: int
    installation: int
    default_branch: str


@dataclass(frozen=True)
class RunCommands:
    repo: str
    number: int
    installation: int
    sender: str
    sender_type: str
    body: str
    default_branch: str


@dataclass(frozen=True)
class PRMerged:
    repo: str
    number: int
    installation: int
    title: str
    body: str


@dataclass(frozen=True)
class BuildFinished:
    repo: str
    installation: int
    head_sha: str
    head_branch: str | None
    default_branch: str
    conclusion: str | None


Action = EvaluatePR | RunCommands | PRMerged | BuildFinished


def actions(event: str, payload: dict[str, Any]) -> list[Action]:
    repo_info = payload.get("repository") or {}
    repo = repo_info.get("full_name")
    default = repo_info.get("default_branch") or "main"
    inst = (payload.get("installation") or {}).get("id")
    if not repo or inst is None:
        return []
    act = payload.get("action")

    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        if act in _EVALUATE_ON and not pr.get("draft"):
            return [EvaluatePR(repo, int(pr["number"]), int(inst), default)]
        if act == "closed" and pr.get("merged"):
            return [PRMerged(repo, int(pr["number"]), int(inst), pr.get("title") or "", pr.get("body") or "")]
        return []

    if event == "issue_comment" and act == "created":
        comment = payload.get("comment") or {}
        user = comment.get("user") or {}
        issue = payload.get("issue") or {}
        return [RunCommands(repo, int(issue["number"]), int(inst), user.get("login", ""), user.get("type", ""),
                            comment.get("body") or "", default)]

    if event in ("workflow_run", "check_suite") and act == "completed":
        run = payload.get(event) or {}
        return [BuildFinished(repo, int(inst), run.get("head_sha", ""), run.get("head_branch"), default,
                              run.get("conclusion"))]
    return []
