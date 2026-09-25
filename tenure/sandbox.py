"""
Sandboxes: somewhere to build and test a PR that isn't Tenure's own machine.

A sandbox state is an opaque id for a filesystem snapshot. Running a command
from a state returns a new state and leaves the old one as it was, so a single
checkpoint (the base branch built, dependencies installed) can be branched into
the PR run and the revert run without rebuilding. That is how Nebius Token
Factory Sandboxes (ConTree) work, and `ContreeSandbox` wraps them.

Nothing Tenure holds goes into a sandbox: no GitHub token, no API key, no
environment. The code under test is untrusted and is treated that way.

`LocalSandbox` runs commands on this machine, in temporary directories, with
no isolation at all. It exists so the verification agent can be tested
end to end against a known-good fixture repo. It is never selected by
configuration and must never be pointed at a real pull request.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

WORKDIR = "/work"
INBOX = "/tmp/tenure"  # noqa: S108 -- a path inside the sandbox, where Tenure puts what it uploads


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

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or tempfile.mkdtemp(prefix="tenure-local-sandbox-"))
        self.states: dict[str, Path] = {}

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
        for prefix, local in mapped.items():
            shell = shell.replace(prefix, str(local))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(new / "home")}
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
