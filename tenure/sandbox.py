"""
Sandboxes: somewhere to build and test a PR that isn't Tenure's own machine.

A sandbox state is an opaque id for a filesystem snapshot. Running a command
from a state returns a new state and leaves the old one as it was, so a single
checkpoint (the base branch built, dependencies installed) can be branched into
the PR run and the revert run without rebuilding. That is how Nebius Token
Factory Sandboxes (ConTree) work, and `ContreeSandbox` wraps them.

Nothing Tenure holds goes into a sandbox: no GitHub token, no API key, no
environment. The code under test is untrusted and is treated that way.

`LocalSandbox` runs commands on the machine it is on, in temporary
directories, with no isolation of its own. Tenure itself never runs it on a
real pull request: the tests use it against a fixture repo, and the runner
service (tenure/runner.py) uses it inside a container of its own that holds
no credentials. `RemoteSandbox` is how Tenure talks to that runner.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

WORKDIR = "/work"
INBOX = "/tmp/tenure"  # noqa: S108 -- a path inside the sandbox, where Tenure puts what it uploads


_PATHS = re.compile(rf"(?<![\w/.-])({re.escape(INBOX)}|{re.escape(WORKDIR)})(?![\w.-])")


@dataclass(frozen=True)
class Step:
    state: str               # the snapshot after the command ran
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def tail(self, chars: int = 4000) -> str:
        return (self.stdout + "\n" + self.stderr).strip()[-chars:]


class Sandbox(Protocol):
    def image(self, name: str) -> str:
        """The state for a public container image, e.g. "python:3.12-slim"."""
        ...

    def run(self, state: str, shell: str, *, files: dict[str, bytes] | None = None, timeout: int = 600) -> Step:
        """Run `shell` in WORKDIR on top of `state`, after writing `files`
        (paths under WORKDIR or INBOX → content). `state` is left unchanged."""
        ...


class LocalSandbox:
    """Tests only: each state is a directory copy, and commands run on the host."""

    def __init__(self, root: Path | None = None, env: dict[str, str] | None = None) -> None:
        self.root = Path(root or tempfile.mkdtemp(prefix="tenure-local-sandbox-"))
        self.states: dict[str, Path] = {}
        self.env = dict(env or {})            # e.g. where an installed toolchain lives (RUSTUP_HOME)

    def image(self, name: str) -> str:
        state = f"image:{name}"
        if state not in self.states:
            path = self.root / uuid.uuid4().hex
            for sub in ("work", "inbox", "home"):
                (path / sub).mkdir(parents=True)
            self.states[state] = path
        return state

    def run(self, state: str, shell: str, *, files: dict[str, bytes] | None = None, timeout: int = 600) -> Step:
        new = self.root / uuid.uuid4().hex
        shutil.copytree(self.states[state], new, symlinks=True)
        mapped = {WORKDIR: new / "work", INBOX: new / "inbox"}
        for path, content in (files or {}).items():
            prefix = next((p for p in mapped if path.startswith(p + "/")), None)
            if prefix is None:
                raise ValueError(f"LocalSandbox only writes under {WORKDIR} or {INBOX}: {path}")
            target = mapped[prefix] / path[len(prefix) + 1:]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        # One pass, whole paths only: rewriting one prefix and then the next
        # would match inside the first rewrite (on Linux this sandbox's own
        # directory is /tmp/tenure-local-sandbox-..., which starts with INBOX).
        shell = _PATHS.sub(lambda m: str(mapped[m.group(1)]), shell)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **self.env, "HOME": str(new / "home")}
        try:
            p = subprocess.run(["/bin/bash", "-c", shell], cwd=new / "work", env=env,  # noqa: S603
                               capture_output=True, text=True, timeout=timeout, check=False)
            code, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            code, out, err = 124, "", f"timed out after {timeout}s"
        name = f"local:{new.name}"
        self.states[name] = new
        return Step(name, code, out, err)


class ContreeSandbox:
    """Nebius Token Factory Sandboxes through the ConTree SDK.

    Credentials come from the environment the SDK reads (NEBIUS_API_KEY and
    NEBIUS_PROJECT_ID) and stay in this process; nothing is passed into the
    sandbox. Every run is non-disposable, so its result is a new snapshot that
    later runs can branch from.
    """

    def __init__(self) -> None:
        from contree_sdk import ContreeSync  # optional dependency: pip install tenure[sandbox]
        self.client = ContreeSync()
        self.handles: dict[str, object] = {}

    def image(self, name: str) -> str:
        handle = self.client.images.oci(f"docker://docker.io/library/{name}")
        state = f"image:{name}"
        self.handles[state] = handle
        return state

    def run(self, state: str, shell: str, *, files: dict[str, bytes] | None = None, timeout: int = 600) -> Step:
        from contree_sdk.utils.models.file import UploadFileSpec
        with tempfile.TemporaryDirectory(prefix="tenure-upload-") as tmp:
            specs = []
            for i, (path, content) in enumerate((files or {}).items()):
                local = Path(tmp) / str(i)
                local.write_bytes(content)
                specs.append(UploadFileSpec(source=str(local), path=path))
            result = self.handles[state].run(  # noqa: S604 -- runs in the remote sandbox, not here
                shell=f"mkdir -p {WORKDIR} {INBOX} && cd {WORKDIR} && {shell}",
                files=specs, timeout=timeout, disposable=False).wait()
        name = f"contree:{uuid.uuid4().hex}"
        self.handles[name] = result
        return Step(name, result.exit_code, _text(result.stdout), _text(result.stderr))


def _text(stream: object) -> str:
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    if hasattr(stream, "read"):
        data = stream.read()
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return str(stream or "")


class RemoteSandbox:
    """The runner service (tenure/runner.py) over HTTPS.

    The runner is a separate service with no credentials of its own, so the
    PR's code never runs where Tenure's keys are. Each request carries an
    identity token for the runner's address, which only Tenure's own service
    account may call; the token comes from the platform's metadata server, or
    from TENURE_RUNNER_TOKEN when run by hand.
    """

    METADATA = ("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
                "?audience={audience}")

    def __init__(self, url: str, client: object | None = None) -> None:
        import httpx
        self.url = url.rstrip("/")
        self.http = client or httpx.Client(timeout=3700)

    def _token(self) -> str:
        if token := os.environ.get("TENURE_RUNNER_TOKEN"):
            return token
        r = self.http.get(self.METADATA.format(audience=self.url), headers={"Metadata-Flavor": "Google"}, timeout=10)
        r.raise_for_status()
        return r.text

    def _post(self, path: str, body: dict) -> dict:
        r = self.http.post(f"{self.url}{path}", json=body, headers={"Authorization": f"Bearer {self._token()}"})
        if r.status_code != 200:
            raise RuntimeError(f"the runner answered {r.status_code}: {r.text[:300]}")
        return r.json()

    def image(self, name: str) -> str:
        return self._post("/image", {"name": name})["state"]

    def run(self, state: str, shell: str, *, files: dict[str, bytes] | None = None, timeout: int = 600) -> Step:
        import base64
        d = self._post("/run", {"state": state, "shell": shell, "timeout": timeout,
                                "files": {k: base64.b64encode(v).decode() for k, v in (files or {}).items()}})
        return Step(d["state"], d["exit_code"], d["stdout"], d["stderr"])
