"""
`/tenure` commands, typed by people in comments.

    /tenure grant dependabot deps_patch 90d @alice because patch bumps have been clean since June
    /tenure revoke dependabot deps_patch because a bump broke the build
    /tenure halt because we're mid-incident
    /tenure resume
    /tenure status

Who may run them is checked when the command arrives, against GitHub's own
answer about the sender's permission: maintain or admin, and a person, not a
bot. A bot's comment is never a command — otherwise an agent could write the
comment that grants itself autonomy. The app reads commands only from
comments, never from a PR's title or body.

Lines that are quoted (`> /tenure …`) or inside a fenced code block are not
commands: quoting someone's grant in a reply must not grant it again.
Refused attempts are written to the audit chain too; a bot trying to grant
itself is exactly what a reviewer of that chain wants to find.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from . import identity
from .config import RepoPolicy
from .grants import GRANTABLE, GrantError, GrantStore
from .halt import HaltSwitch

AUTHORITY = frozenset({"admin", "maintain"})
ACTIONS = ("grant", "revoke", "halt", "resume", "status")


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    action: str
    agent: str | None = None
    change_class: str | None = None
    days: int | None = None
    owner: str | None = None
    reason: str = ""


def extract(body: str) -> list[str]:
    """Command lines in a comment, skipping quotes and code blocks."""
    lines, fenced = [], False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        # The command has to open the line. That alone excludes quotes ("> /tenure…"),
        # list items and inline mentions, so they need no rule of their own.
        if line == "/tenure" or line.startswith("/tenure "):
            lines.append(line)
    return lines


_DAYS = re.compile(r"^(\d{1,3})d$")


def parse(line: str) -> Command:
    head, _, reason = line.partition(" because ")
    words = head.split()[1:]                       # drop "/tenure"
    if not words or words[0] not in ACTIONS:
        raise CommandError(f"expected one of: {', '.join(ACTIONS)}")
    action, rest = words[0], words[1:]
    reason = reason.strip()

    if action in ("grant", "revoke"):
        if len(rest) < 2:
            raise CommandError(f"usage: /tenure {action} <agent> <class> ...")
        agent, cls, extra = rest[0], rest[1], rest[2:]
        if cls not in GRANTABLE:
            raise CommandError(f"{cls!r} isn't a grantable class: {', '.join(GRANTABLE)}")
        days = owner = None
        if action == "grant":
            for word in extra:
                if m := _DAYS.match(word):
                    days = int(m.group(1))
                elif word.startswith("@") and len(word) > 1:
                    owner = word[1:]
                else:
                    raise CommandError(f"didn't understand {word!r}")
            if days is None:
                raise CommandError("a grant needs a duration, like 30d: every grant expires")
            if not reason:
                raise CommandError("a grant needs a reason: add 'because …'")
        elif extra:
            raise CommandError(f"didn't understand {' '.join(extra)!r}")
        return Command(action, agent, cls, days, owner, reason)

    if rest:
        raise CommandError(f"/tenure {action} takes no arguments besides 'because …'")
    return Command(action, reason=reason)


def execute(cmd: Command, *, repo: str, sender: str, sender_type: str,
            permission_of: Callable[[str], str], store: GrantStore, switch: HaltSwitch,
            policy: RepoPolicy, now: datetime) -> str:
    """Run one command and return the reply to post. `permission_of(login)` is
    GitHub's answer: admin, maintain, write, triage, read or none."""
    def refuse(why: str) -> str:
        store.audit.record("refused", {"repo": repo, "sender": sender, "sender_type": sender_type,
                                       "command": cmd.action, "why": why}, at=now)
        return f"Not done: {why}"

    if sender_type != "User":
        return refuse("commands from bots are ignored")
    try:
        permission = permission_of(sender)
    except Exception:                                  # noqa: BLE001 — can't confirm means no
        return refuse(f"couldn't confirm @{sender}'s permission")
    if permission not in AUTHORITY:
        return refuse(f"@{sender} has {permission} access; this needs maintain or admin")

    known = set(identity.KNOWN_AGENTS.values()) | set(policy.extra_agents.values())

    if cmd.action == "grant":
        assert cmd.agent and cmd.change_class and cmd.days
        if cmd.agent not in known:
            return refuse(f"{cmd.agent!r} isn't a known agent: {', '.join(sorted(known))}")
        owner = cmd.owner or sender
        try:
            owner_permission = permission if owner == sender else permission_of(owner)
        except Exception:                              # noqa: BLE001
            return refuse(f"couldn't confirm @{owner}'s permission")
        if owner_permission not in AUTHORITY:
            return refuse(f"@{owner} can't own a grant without maintain or admin access")
        try:
            g = store.grant(repo=repo, agent=cmd.agent, change_class=cmd.change_class, owner=owner,
                            granted_by=sender, reason=cmd.reason, days=cmd.days,
                            max_days=policy.max_grant_days, rule_version=policy.rule_version, now=now)
        except GrantError as exc:
            return refuse(str(exc))
        return (f"Granted: {g.agent} may merge {g.change_class} changes (and anything less) here "
                f"without review until {g.expires_at.date().isoformat()}. Owner: @{g.owner}.")

    if cmd.action == "revoke":
        assert cmd.agent and cmd.change_class
        if store.revoke(repo, cmd.agent, cmd.change_class, by=sender,
                        reason=cmd.reason or "no reason given", now=now):
            return f"Revoked: {cmd.agent} can no longer merge {cmd.change_class} changes here unattended."
        return f"Nothing to revoke: {cmd.agent} had no {cmd.change_class} grant here."

    if cmd.action == "halt":
        switch.halt(repo, by=sender, reason=cmd.reason or "no reason given", now=now)
        return "Halted: no agent PR merges here without a person until someone runs /tenure resume."

    if cmd.action == "resume":
        if switch.resume(repo, by=sender, now=now):
            return "Resumed: grants apply again, exactly as they stood before the halt."
        return "Tenure wasn't halted here."

    grants = [g for g in store.all() if g.repo == repo]
    if not grants:
        return "No grants here: every agent PR waits for a person."
    rows = []
    for g in sorted(grants, key=lambda g: (g.agent, g.change_class)):
        try:
            owner_ok = permission_of(g.owner) in AUTHORITY
        except Exception:                              # noqa: BLE001 — same answer a merge would get
            owner_ok = False
        ok, why = g.holds(rule_version=policy.rule_version, now=now, max_days=policy.max_grant_days,
                          owner_has_access=owner_ok)
        fired = g.last_fired.date().isoformat() if g.last_fired else "never"
        rows.append(f"- {g.agent} · {g.change_class} · {'active' if ok else 'not holding'} · "
                    f"{why} · last used {fired}")
    return "\n".join(rows)
