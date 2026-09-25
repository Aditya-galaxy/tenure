"""The verification agent, end to end against a real git repository.

The sandbox here is LocalSandbox (commands on this machine, for a fixture we
wrote) and the model is scripted, so these tests exercise everything except
Nebius itself: planning from the base branch only, repair after a failed
step, the three branched runs, and the fixed rules that judge them.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile

import pytest

from tenure import config
from tenure.agent import Recipe, VerificationAgent, count_tests
from tenure.model import ModelError, json_object
from tenure.sandbox import LocalSandbox
from tests.test_github_app import EVAL, FakeInstallation, Verdict, engine, grant, parts  # noqa: F401

LIB = "def add(a, b):\n    return a + b\n"
TESTS = ("from lib import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
         "def test_zero():\n    assert add(0, 0) == 0\n")
MARKER = "PR_ONLY_TEXT_7f3a"      # appears only in PRs; must never reach the planner

RECIPE = {"image": "python:3.12-slim", "setup": ["true"],
          "test": f"{sys.executable} -m pytest -p no:cacheprovider", "framework": "pytest"}


class ScriptedModel:
    def __init__(self, *answers: dict | str) -> None:
        self.answers = [a if isinstance(a, str) else json.dumps(a) for a in answers]
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        self.prompts.append(system + "\n" + user)
        if not self.answers:
            raise ModelError("no more scripted answers")
        return self.answers.pop(0)


class GitSource:
    """A repository on disk, answering like github.Installation."""

    def __init__(self, tmp_path, files: dict[str, str]) -> None:
        self.dir = tmp_path / "origin"
        self.dir.mkdir()
        self._git("init", "-q")
        self.base = self.commit(files, "base")

    def _git(self, *args: str) -> str:
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        return subprocess.run(["git", *args], cwd=self.dir, env=env, capture_output=True, text=True,
                              check=True).stdout

    def commit(self, files: dict[str, str | None], message: str, parent: str | None = None) -> str:
        if parent:
            self._git("checkout", "-q", "--detach", parent)
        for path, text in files.items():
            target = self.dir / path
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        self._git("add", "-A")
        self._git("commit", "-qm", message)
        return self._git("rev-parse", "HEAD").strip()

    def tree(self, repo, sha):
        return self._git("ls-tree", "-r", "--name-only", sha).split()

    def file_text(self, repo, path, ref):
        try:
            return self._git("show", f"{ref}:{path}")
        except subprocess.CalledProcessError:
            return None

    def tarball(self, repo, sha):
        raw = subprocess.run(["git", "archive", "--format=tar", f"--prefix=repo-{sha[:7]}/", sha], cwd=self.dir,
                             capture_output=True, check=True).stdout
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as gz, tarfile.open(fileobj=io.BytesIO(raw)) as t:
            for member in t.getmembers():
                gz.addfile(member, t.extractfile(member) if member.isfile() else None)
        return out.getvalue()

    def compare_diff(self, repo, base_sha, head_sha):
        return self._git("diff", "--binary", f"{base_sha}...{head_sha}")


@pytest.fixture
def repo(tmp_path):
    return GitSource(tmp_path, {"lib.py": LIB, "test_lib.py": TESTS, "pyproject.toml": "[project]\nname='x'\n"})


def verify(agent, repo, head):
    return agent.verify("o/r", 7, head, repo.base, config.RepoPolicy(), source=repo)


def test_a_good_change_verifies_and_reverts_to_the_baseline(tmp_path, repo):
    head = repo.commit({"lib.py": LIB + f"\n\ndef sub(a, b):  # {MARKER}\n    return a - b\n",
                        "test_sub.py": "from lib import sub\n\n\ndef test_sub():\n    assert sub(3, 2) == 1\n"},
                       "add sub", parent=repo.base)
    model = ScriptedModel(RECIPE)
    v = verify(VerificationAgent(LocalSandbox(tmp_path / "sb"), model), repo, head)
    assert v.problem is None, v.brief
    assert (v.tests_passed, v.tests_before, v.tests_after, v.reversible) == (True, 2, 3, True)
    assert v.head_sha == head
    assert "base branch: tests pass, 2 tests" in v.brief and "matches the base branch" in v.brief
    assert all(MARKER not in p for p in model.prompts)          # the planner saw the base branch only


def test_a_breaking_change_fails_with_the_output_in_the_brief(tmp_path, repo):
    head = repo.commit({"lib.py": "def add(a, b):\n    return a - b\n"}, "oops", parent=repo.base)
    v = verify(VerificationAgent(LocalSandbox(tmp_path / "sb"), ScriptedModel(RECIPE)), repo, head)
    assert not v.tests_passed and v.problem is None
    assert "tests FAIL" in v.brief and "assert" in v.brief


def test_deleting_a_test_is_seen_as_a_lower_count(tmp_path, repo):
    head = repo.commit({"test_lib.py": "from lib import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
                       "trim", parent=repo.base)
    v = verify(VerificationAgent(LocalSandbox(tmp_path / "sb"), ScriptedModel(RECIPE)), repo, head)
    assert v.tests_passed and (v.tests_before, v.tests_after) == (2, 1)


def test_a_failed_setup_step_is_repaired_from_base_branch_errors_only(tmp_path, repo):
    head = repo.commit({"README.md": MARKER}, "docs", parent=repo.base)
    broken = {**RECIPE, "setup": ["echo missing-toolchain >&2; exit 3"]}
    model = ScriptedModel(broken, RECIPE)
    v = verify(VerificationAgent(LocalSandbox(tmp_path / "sb"), model), repo, head)
    assert v.problem is None and v.tests_passed
    assert len(model.prompts) == 2 and "missing-toolchain" in model.prompts[1]
    assert all(MARKER not in p for p in model.prompts)


def test_giving_up_after_the_repair_budget_holds_with_a_reason(tmp_path, repo):
    head = repo.commit({"README.md": "x"}, "docs", parent=repo.base)
    broken = {**RECIPE, "setup": ["exit 1"]}
    v = verify(VerificationAgent(LocalSandbox(tmp_path / "sb"), ScriptedModel(broken, broken), repairs=1),
               repo, head)
    assert v.problem and "after 2 attempts" in v.problem and not v.tests_passed


def test_a_recipe_outside_the_rules_is_refused(tmp_path, repo):
    head = repo.commit({"README.md": "x"}, "docs", parent=repo.base)
    model = ScriptedModel({**RECIPE, "image": "attacker/anything:latest"})
    v = verify(VerificationAgent(LocalSandbox(tmp_path / "sb"), model), repo, head)
    assert v.problem and "official" in v.problem


def test_a_pr_that_does_not_apply_to_the_base_holds(tmp_path, repo):
    elsewhere = repo.commit({"lib.py": "def add(a, b):\n    return b + a\n"}, "moved on", parent=repo.base)
    head = repo.commit({"lib.py": "def add(x, y):\n    return b + a + 0\n"}, "pr", parent=elsewhere)
    model = ScriptedModel(RECIPE)
    agent = VerificationAgent(LocalSandbox(tmp_path / "sb"), model)
    # Verify against the old base, whose lib.py the diff no longer fits.
    source = repo
    source.compare_diff = lambda r, b, h: repo._git("diff", elsewhere, head)  # a diff made against another base
    v = agent.verify("o/r", 7, head, repo.base, config.RepoPolicy(), source=source)
    assert v.problem and "doesn't apply cleanly" in v.problem


def test_a_base_commit_is_built_once(tmp_path, repo):
    a = repo.commit({"README.md": "a"}, "a", parent=repo.base)
    b = repo.commit({"NOTES.md": "b"}, "b", parent=repo.base)
    model = ScriptedModel(RECIPE)
    agent = VerificationAgent(LocalSandbox(tmp_path / "sb"), model)
    assert verify(agent, repo, a).tests_passed and verify(agent, repo, b).tests_passed
    assert len(model.prompts) == 1


def test_counting_reads_each_frameworks_own_summary():
    assert count_tests("pytest", "..\n===== 3 passed, 1 failed, 2 skipped in 0.12s =====") == 6
    assert count_tests("pytest", "no summary here") is None
    assert count_tests("cargo", "test result: ok. 4 passed; 0 failed; 1 ignored; 0 measured\n"
                                "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured") == 7
    assert count_tests("jest", "Tests:       1 failed, 12 passed, 13 total") == 13
    assert count_tests("vitest", " Tests  9 passed (9)") == 9
    assert count_tests("go", "=== RUN   TestA\n--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.00s)") == 2
    assert count_tests("unittest", "......\nRan 6 tests in 0.004s\n\nOK") == 6
    assert count_tests("rspec", "12 examples, 0 failures") == 12
    assert count_tests("mocha", "  5 passing (20ms)\n  1 failing") == 6


def test_recipe_parsing_is_strict_and_tolerates_reasoning_text():
    r = Recipe.parse("<think>cargo, so rust</think>\n```json\n" + json.dumps(RECIPE) + "\n```")
    assert r.framework == "pytest"
    with pytest.raises(ModelError):
        Recipe.parse(json.dumps({**RECIPE, "setup": ["x"] * 13}))
    with pytest.raises(ModelError):
        Recipe.parse(json.dumps({**RECIPE, "framework": "whatever"}))
    with pytest.raises(ModelError):
        json_object("no json at all")


class Crashes:
    def verify(self, *a, **k):
        raise RuntimeError("sandbox API down")


class Counts:
    def __init__(self):
        self.calls = 0

    def verify(self, *a, **k):
        self.calls += 1


def test_the_engine_verifies_only_when_a_grant_could_apply(parts):  # noqa: F811
    counting = Counts()
    engine(parts, FakeInstallation(), counting).evaluate(EVAL)
    assert counting.calls == 0                                   # no grant: held anyway, no sandbox spend
    grant(parts)
    engine(parts, FakeInstallation(), counting).evaluate(EVAL)
    assert counting.calls == 1


def test_a_crashing_verifier_holds_and_says_why(parts):  # noqa: F811
    grant(parts)
    inst = FakeInstallation()
    d = engine(parts, inst, Crashes()).evaluate(EVAL)
    assert d.verdict is Verdict.HOLD and inst.merges == []
    assert any("verifier failed (RuntimeError)" in r for r in d.reasons)


def test_without_nebius_credentials_nothing_is_verified(monkeypatch):
    from tenure.app import _verifier
    from tenure.verify import Unverified
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "p")
    assert isinstance(_verifier(), Unverified)
