"""
Earned merge authority: who may merge what without a human, and until when.

A grant is scoped to one repository, one agent and one change class — say,
"dependabot may merge deps_patch in owner/app". It covers that class and every
class below it (see classify.ORDER). It is always granted by a person, and it
carries four things that decide whether it still holds:

  * An expiry, always. There are no permanent grants. Autonomy that isn't
    renewed lapses, so inattention withdraws it instead of extending it. A
    repository that later shortens `max_grant_days` shortens existing grants
    too: the read path uses whichever end comes first.
  * An owner: the person accountable for it now. If the owner loses
    maintain/admin access to the repository, the grant stops holding, because
    nobody left can answer for it.
  * The classification rules it was granted under. If the rules change, the
    grant stops holding: it was a decision about changes classified the old
    way, and nobody has decided about the new way yet.
  * A suspension, latched by evidence and lifted only by a person renewing.

Only people add or renew grants. Evidence — a revert, a broken main — only
takes them away. That asymmetry is the point.

Ideas carried over from Kronagent's allowlist; the code is new.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from .audit import AuditLog
from .classify import ORDER

GRANTABLE = tuple(c for c in ORDER if c != "never")
_RANK = {name: i for i, name in enumerate(ORDER)}


@dataclass(frozen=True)
class Grant:
    repo: str
    agent: str
    change_class: str
    owner: str
    granted_by: str
    reason: str
    granted_at: datetime
    expires_at: datetime
    rule_version: str
    last_fired: datetime | None = None
    suspended: str | None = None

    @property
    def scope(self) -> tuple[str, str, str]:
        return (self.repo, self.agent, self.change_class)

    def covers(self, change_class: str) -> bool:
        return change_class in _RANK and change_class != "never" \
            and _RANK[change_class] <= _RANK[self.change_class]

    def ends(self, max_days: int) -> datetime:
        return min(self.expires_at, self.granted_at + timedelta(days=max_days))

    def holds(self, *, rule_version: str, now: datetime, max_days: int,
              owner_has_access: bool) -> tuple[bool, str]:
        """Whether this grant authorises a merge right now, and if not, why not."""
        if self.suspended:
            return False, f"suspended: {self.suspended}"
        if now >= self.ends(max_days):
            return False, f"expired {self.ends(max_days).date().isoformat()}"
        if self.rule_version != rule_version:
            return False, "classification rules changed since it was granted; it needs renewing"
        if not owner_has_access:
            return False, f"owner @{self.owner} no longer has maintain or admin access"
        return True, f"granted by @{self.granted_by}, owned by @{self.owner}, " \
                     f"until {self.ends(max_days).date().isoformat()}"


class GrantError(ValueError):
    pass


def _encode(g: Grant) -> dict:
    d = asdict(g)
    for key in ("granted_at", "expires_at", "last_fired"):
        d[key] = d[key].isoformat() if d[key] else None
    return d


def _decode(d: dict) -> Grant:
    d = dict(d)
    for key in ("granted_at", "expires_at", "last_fired"):
        d[key] = datetime.fromisoformat(d[key]) if d[key] else None
    return Grant(**d)


class GrantStore:
    """Grants on disk, one per scope, every change written to the audit chain."""

    def __init__(self, path: str | Path, audit: AuditLog) -> None:
        self.path = Path(path)
        self.audit = audit
        self._lock = threading.Lock()

    # ── reading ──────────────────────────────────────────────────────────────

    def all(self) -> list[Grant]:
        if not self.path.exists():
            return []
        return [_decode(d) for d in json.loads(self.path.read_text(encoding="utf-8"))]

    def get(self, repo: str, agent: str, change_class: str) -> Grant | None:
        return next((g for g in self.all() if g.scope == (repo, agent, change_class)), None)

    def covering(self, repo: str, agent: str, change_class: str) -> list[Grant]:
        """Every grant that could authorise this change, narrowest first — the
        grant made for exactly this kind of change is the one to use when it
        holds, not a broader one that happens to cover it."""
        found = [g for g in self.all() if g.repo == repo and g.agent == agent and g.covers(change_class)]
        return sorted(found, key=lambda g: _RANK[g.change_class])

    # ── writing ──────────────────────────────────────────────────────────────

    def _save(self, grants: list[Grant]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".grants-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([_encode(g) for g in grants], fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)          # a crash leaves the old file or the new, never half

    def _put(self, grant: Grant) -> None:
        self._save([g for g in self.all() if g.scope != grant.scope] + [grant])

    def grant(self, *, repo: str, agent: str, change_class: str, owner: str, granted_by: str,
              reason: str, days: int, max_days: int, rule_version: str, now: datetime) -> Grant:
        """Create or renew. Renewing is how a suspension is lifted: it is a
        fresh decision by a person, against the rules as they are today."""
        if change_class not in GRANTABLE:
            raise GrantError(f"can't grant {change_class!r}; grantable: {', '.join(GRANTABLE)}")
        if not 1 <= days <= max_days:
            raise GrantError(f"a grant lasts 1 to {max_days} days in this repository")
        if not reason.strip():
            raise GrantError("a grant needs a reason, so whoever reviews it later knows why")
        with self._lock:
            before = self.get(repo, agent, change_class)
            g = Grant(repo=repo, agent=agent, change_class=change_class, owner=owner.lstrip("@"),
                      granted_by=granted_by.lstrip("@"), reason=reason.strip(), granted_at=now,
                      expires_at=now + timedelta(days=days), rule_version=rule_version,
                      last_fired=before.last_fired if before else None)
            self._put(g)
            self.audit.record("renew" if before else "grant", _encode(g), at=now)
            return g

    def revoke(self, repo: str, agent: str, change_class: str, *, by: str, reason: str,
               now: datetime) -> bool:
        with self._lock:
            g = self.get(repo, agent, change_class)
            if g is None:
                return False
            self._save([x for x in self.all() if x.scope != g.scope])
            self.audit.record("revoke", {"scope": list(g.scope), "by": by, "reason": reason}, at=now)
            return True

    def suspend(self, grant: Grant, *, reason: str, now: datetime) -> Grant:
        with self._lock:
            current = self.get(*grant.scope)
            if current is None or current.suspended:
                return current or grant
            s = replace(current, suspended=reason)
            self._put(s)
            self.audit.record("suspend", {"scope": list(s.scope), "reason": reason}, at=now)
            return s

    def record_fired(self, grant: Grant, *, now: datetime, pr: str) -> None:
        """A grant that never fires is standing authority with no benefit;
        knowing when each one last did is what makes that visible."""
        with self._lock:
            current = self.get(*grant.scope)
            if current is None:
                return
            self._put(replace(current, last_fired=now))
            self.audit.record("fired", {"scope": list(current.scope), "pr": pr}, at=now)

    def expire_due(self, *, now: datetime, max_days: int) -> list[Grant]:
        """Clear lapsed grants and write each lapse into the audit chain. The
        read path already refuses them; this records that they ended."""
        with self._lock:
            grants = self.all()
            lapsed = [g for g in grants if now >= g.ends(max_days)]
            if lapsed:
                self._save([g for g in grants if g not in lapsed])
                for g in lapsed:
                    self.audit.record("lapse", {"scope": list(g.scope),
                                                "ended": g.ends(max_days).isoformat()}, at=now)
            return lapsed
