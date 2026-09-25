"""
A tamper-evident record of every decision, grant and command.

Tenure's whole claim is "an agent merged this because a named person granted
it, and here is when". That claim is worth exactly as much as the record
behind it, so the record is a hash chain: each line carries the hash of the
line before, and changing or deleting any line breaks verification of every
line after it. `verify()` names the first line that broke.

The known limit of any hash chain: deleting the *last* lines leaves a shorter
chain that still verifies. The answer is an anchor held somewhere else —
Tenure writes the chain's latest hash into each PR's check run, on GitHub,
where this process can't rewrite it.

Adapted from the audit chain in NoScam (MIT, same author).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(previous: str, body: dict[str, Any]) -> str:
    # Sorted keys and fixed separators: re-serialising a record must give the
    # same bytes, or verification would fail on honest records.
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{previous}{canonical}".encode()).hexdigest()


class AuditLog:
    """Append-only, hash-chained JSON lines."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._last: str | None = None

    def _tail(self) -> str:
        if self._last is None:
            records = self.records()
            self._last = records[-1].get("hash", GENESIS) if records else GENESIS
        return self._last

    def record(self, kind: str, payload: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            previous = self._tail()
            body = {"ts": (at or datetime.now(UTC)).isoformat(), "kind": kind,
                    "payload": payload, "previous": previous}
            entry = {**body, "hash": _digest(previous, body)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._last = entry["hash"]
            return entry

    @property
    def head(self) -> str:
        """The latest hash: the value to anchor outside this machine."""
        return self._tail()

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"corrupt": line})     # reported by verify(), not skipped
        return out

    def verify(self) -> tuple[bool, int | None]:
        """(ok, first broken line), counting lines from 1 as a reader would."""
        previous = GENESIS
        for number, entry in enumerate(self.records(), start=1):
            if "corrupt" in entry:
                return False, number
            body = {k: entry[k] for k in ("ts", "kind", "payload", "previous") if k in entry}
            if len(body) != 4 or entry.get("previous") != previous:
                return False, number
            if _digest(previous, body) != entry.get("hash"):
                return False, number
            previous = entry["hash"]
        return True, None
