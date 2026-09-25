"""
The verification agent: builds a repository in a sandbox, runs its tests with
and without the PR, and proves the PR can be reverted.

The work splits into parts a model may do and parts it may not:

  * **The model plans and repairs.** Nemotron reads the *base branch's* file
    list and build files (CI workflows, Cargo.toml, package.json, pyproject…)
    and writes a recipe: a container image, setup commands, a test command, and
    which test framework it is. When a setup step fails on the base branch, the
    model gets the error and revises the recipe, up to a fixed number of
    times. Nothing from the PR ever reaches the planner, so a PR can't shape
    the commands it will be judged by. The recipe and the built checkpoint are
    cached per base commit.
  * **Deterministic code does everything else.** Tenure applies the PR (the
    diff between base and the exact head commit), runs the base's test
    command, then writes the revert with `git revert` and runs it again. It
    reads the results from exit codes and from the test framework's own
    summary line, parsed by fixed rules. No model reads a test result or
    says whether something passed.

Three runs branch from one checkpoint (base built, dependencies installed):
the baseline, the PR, and the PR reverted. A PR verifies when its tests pass,
the test count doesn't drop, and the revert brings the tests back to the
baseline exactly.

What this proves, and what it doesn't: the base branch's test command passes
with the PR applied. The PR's own code runs during those tests and could lie
to them, so verification is one condition of a grant, never a grant. Earned
trust and revocation on revert (policy.py, outcomes.py) cover the rest.

Every verified PR gets a reviewer brief for its check run: what ran, the
three results, and the end of any failing output. It is built from the
results by fixed code, and `decide()` never sees it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Protocol

from . import config
from .globs import any_match
from .model import Model, ModelError, json_object
from .policy import Verification
from .sandbox import INBOX, WORKDIR, Sandbox, Step

MAX_REPAIRS = 3
SETUP_TIMEOUT = 1200
TEST_TIMEOUT = 1800
MAX_DIFF = 5_000_000
MAX_TARBALL = 300_000_000

# Files that tell a planner how a repository builds and tests, read from the base branch.
PLAN_FILES = (".github/workflows/*.yml", ".github/workflows/*.yaml", "Cargo.toml", "package.json",
              "pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt", "tox.ini", "noxfile.py",
              "Makefile", "justfile", "go.mod", "Gemfile", "pom.xml", "build.gradle*", "rust-toolchain*",
              ".python-version", ".nvmrc", ".tool-versions")
PER_FILE = 6_000
PLAN_BUDGET = 40_000

IMAGE = re.compile(r"^(python|node|rust|golang|ruby|openjdk|eclipse-temurin|maven|gradle|ubuntu|debian)"
                   r":[\w.][\w.-]{0,63}$")
FRAMEWORKS = ("pytest", "unittest", "cargo", "jest", "vitest", "mocha", "go", "rspec")

# Tenure's own steps, not the model's.
_PRELUDE = ("command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git >/dev/null) "
            "&& git config --global user.email tenure@localhost && git config --global user.name tenure "
            "&& git config --global init.defaultBranch main")
_UNPACK = (f"mkdir -p {WORKDIR} && tar -xzf {INBOX}/base.tar.gz --strip-components=1 -C {WORKDIR} "
           f"&& rm {INBOX}/base.tar.gz && cd {WORKDIR} && git init -q && git add -A && git commit -qm base")
# A snapshot can change file timestamps, and setup can rewrite tracked files
# (a lockfile, say): settle both so the PR applies to what the tests ran on.
_SETTLE = f"cd {WORKDIR} && git update-index -q --refresh; git commit -qam setup --allow-empty --no-verify"
_APPLY = (f"cd {WORKDIR} && git update-index -q --refresh; "
          f"git apply --index --whitespace=nowarn {INBOX}/pr.diff && git commit -qm pr --no-verify")
_REVERT = f"cd {WORKDIR} && git update-index -q --refresh; git revert --no-edit HEAD"


class Source(Protocol):
    """What the agent reads from GitHub (github.Installation implements it)."""
    def tree(self, repo: str, sha: str) -> list[str]: ...
    def file_text(self, repo: str, path: str, ref: str) -> str | None: ...
    def tarball(self, repo: str, sha: str) -> bytes: ...
    def compare_diff(self, repo: str, base_sha: str, head_sha: str) -> str: ...


class Unverifiable(RuntimeError):
    """The agent couldn't establish a result. The PR is held, with this reason."""


@dataclass(frozen=True)
class Recipe:
    image: str
    setup: tuple[str, ...]
    test: str
    framework: str

    @classmethod
    def parse(cls, answer: str) -> Recipe:
        data = json_object(answer)
        image, setup, test, framework = (data.get(k) for k in ("image", "setup", "test", "framework"))
        if not isinstance(image, str) or not IMAGE.match(image):
            raise ModelError(f"image must be an official language or OS image, got {image!r}")
        if not isinstance(setup, list) or len(setup) > 12 or \
                not all(isinstance(s, str) and 0 < len(s) <= 600 for s in setup):
            raise ModelError("setup must be a list of at most 12 commands")
        if not isinstance(test, str) or not 0 < len(test) <= 600:
            raise ModelError("test must be one command")
        if framework not in FRAMEWORKS:
            raise ModelError(f"framework must be one of {', '.join(FRAMEWORKS)}")
        return cls(image, tuple(setup), test, framework)


@dataclass(frozen=True)
class TestRun:
    passed: bool
    count: int | None
    output: str


@dataclass
class Built:
    """A base commit, built: the recipe that worked and the checkpoint after setup."""
    recipe: Recipe
    checkpoint: str
    baseline: TestRun
    log: list[str] = field(default_factory=list)


# ── counting tests from the framework's own summary ─────────────────────────

def count_tests(framework: str, output: str) -> int | None:
    """The number of tests a run reported, from the last summary the framework
    printed. None when there's no summary to read, which never counts as a pass."""
    def last(pattern: str) -> re.Match[str] | None:
        found = list(re.finditer(pattern, output, flags=re.M))
        return found[-1] if found else None

    if framework == "pytest":
        m = last(r"^=+ (.*\d+ (?:passed|failed|error|errors|skipped|xfailed|xpassed).*?) in [\d.]+s")
        if not m:
            return None
        return sum(int(n) for n, _ in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)",
                                                m.group(1)))
    if framework == "unittest":
        m = last(r"^Ran (\d+) tests? in")
        return int(m.group(1)) if m else None
    if framework == "cargo":
        runs = re.findall(r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", output, flags=re.M)
        return sum(int(a) + int(b) + int(c) for a, b, c in runs) if runs else None
    if framework == "jest":
        m = last(r"^Tests:.*?(\d+) total")
        return int(m.group(1)) if m else None
    if framework == "vitest":
        m = last(r"^\s*Tests\s+.*?\((\d+)\)")
        return int(m.group(1)) if m else None
    if framework == "mocha":
        counts = [last(rf"^\s*(\d+) {word}") for word in ("passing", "failing", "pending")]
        return sum(int(m.group(1)) for m in counts if m) if counts[0] or counts[1] else None
    if framework == "go":
        n = len(re.findall(r"^\s*--- (?:PASS|FAIL|SKIP): ", output, flags=re.M))
        return n or None
    if framework == "rspec":
        m = last(r"^(\d+) examples?, \d+ failures?")
        return int(m.group(1)) if m else None
    return None


# ── the agent ───────────────────────────────────────────────────────────────

class VerificationAgent:
    def __init__(self, sandbox: Sandbox, model: Model, *, repairs: int = MAX_REPAIRS) -> None:
        self.sandbox, self.model, self.repairs = sandbox, model, repairs
        self.built: dict[tuple[str, str], Built] = {}

    # The Verifier protocol (verify.py).
    def verify(self, repo: str, number: int, head_sha: str, base_sha: str,
               policy: config.RepoPolicy, *, source: Source) -> Verification:
        try:
            built = self.build(repo, base_sha, source)
            return self.check(repo, number, head_sha, base_sha, built, source)
        except (Unverifiable, ModelError) as exc:
            return Verification(head_sha, tests_passed=False, tests_before=0, tests_after=0, reversible=False,
                                problem=str(exc), brief=f"Tenure couldn't verify this PR: {exc}")

    # ── planning and building: base branch only ─────────────────────────────

    def build(self, repo: str, base_sha: str, source: Source) -> Built:
        if (repo, base_sha) in self.built:
            return self.built[(repo, base_sha)]
        context = planning_context(repo, base_sha, source)
        recipe = Recipe.parse(self.model.complete(PLANNER, context))
        tarball = source.tarball(repo, base_sha)
        if len(tarball) > MAX_TARBALL:
            raise Unverifiable("the repository is too large to copy into a sandbox")
        log: list[str] = []

        for attempt in range(self.repairs + 1):
            state = self._fixed(self.sandbox.image(recipe.image), _PRELUDE, "installing git")
            state = self._fixed(state, _UNPACK, "unpacking the base branch", files={f"{INBOX}/base.tar.gz": tarball})
            failed: tuple[str, Step] | None = None
            for command in recipe.setup:
                step = self.sandbox.run(state, command, timeout=SETUP_TIMEOUT)
                log.append(f"$ {command} → exit {step.exit_code}")
                if not step.ok:
                    failed = (command, step)
                    break
                state = step.state
            if failed is None:
                state = self._fixed(state, _SETTLE, "recording the built state")
                baseline = self._test(state, recipe)
                log.append(f"$ {recipe.test} → {'pass' if baseline.passed else 'FAIL'}, {baseline.count} tests")
                if baseline.passed and baseline.count:
                    built = Built(recipe, state, baseline, log)
                    self.built[(repo, base_sha)] = built
                    return built
                why = "no test summary found in the output" if not baseline.count else "tests failed"
                failed = (recipe.test, Step(state, 1, baseline.output, why))
            if attempt == self.repairs:
                raise Unverifiable(f"the base branch didn't build and pass its tests after {attempt + 1} attempts; "
                                   f"last failure: `{failed[0]}`")
            recipe = Recipe.parse(self.model.complete(PLANNER, repair_prompt(context, recipe, *failed)))
        raise AssertionError("unreachable")

    def _fixed(self, state: str, command: str, what: str, files: dict[str, bytes] | None = None) -> str:
        step = self.sandbox.run(state, command, files=files, timeout=SETUP_TIMEOUT)
        if not step.ok:
            raise Unverifiable(f"{what} failed: {step.tail(300)}")
        return step.state

    def _test(self, state: str, recipe: Recipe) -> TestRun:
        step = self.sandbox.run(state, recipe.test, timeout=TEST_TIMEOUT)
        output = step.stdout + "\n" + step.stderr
        return TestRun(step.ok, count_tests(recipe.framework, output), output[-6000:])

    # ── checking the PR: fixed steps, fixed rules ───────────────────────────

    def check(self, repo: str, number: int, head_sha: str, base_sha: str, built: Built,
              source: Source) -> Verification:
        diff = source.compare_diff(repo, base_sha, head_sha)
        if len(diff) > MAX_DIFF:
            raise Unverifiable("the diff is too large to apply in a sandbox")
        applied = self.sandbox.run(built.checkpoint, _APPLY, files={f"{INBOX}/pr.diff": diff.encode()})
        if not applied.ok:
            raise Unverifiable(f"the PR doesn't apply cleanly to the base branch: {applied.tail(300)}")
        reinstall = _touches_dependencies(diff)
        after_pr = self._resetup(applied.state, built.recipe) if reinstall else applied.state
        with_pr = self._test(after_pr, built.recipe)

        reverted = self.sandbox.run(applied.state, _REVERT)
        with_revert = None
        if reverted.ok:
            after_revert = self._resetup(reverted.state, built.recipe) if reinstall else reverted.state
            with_revert = self._test(after_revert, built.recipe)
        reversible = bool(with_revert and with_revert.passed and with_revert.count == built.baseline.count)

        before = built.baseline.count or 0
        passed = with_pr.passed and with_pr.count is not None
        return Verification(head_sha, tests_passed=passed, tests_before=before, tests_after=with_pr.count or 0,
                            reversible=reversible, problem=None,
                            brief=brief(built, with_pr, with_revert, reverted.ok, diff))

    def _resetup(self, state: str, recipe: Recipe) -> str:
        for command in recipe.setup:
            step = self.sandbox.run(state, command, timeout=SETUP_TIMEOUT)
            if not step.ok:
                raise Unverifiable(f"with the PR applied, setup step `{command}` failed: {step.tail(300)}")
            state = step.state
        return state


def _touches_dependencies(diff: str) -> bool:
    paths = re.findall(r"^diff --git a/(\S+) b/(\S+)", diff, flags=re.M)
    watched = config.MANIFESTS + config.LOCKFILES
    return any(any_match(p, watched, sensitive=False) for pair in paths for p in pair)


# ── what the model is told ──────────────────────────────────────────────────

PLANNER = f"""You set up a software repository's test suite in a fresh Linux container, so that an
automated check can run it. You are given the repository's file list and its build and CI files,
from its default branch.

Reply with one JSON object and nothing else:
{{"image": "<official Docker Hub image:tag, one of python, node, rust, golang, ruby, openjdk,
eclipse-temurin, maven, gradle, ubuntu, debian>",
 "setup": ["<shell command>", ...],
 "test": "<one shell command that runs the whole test suite once>",
 "framework": "<one of {', '.join(FRAMEWORKS)}>"}}

The repository is already unpacked in {WORKDIR}, which is the working directory for every command.
Commands run as root, non-interactively, with network access for installing dependencies. There are
no secrets, databases or other services. Pick the image and toolchain version the CI files use.
Setup installs the toolchain's extra tools and the project's dependencies and builds it; at most 12
commands. Choose the test command CI uses, restricted to tests that need no network or services,
and make it print the framework's normal summary (for Go, use `go test -v`; for pytest, don't pass -q
twice or --no-summary). Don't change repository files."""


def planning_context(repo: str, base_sha: str, source: Source) -> str:
    paths = source.tree(repo, base_sha)
    shown = sorted(p for p in paths if p.count("/") <= 1)[:400]
    parts = [f"Repository: {repo}", "Files (top two levels):", "\n".join(shown)]
    budget = PLAN_BUDGET
    for path in sorted(p for p in paths if any(fnmatch(p, g) for g in PLAN_FILES)):
        text = source.file_text(repo, path, base_sha)
        if not text:
            continue
        chunk = f"\n--- {path} ---\n{text[:PER_FILE]}"
        if len(chunk) > budget:
            break
        parts.append(chunk)
        budget -= len(chunk)
    return "\n".join(parts)


def repair_prompt(context: str, recipe: Recipe, command: str, step: Step) -> str:
    return (f"{context}\n\nThis recipe failed:\n{recipe}\n\nThe failing command was `{command}`, "
            f"exit code {step.exit_code}. The end of its output:\n{step.tail(3000)}\n\n"
            "Reply with a corrected recipe, as the same kind of JSON object.")


def brief(built: Built, with_pr: TestRun, with_revert: TestRun | None, revert_applied: bool, diff: str) -> str:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)", diff, flags=re.M)
    before = built.baseline.count
    lines = [f"**Sandbox verification** (`{built.recipe.image}`, `{built.recipe.test}`)",
             f"- base branch: tests pass, {before} tests",
             f"- with this PR: {'tests pass' if with_pr.passed else 'tests FAIL'}, "
             f"{with_pr.count if with_pr.count is not None else 'no'} tests",
             "- reverted: " + ("`git revert` didn't apply" if not revert_applied else
                               f"{'tests pass' if with_revert and with_revert.passed else 'tests FAIL'}, "
                               f"{with_revert.count if with_revert else 'no'} tests "
                               f"({'matches' if with_revert and with_revert.count == before else 'differs from'} "
                               "the base branch)"),
             f"- files changed: {len(files)}" + (f" ({', '.join(files[:8])}{'…' if len(files) > 8 else ''})"
                                                 if files else "")]
    if not with_pr.passed:
        lines += ["", "End of the failing test output:", "```", with_pr.output[-1500:].strip(), "```"]
    return "\n".join(lines)
