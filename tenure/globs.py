"""
Path patterns, with one rule about which way to be wrong.

Patterns follow the familiar shape: `**` spans directories, `*` and `?` stay
inside one path segment, and a pattern containing `/` is anchored at the
repository root. A pattern without a slash — `*.md`, `Dockerfile*` — is not
anchored, and this is where the two uses differ:

  * `sensitive=True` (never-auto lists) matches a slash-free pattern against
    *any* path component. `*risk*` then covers `src/risk/limits.rs` as well
    as `risk_model.py`. Over-matching here holds a PR for a human; under-
    matching lets an agent merge into code someone marked as never-auto.
  * `sensitive=False` (the patterns that make a PR look harmless, like docs
    or tests) matches only the file's own name. Otherwise `notes.md/run.sh`
    would count as documentation.

Python 3.13 has PurePath.full_match; this has to work on 3.11 and pin exactly
the semantics above, so it is written out.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern: str, sensitive: bool) -> re.Pattern[str]:
    p = pattern.strip().lstrip("/")
    anchored = "/" in p.rstrip("/")
    p = p.rstrip("/") if not p.endswith("/**") else p
    out: list[str] = []
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("/**", i) and i + 3 == len(p):
            out.append("(?:/.*)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    body = "".join(out)
    if anchored:
        return re.compile(f"^{body}(?:/.*)?$" if sensitive else f"^{body}$")
    if sensitive:
        return re.compile(f"^(?:.*/)?{body}(?:/.*)?$")
    return re.compile(f"^(?:.*/)?{body}$")


def matches(path: str, pattern: str, *, sensitive: bool) -> bool:
    return bool(_compile(pattern, sensitive).match(path.lstrip("/")))


def any_match(path: str, patterns: tuple[str, ...] | list[str], *, sensitive: bool) -> str | None:
    """The first pattern that matches, so a decision can say which one."""
    for pattern in patterns:
        if matches(path, pattern, sensitive=sensitive):
            return pattern
    return None
