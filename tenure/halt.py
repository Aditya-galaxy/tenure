"""
The kill switch: one command stops every unattended merge in a repository.

It holds merges; it doesn't touch grants. Resuming restores exactly what was
granted before, so pulling the switch in a hurry costs nothing to undo — which
is what makes people willing to pull it.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .audit import AuditLog


class HaltSwitch:
    def __init__(self, path: str | Path, audit: AuditLog) -> None:
        self.path = Path(path)
        self.audit = audit
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _write(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def is_halted(self, repo: str) -> bool:
        return repo in self._read()

    def halt(self, repo: str, *, by: str, reason: str, now: datetime) -> None:
        with self._lock:
            data = self._read()
            data[repo] = {"by": by, "reason": reason, "at": now.isoformat()}
            self._write(data)
            self.audit.record("halt", {"repo": repo, "by": by, "reason": reason}, at=now)

    def resume(self, repo: str, *, by: str, now: datetime) -> bool:
        with self._lock:
            data = self._read()
            if repo not in data:
                return False
            del data[repo]
            self._write(data)
            self.audit.record("resume", {"repo": repo, "by": by}, at=now)
            return True
