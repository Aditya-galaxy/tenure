"""The runner service, driven the way Tenure drives it: through RemoteSandbox."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tenure.runner import create_app
from tenure.sandbox import INBOX, WORKDIR, LocalSandbox, RemoteSandbox


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setenv("TENURE_RUNNER_TOKEN", "test-token")
    client = TestClient(create_app(LocalSandbox(tmp_path)))
    return RemoteSandbox("http://runner", client=client), client


def test_steps_branch_from_snapshots_through_the_service(remote):
    box, _ = remote
    base = box.image("python:3.12-slim")
    built = box.run(base, f"echo built > {WORKDIR}/marker && cat {INBOX}/note", files={f"{INBOX}/note": b"hi"})
    assert built.ok and built.stdout.strip() == "hi"
    pr = box.run(built.state, "echo pr >> marker && cat marker")
    assert pr.stdout.split() == ["built", "pr"]
    again = box.run(built.state, "cat marker")              # the earlier snapshot is unchanged
    assert again.stdout.split() == ["built"]


def test_a_failing_command_comes_back_as_a_failed_step(remote):
    box, _ = remote
    step = box.run(box.image("python:3"), "exit 3")
    assert step.exit_code == 3 and not step.ok


def test_a_toolchain_it_does_not_have_is_refused(remote):
    box, _ = remote
    with pytest.raises(RuntimeError, match="422"):
        box.image("node:20")


def test_an_unknown_state_is_refused_not_guessed(remote):
    _, client = remote
    r = client.post("/run", json={"state": "local:gone", "shell": "true"})
    assert r.status_code == 410


def test_paths_are_rewritten_once_even_when_the_sandbox_lives_under_tmp_tenure(tmp_path):
    """On Linux the sandbox's own directory is /tmp/tenure-local-sandbox-...,
    which starts with INBOX. Rewriting WORKDIR and then INBOX mangled it."""
    root = tmp_path / "tmp" / "tenure-local-sandbox-x"
    box = LocalSandbox(root)
    base = box.image("python:3")
    step = box.run(base, f"cd {WORKDIR} && pwd && ls {INBOX}/note", files={f"{INBOX}/note": b"x"})
    assert step.ok, step.tail()
    assert step.stdout.splitlines()[0].endswith("/work")
