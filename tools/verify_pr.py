"""
Run the verification agent on one real pull request, outside the GitHub App.
This is the spike for Token Factory: Nemotron plans and Sandboxes run.

    export NEBIUS_API_KEY=... NEBIUS_PROJECT_ID=...
    GH_TOKEN=$(gh auth token) python tools/verify_pr.py Aditya-galaxy/hyperion-quant 12

It reads the repository with your GitHub token (read-only calls), prints the
recipe the model chose, every sandbox step, and the Verification that policy.decide
would receive. It merges nothing and writes nothing to GitHub.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tenure.agent import VerificationAgent
from tenure.config import RepoPolicy
from tenure.model import TokenFactory
from tenure.sandbox import ContreeSandbox, RemoteSandbox, Step


class ReadOnlyGitHub:
    def __init__(self, token: str) -> None:
        self.http = httpx.Client(base_url="https://api.github.com", timeout=60, follow_redirects=True,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def get(self, path: str, **kw) -> httpx.Response:
        r = self.http.get(path, **kw)
        r.raise_for_status()
        return r

    def tree(self, repo: str, sha: str) -> list[str]:
        tree = self.get(f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"}).json()["tree"]
        return [e["path"] for e in tree if e["type"] == "blob"]

    def file_text(self, repo: str, path: str, ref: str) -> str | None:
        r = self.http.get(f"/repos/{repo}/contents/{path}", params={"ref": ref},
                          headers={"Accept": "application/vnd.github.raw"})
        return r.text if r.status_code == 200 else None

    def tarball(self, repo: str, sha: str) -> bytes:
        return self.get(f"/repos/{repo}/tarball/{sha}").content

    def compare_diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        return self.get(f"/repos/{repo}/compare/{base_sha}...{head_sha}",
                        headers={"Accept": "application/vnd.github.diff"}).text


class Logged:
    """Prints every sandbox step as it happens."""

    def __init__(self, inner: ContreeSandbox) -> None:
        self.inner = inner

    def image(self, name: str) -> str:
        print(f"  image {name}", flush=True)
        return self.inner.image(name)

    def run(self, state: str, shell: str, **kw) -> Step:
        start = time.monotonic()
        step = self.inner.run(state, shell, **kw)
        print(f"  [{time.monotonic() - start:6.1f}s] exit {step.exit_code}  {shell[:100]}", flush=True)
        return step


def main() -> int:
    repo, number = sys.argv[1], int(sys.argv[2])
    gh = ReadOnlyGitHub(os.environ["GH_TOKEN"])
    pr = gh.get(f"/repos/{repo}/pulls/{number}").json()
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    print(f"{repo}#{number} {pr['title']!r}\n  base {base[:7]}  head {head[:7]}")
    runner = os.environ.get("TENURE_RUNNER_URL")
    sandbox = RemoteSandbox(runner) if runner else ContreeSandbox()
    agent = VerificationAgent(Logged(sandbox), TokenFactory.from_env())
    start = time.monotonic()
    v = agent.verify(repo, number, head, base, RepoPolicy(), source=gh)
    built = agent.built.get((repo, base))
    if built:
        print(f"\nrecipe: {built.recipe}\n" + "\n".join(built.log))
    print(f"\n{v}\n\n{v.brief}\n\ntotal {time.monotonic() - start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
