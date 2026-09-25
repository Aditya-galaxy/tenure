"""Grants, decisions, commands and the audit chain: the security case, as tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tenure import config
from tenure.audit import AuditLog
from tenure.classify import Classification
from tenure.commands import CommandError, execute, extract, parse
from tenure.grants import GrantError, GrantStore
from tenure.halt import HaltSwitch
from tenure.identity import KNOWN_AGENTS, agent_of
from tenure.policy import PullRequest, Verdict, Verification, decide

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
REPO = "Aditya-galaxy/hyperion-quant"
POLICY = config.RepoPolicy()
SHA = "a" * 40
PR = PullRequest(REPO, 7, SHA)
GOOD = Verification(SHA, tests_passed=True, tests_before=13, tests_after=13, reversible=True)


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def store(tmp_path, audit):
    return GrantStore(tmp_path / "grants.json", audit)


@pytest.fixture
def switch(tmp_path, audit):
    return HaltSwitch(tmp_path / "halts.json", audit)


def grant(store, cls="deps_patch", agent="dependabot", days=30, owner="alice", now=NOW, **kw):
    return store.grant(repo=REPO, agent=agent, change_class=cls, owner=owner, granted_by="alice",
                       reason="clean for a month", days=days, max_days=POLICY.max_grant_days,
                       rule_version=kw.get("rule_version", POLICY.rule_version), now=now)


def classification(c="deps_patch"):
    return Classification(c, ("r",), POLICY.rule_version)


def run(store, *, cls="deps_patch", agent="dependabot", verification=GOOD, halted=False,
        flags=(), owner_ok=lambda login: True, now=NOW, pr=PR, policy=POLICY):
    return decide(pr, Classification(cls, ("r",), policy.rule_version), agent=agent,
                  grants=store.covering(REPO, agent, cls) if agent else [], policy=policy, now=now,
                  halted=halted, verification=verification, flags=flags, owner_has_access=owner_ok)


# ── identity ─────────────────────────────────────────────────────────────────

def test_agents_are_known_by_id_and_bot_type_only():
    assert agent_of(49699333, "Bot") == "dependabot"
    assert agent_of(198982749, "Bot") == "copilot"
    assert agent_of(49699333, "User") is None          # a person can't be an agent
    assert agent_of(12345, "Bot") is None


def test_config_cannot_rename_a_known_agent():
    assert agent_of(49699333, "Bot", extra={49699333: "renovate"}) == "dependabot"
    assert agent_of(777, "Bot", extra={777: "inhouse-bot"}) == "inhouse-bot"


# ── decisions ────────────────────────────────────────────────────────────────

def test_cold_start_holds_everything(store):
    d = run(store)
    assert d.verdict is Verdict.HOLD
    assert any("/tenure grant dependabot deps_patch" in r for r in d.reasons)


def test_a_holding_grant_merges_the_verified_commit(store):
    grant(store)
    d = run(store)
    assert d.verdict is Verdict.AUTO_MERGE
    assert d.head_sha == SHA
    assert d.grant_scope == (REPO, "dependabot", "deps_patch")


def test_people_are_not_governed(store):
    assert run(store, agent=None).verdict is Verdict.IGNORE


def test_never_is_never_even_with_a_code_grant(store):
    grant(store, cls="code")
    assert run(store, cls="never").verdict is Verdict.NEVER


def test_grant_covers_classes_below_not_above(store):
    grant(store, cls="deps")
    assert run(store, cls="docs").verdict is Verdict.AUTO_MERGE
    assert run(store, cls="code").verdict is Verdict.HOLD


def test_a_grant_is_for_one_agent(store):
    grant(store, agent="dependabot")
    assert run(store, agent="renovate").verdict is Verdict.HOLD


@pytest.mark.parametrize("verification, fragment", [
    (None, "not verified"),
    (Verification("b" * 40, True, 13, 13, True), "PR is now at"),        # a commit pushed after checking
    (Verification(SHA, False, 13, 13, True), "tests fail"),
    (Verification(SHA, True, 13, 12, True), "reduces the test count"),
    (Verification(SHA, True, 13, 13, False), "undoable"),
])
def test_anything_short_of_a_clean_verification_holds(store, verification, fragment):
    grant(store)
    d = run(store, verification=verification)
    assert d.verdict is Verdict.HOLD
    assert any(fragment in r for r in d.reasons)


def test_halt_holds_but_keeps_grants(store, switch):
    grant(store)
    switch.halt(REPO, by="alice", reason="incident", now=NOW)
    assert run(store, halted=switch.is_halted(REPO)).verdict is Verdict.HOLD
    switch.resume(REPO, by="alice", now=NOW)
    assert run(store, halted=switch.is_halted(REPO)).verdict is Verdict.AUTO_MERGE


def test_a_flag_holds(store):
    grant(store)
    d = run(store, flags=("the PR description addresses the reviewer",))
    assert d.verdict is Verdict.HOLD


def test_every_reason_is_reported_not_just_the_first(store):
    d = run(store, verification=None, halted=True, flags=("x",))
    assert len(d.reasons) >= 4


# ── grants hold only while every condition does ──────────────────────────────

def test_grant_expires_fail_closed(store):
    grant(store, days=30)
    assert run(store, now=NOW + timedelta(days=29)).verdict is Verdict.AUTO_MERGE
    assert run(store, now=NOW + timedelta(days=30)).verdict is Verdict.HOLD


def test_shortening_max_days_shortens_existing_grants(store):
    grant(store, days=60)
    stricter = config.parse("max_grant_days: 14")
    d = run(store, now=NOW + timedelta(days=20), policy=stricter)
    assert d.verdict is Verdict.HOLD and any("expired" in r for r in d.reasons)


def test_owner_losing_access_stops_the_grant(store):
    grant(store, owner="bob")
    assert run(store, owner_ok=lambda login: login != "bob").verdict is Verdict.HOLD


def test_unanswerable_access_question_is_a_no(store):
    grant(store)
    def broken(login):
        raise TimeoutError("GitHub didn't answer")
    assert run(store, owner_ok=broken).verdict is Verdict.HOLD


def test_changed_rules_stop_old_grants(store):
    grant(store, rule_version="old-rules")
    d = run(store)
    assert d.verdict is Verdict.HOLD and any("rules changed" in r for r in d.reasons)


def test_suspension_holds_until_renewed(store):
    g = grant(store)
    store.suspend(g, reason="a merge was reverted", now=NOW)
    assert run(store).verdict is Verdict.HOLD
    grant(store)                                          # a person decides again
    assert run(store).verdict is Verdict.AUTO_MERGE


def test_narrowest_holding_grant_is_used(store):
    grant(store, cls="deps")
    grant(store, cls="docs")
    assert run(store, cls="docs").grant_scope[2] == "docs"


def test_a_broader_grant_still_applies_when_the_narrow_one_lapsed(store):
    grant(store, cls="docs", days=1)
    grant(store, cls="deps", days=30)
    assert run(store, cls="docs", now=NOW + timedelta(days=2)).grant_scope[2] == "deps"


@pytest.mark.parametrize("kw, message", [
    ({"cls": "never"}, "can't grant"),
    ({"days": 0}, "1 to"),
    ({"days": 91}, "1 to"),
])
def test_bad_grants_are_refused(store, kw, message):
    with pytest.raises(GrantError, match=message):
        grant(store, **kw)


def test_expire_due_records_lapses(store, audit):
    grant(store, days=1)
    assert store.expire_due(now=NOW + timedelta(days=2), max_days=90)
    assert store.all() == []
    assert audit.records()[-1]["kind"] == "lapse"


# ── commands ─────────────────────────────────────────────────────────────────

def test_extract_skips_quotes_and_code_blocks():
    body = "\n".join([
        "Looks good.",
        "/tenure grant dependabot deps_patch 30d because clean",
        "> /tenure grant devin code 90d because quoted, must not run",
        "```",
        "/tenure halt because inside a code block",
        "```",
        "/tenurefoo not a command",
    ])
    assert extract(body) == ["/tenure grant dependabot deps_patch 30d because clean"]


def test_parse_grant():
    c = parse("/tenure grant dependabot deps_patch 30d @bob because clean since June")
    assert (c.action, c.agent, c.change_class, c.days, c.owner, c.reason) == \
        ("grant", "dependabot", "deps_patch", 30, "bob", "clean since June")


@pytest.mark.parametrize("line, fragment", [
    ("/tenure grant dependabot deps_patch because no duration", "duration"),
    ("/tenure grant dependabot deps_patch 30d", "reason"),
    ("/tenure grant dependabot never 30d because x", "grantable"),
    ("/tenure explode", "expected one of"),
    ("/tenure grant dependabot deps_patch 30d please because x", "didn't understand"),
])
def test_parse_refuses_incomplete_commands(line, fragment):
    with pytest.raises(CommandError, match=fragment):
        parse(line)


def perms(**table):
    return lambda login: table.get(login, "none")


def exec_(cmd, store, switch, sender="alice", sender_type="User", permission_of=None):
    return execute(parse(cmd), repo=REPO, sender=sender, sender_type=sender_type,
                   permission_of=permission_of or perms(alice="admin", bob="maintain", carol="write"),
                   store=store, switch=switch, policy=POLICY, now=NOW)


def test_a_maintainer_can_grant(store, switch):
    reply = exec_("/tenure grant dependabot deps_patch 30d because clean", store, switch)
    assert reply.startswith("Granted")
    assert store.get(REPO, "dependabot", "deps_patch").owner == "alice"


def test_a_bot_cannot_grant_itself_and_the_attempt_is_recorded(store, switch, audit):
    reply = exec_("/tenure grant dependabot code 90d because trust me", store, switch,
                  sender="dependabot[bot]", sender_type="Bot",
                  permission_of=perms(**{"dependabot[bot]": "admin"}))
    assert reply.startswith("Not done")
    assert store.all() == []
    assert audit.records()[-1]["kind"] == "refused"


def test_write_access_is_not_enough(store, switch):
    assert exec_("/tenure grant dependabot docs 30d because x", store, switch,
                 sender="carol").startswith("Not done")


def test_the_owner_needs_authority_too(store, switch):
    reply = exec_("/tenure grant dependabot docs 30d @carol because x", store, switch)
    assert reply.startswith("Not done") and "carol" in reply


def test_unknown_agents_cannot_be_granted(store, switch):
    assert "known agent" in exec_("/tenure grant skynet code 30d because x", store, switch)


def test_permission_lookup_failure_refuses(store, switch):
    def broken(login):
        raise TimeoutError
    assert exec_("/tenure halt", store, switch, permission_of=broken).startswith("Not done")


def test_revoke_halt_resume_status(store, switch):
    exec_("/tenure grant dependabot deps_patch 30d because clean", store, switch)
    assert "dependabot · deps_patch · active" in exec_("/tenure status", store, switch)
    assert exec_("/tenure halt because incident", store, switch).startswith("Halted")
    assert switch.is_halted(REPO)
    assert exec_("/tenure resume", store, switch).startswith("Resumed")
    assert exec_("/tenure revoke dependabot deps_patch because done", store, switch).startswith("Revoked")
    assert store.all() == []


def test_status_shows_a_grant_whose_owner_left_as_not_holding(store, switch):
    exec_("/tenure grant dependabot docs 30d @bob because x", store, switch)
    reply = exec_("/tenure status", store, switch, permission_of=perms(alice="admin"))
    assert "not holding" in reply and "@bob" in reply


# ── the audit chain ──────────────────────────────────────────────────────────

def test_every_governance_change_is_chained_and_verifies(store, switch, audit):
    exec_("/tenure grant dependabot deps_patch 30d because clean", store, switch)
    exec_("/tenure halt because incident", store, switch)
    exec_("/tenure resume", store, switch)
    kinds = [r["kind"] for r in audit.records()]
    assert kinds == ["grant", "halt", "resume"]
    assert audit.verify() == (True, None)


def test_editing_a_record_is_detected(audit):
    for i in range(3):
        audit.record("x", {"i": i}, at=NOW)
    lines = audit.path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["i"] = 99
    lines[1] = json.dumps(tampered, sort_keys=True)
    audit.path.write_text("\n".join(lines) + "\n")
    assert AuditLog(audit.path).verify() == (False, 2)


def test_deleting_a_middle_record_is_detected(audit):
    for i in range(3):
        audit.record("x", {"i": i}, at=NOW)
    lines = audit.path.read_text().splitlines()
    audit.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert AuditLog(audit.path).verify() == (False, 2)


def test_head_is_the_anchor_for_tail_truncation(audit):
    """Cutting the last line leaves a valid chain — which is why the head is
    published elsewhere: the published value no longer matches."""
    for i in range(3):
        audit.record("x", {"i": i}, at=NOW)
    published = audit.head
    lines = audit.path.read_text().splitlines()
    audit.path.write_text("\n".join(lines[:2]) + "\n")
    reopened = AuditLog(audit.path)
    assert reopened.verify() == (True, None)
    assert reopened.head != published


def test_known_agents_ids_are_unique():
    assert len(set(KNOWN_AGENTS.values())) == len(KNOWN_AGENTS)
