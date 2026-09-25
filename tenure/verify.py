"""
Verification: running the base branch's tests against the PR, and against its
revert, in an isolated sandbox.

The verifier is the verification agent (agent.py), which plans with Nemotron
and runs in Nebius Token Factory Sandboxes. Until one is configured,
`Unverified` answers for every PR, and policy.decide holds all of them — "not
verified in a sandbox". That is the intended failure: a missing verifier must
never read as a passing one.
"""

from __future__ import annotations

from typing import Any, Protocol

from .config import RepoPolicy
from .policy import Verification


class Verifier(Protocol):
    def verify(self, repo: str, number: int, head_sha: str, base_sha: str,
               policy: RepoPolicy, *, source: Any) -> Verification | None: ...


class Unverified:
    def verify(self, repo: str, number: int, head_sha: str, base_sha: str,
               policy: RepoPolicy, *, source: Any) -> Verification | None:
        return None
