"""
From a webhook action to what Tenure does on GitHub.

Everything that decides is elsewhere and pure (classify, policy, commands,
outcomes). This file only gathers the facts, asks, and acts — and records
each step in the audit chain. The shape of every handler is the same: read
fresh state from GitHub rather than trusting the webhook payload, decide,
then act.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from . import config, detonate, outcomes
from .audit import AuditLog
from .classify import classify
from .commands import AUTHORITY, CommandError, execute, extract, parse
from .github import GitHubError, MergeRefused
from .grants import GrantStore
from .halt import HaltSwitch
from .identity import agent_of
from .ledger import Ledger, Merge
from .policy import Decision, PullRequest, Verdict, Verification, decide
from .verify import Unverified, Verifier
from .webhook import BuildFinished, EvaluatePR, PRMerged, RunCommands

POLICY_FILE = ".tenure.yml"

_CONCLUSION = {Verdict.AUTO_MERGE: "success", Verdict.HOLD: "neutral", Verdict.NEVER: "neutral"}


def _title(d: Decision) -> str:
    if d.verdict is Verdict.AUTO_MERGE:
        return f"Merged without review: {d.agent} holds a {d.grant_scope[2]} grant"
    if d.verdict is Verdict.NEVER:
        return "Never merged unattended: " + d.reasons[0]
    return "Held for a person: " + d.reasons[0]


def _summary(d: Decision, audit_head: str, detonation: str | None = None, brief: str = "") -> str:
    lines = [f"**Agent:** {d.agent} · **Change:** {d.change_class} · **Commit:** `{d.head_sha[:7]}`", ""]
    lines += [f"- {r}" for r in d.reasons]
    if detonation:
        lines += ["", detonation]
    if brief:
        lines += ["", brief]
    lines += ["", f"Audit chain head: `{audit_head[:16]}` — recorded here so the log can't be shortened unnoticed."]
    return "\n".join(lines)


class Engine:
    def __init__(self, installation: Callable[[int], Any], *, store: GrantStore, switch: HaltSwitch,
                 ledger: Ledger, audit: AuditLog, verifier: Verifier | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.installation = installation
        self.store, self.switch, self.ledger, self.audit = store, switch, ledger, audit
        self.verifier = verifier or Unverified()
        self.clock = clock

    def _policy(self, inst: Any, repo: str, default_branch: str) -> tuple[config.RepoPolicy, list[str]]:
        """The policy on the default branch. An unreadable one holds everything and says why."""
        try:
            return config.parse(inst.file_text(repo, POLICY_FILE, default_branch)), []
        except config.ConfigError as exc:
            return config.RepoPolicy(), [f"the {POLICY_FILE} on {default_branch} can't be used: {exc}"]
        except GitHubError as exc:
            return config.RepoPolicy(), [f"couldn't read {POLICY_FILE}: {exc}"]

    # ── pull requests ────────────────────────────────────────────────────────

    def evaluate(self, a: EvaluatePR) -> Decision | None:
        inst = self.installation(a.installation)
        pr = inst.pull(a.repo, a.number)
        if pr.get("state") != "open" or pr.get("draft"):
            return None
        policy, flags = self._policy(inst, a.repo, a.default_branch)
        user = pr.get("user") or {}
        agent = agent_of(int(user.get("id", 0)), user.get("type", ""), policy.extra_agents)
        if agent is None:
            return None                                   # people's PRs: no check run, no opinion
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        now = self.clock()

        files = inst.pull_files(a.repo, a.number)
        classification = classify(files, policy)
        # A change to what agents are told gets Detonate's evidence on the check
        # run. It never changes the verdict for the better: these files are
        # never merged unattended; the evidence is for the person who reviews.
        touches_instructions = any(detonate.is_instruction_file(f.path) for f in files)
        findings = detonate.static_scan(files) if touches_instructions else []
        grants = self.store.covering(a.repo, agent, classification.change_class)
        # A sandbox run costs money and only matters when a grant could let the
        # PR through; without one the PR is held whatever the tests say.
        verification = None
        if classification.change_class != "never" and grants:
            try:
                verification = self.verifier.verify(a.repo, a.number, head, base, policy, source=inst)
            except Exception as exc:  # noqa: BLE001 -- any failure to verify holds, and says why
                verification = Verification(head, tests_passed=False, tests_before=0, tests_after=0,
                                            reversible=False, problem=f"the verifier failed ({type(exc).__name__})")
        decision = decide(PullRequest(a.repo, a.number, head), classification, agent=agent, grants=grants,
                          policy=policy, now=now, halted=self.switch.is_halted(a.repo),
                          verification=verification, flags=tuple(flags),
                          owner_has_access=lambda login: inst.role(a.repo, login) in AUTHORITY)
        self.audit.record("decision", {"repo": a.repo, "pr": a.number, "head": head, "agent": agent,
                                       "class": decision.change_class, "verdict": decision.verdict.value,
                                       "reasons": list(decision.reasons),
                                       "detonate": [f.detail for f in findings]}, at=now)

        if decision.verdict is Verdict.AUTO_MERGE:
            try:
                merge_sha = inst.merge(a.repo, a.number, sha=head, title=pr.get("title") or f"#{a.number}")
            except MergeRefused as exc:
                decision = replace(decision, verdict=Verdict.HOLD, reasons=(str(exc), *decision.reasons))
                self.audit.record("merge_refused", {"repo": a.repo, "pr": a.number, "why": str(exc)}, at=now)
            else:
                assert decision.grant_scope is not None
                self.ledger.record(Merge(a.repo, a.number, pr.get("title") or "", head, merge_sha, agent,
                                         decision.change_class, decision.grant_scope, now.isoformat()))
                grant = self.store.get(*decision.grant_scope)
                if grant:
                    self.store.record_fired(grant, now=now, pr=f"{a.repo}#{a.number}")

        inst.check_run(a.repo, head, conclusion=_CONCLUSION[decision.verdict], title=_title(decision),
                       summary=_summary(decision, self.audit.head,
                                        detonate.report(findings) if touches_instructions else None,
                                        verification.brief if verification else ""))
        return decision

    # ── /tenure commands ─────────────────────────────────────────────────────

    def commands(self, a: RunCommands) -> list[str]:
        lines = extract(a.body)
        if not lines:
            return []
        inst = self.installation(a.installation)
        policy, _ = self._policy(inst, a.repo, a.default_branch)
        replies = []
        for line in lines:
            try:
                cmd = parse(line)
            except CommandError as exc:
                replies.append(f"Didn't understand that command: {exc}")
                continue
            replies.append(execute(cmd, repo=a.repo, sender=a.sender, sender_type=a.sender_type,
                                   permission_of=lambda login: inst.role(a.repo, login),
                                   store=self.store, switch=self.switch, policy=policy, now=self.clock()))
        # A bot's command is refused and audited, but never answered: two bots
        # replying to each other is an endless loop.
        if a.sender_type == "User":
            inst.comment(a.repo, a.number, "\n\n".join(replies))
        return replies

    # ── evidence ─────────────────────────────────────────────────────────────

    def merged(self, a: PRMerged) -> str | None:
        evidence = outcomes.reverted(a.repo, a.title, a.body, self.ledger)
        if evidence is None:
            return None
        message = outcomes.apply(evidence, self.store, now=self.clock())
        self.installation(a.installation).comment(a.repo, a.number, message)
        return message

    def build(self, a: BuildFinished) -> str | None:
        evidence = outcomes.broke_main(a.repo, a.head_branch, a.default_branch, a.head_sha, a.conclusion,
                                       self.ledger)
        if evidence is None:
            return None
        message = outcomes.apply(evidence, self.store, now=self.clock())
        self.installation(a.installation).comment(a.repo, evidence.merge.pr, message)
        return message

    def handle(self, action: Any) -> Any:
        if isinstance(action, EvaluatePR):
            return self.evaluate(action)
        if isinstance(action, RunCommands):
            return self.commands(action)
        if isinstance(action, PRMerged):
            return self.merged(action)
        if isinstance(action, BuildFinished):
            return self.build(action)
        return None
