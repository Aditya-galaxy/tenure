"""
The GitHub App: authenticate as it, then act in one installation.

Only what Tenure needs, and each call written so that a surprise holds a PR
rather than merges it:

  * Permissions come from `role_name`, the one field that tells maintain from
    write. Anything unrecognised reads as "none".
  * Every merge passes `sha`: GitHub refuses it if the head moved after
    verification, and that refusal is reported as a hold, never retried
    blindly.
  * A missing `.tenure.yml` is "no policy file", which means the defaults — it
    is not an error. Any other failure to read it is.
"""

from __future__ import annotations

import base64
import calendar
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from .classify import ChangedFile

API = "https://api.github.com"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tenure-app"}
_ROLES = frozenset({"admin", "maintain", "write", "triage", "read"})


class GitHubError(RuntimeError):
    pass


class MergeRefused(RuntimeError):
    """GitHub declined the merge — usually the head moved, or checks are required."""


@dataclass
class GitHubApp:
    app_id: str
    private_key: str
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=20.0, headers=_HEADERS))
    base: str = API
    _tokens: dict[int, tuple[str, float]] = field(default_factory=dict)

    def app_jwt(self, now: float | None = None) -> str:
        """Ten minutes is GitHub's ceiling; issue for nine, backdated a minute for clock drift."""
        now = time.time() if now is None else now
        return jwt.encode({"iat": int(now) - 60, "exp": int(now) + 540, "iss": str(self.app_id)},
                          self.private_key, algorithm="RS256")

    def installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - time.time() > 60:
            return cached[0]
        r = self.http.post(f"{self.base}/app/installations/{installation_id}/access_tokens",
                           headers={"Authorization": f"Bearer {self.app_jwt()}"})
        if r.status_code != 201:
            raise GitHubError(f"couldn't get an installation token ({r.status_code})")
        data = r.json()
        expires = calendar.timegm(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        self._tokens[installation_id] = (data["token"], expires)
        return data["token"]

    def installation(self, installation_id: int) -> Installation:
        return Installation(self, installation_id)


@dataclass
class Installation:
    app: GitHubApp
    installation_id: int

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        token = self.app.installation_token(self.installation_id)
        return self.app.http.request(method, f"{self.app.base}{path}",
                                     headers={"Authorization": f"Bearer {token}"}, **kw)

    def _json(self, method: str, path: str, ok: tuple[int, ...] = (200,), **kw: Any) -> Any:
        r = self._req(method, path, **kw)
        if r.status_code not in ok:
            raise GitHubError(f"{method} {path} answered {r.status_code}")
        return r.json()

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        return self._json("GET", f"/repos/{repo}/pulls/{number}")

    def pull_files(self, repo: str, number: int) -> list[ChangedFile]:
        out: list[ChangedFile] = []
        page = 1
        while True:
            batch = self._json("GET", f"/repos/{repo}/pulls/{number}/files",
                               params={"per_page": 100, "page": page})
            for f in batch:
                out.append(ChangedFile(path=f["filename"], status=f["status"], additions=f.get("additions", 0),
                                       deletions=f.get("deletions", 0), patch=f.get("patch"),
                                       previous_path=f.get("previous_filename")))
            if len(batch) < 100:
                return out
            page += 1

    def file_text(self, repo: str, path: str, ref: str) -> str | None:
        r = self._req("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GitHubError(f"couldn't read {path} ({r.status_code})")
        data = r.json()
        if data.get("encoding") != "base64":
            raise GitHubError(f"{path} isn't a file")
        return base64.b64decode(data["content"]).decode("utf-8")

    def role(self, repo: str, login: str) -> str:
        r = self._req("GET", f"/repos/{repo}/collaborators/{login}/permission")
        if r.status_code == 404:
            return "none"
        if r.status_code != 200:
            raise GitHubError(f"couldn't read @{login}'s permission ({r.status_code})")
        role = (r.json().get("role_name") or "").lower()
        return role if role in _ROLES else "none"

    def check_run(self, repo: str, head_sha: str, *, conclusion: str, title: str, summary: str) -> int:
        data = self._json("POST", f"/repos/{repo}/check-runs", ok=(201,), json={
            "name": "Tenure", "head_sha": head_sha, "status": "completed", "conclusion": conclusion,
            "output": {"title": title[:250], "summary": summary[:60000]},
        })
        return int(data["id"])

    def merge(self, repo: str, number: int, *, sha: str, title: str) -> str:
        r = self._req("PUT", f"/repos/{repo}/pulls/{number}/merge",
                      json={"sha": sha, "merge_method": "merge", "commit_title": title[:250]})
        if r.status_code == 200 and r.json().get("merged"):
            return r.json()["sha"]
        raise MergeRefused(f"GitHub declined the merge ({r.status_code}): "
                           f"{(r.json() if r.content else {}).get('message', '')}".strip())

    def comment(self, repo: str, number: int, body: str) -> None:
        self._json("POST", f"/repos/{repo}/issues/{number}/comments", ok=(201,), json={"body": body})
