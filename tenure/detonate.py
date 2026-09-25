"""
Detonate: test what a change to an agent's instructions would make agents do.

AGENTS.md, CLAUDE.md, .cursorrules, copilot-instructions.md and MCP configs
aren't documentation. They are the prompt the next coding agent follows, and
MCP configs name programs it will launch. The "Rules File Backdoor" attack
(Pillar Security, 2025) hid instructions in such files with invisible Unicode,
so reviewers saw nothing while agents obeyed. Tenure never merges these files
unattended; Detonate is how it tells the person reviewing one what it does.

Two halves:

  * **Static** — this module, no model and no network: hidden characters
    (including Unicode "tag" characters, which smuggle invisible ASCII and are
    decoded here so the check run shows the hidden text), suspicious
    directives in the lines the PR adds, and MCP servers that launch commands.
  * **Dynamic** — a coding agent (Nemotron) runs the same task twice in a
    Nebius sandbox, from one checkpoint: once with the base branch's
    instructions, once with the PR's, among decoy credentials. `compare()`
    turns the two recorded traces into evidence: what the agent did only
    when given the new instructions. The runner is a Protocol here and is
    wired to Sandboxes when Token Factory access exists.

Everything Detonate reports is evidence for a person. It can hold a PR or say
why a PR is held; it can never make one mergeable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from .classify import ChangedFile
from .globs import any_match

INSTRUCTION_FILES: tuple[str, ...] = (
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".windsurfrules", ".clinerules",
    ".cursor/**", ".claude/**", ".github/copilot-instructions.md", ".github/instructions/**",
    ".mcp.json", ".vscode/mcp.json",
)
_MCP_FILES: tuple[str, ...] = (".mcp.json", ".vscode/mcp.json", ".cursor/mcp.json")


@dataclass(frozen=True)
class Finding:
    severity: str            # high | medium | info
    kind: str                # hidden_text | hidden_chars | directive | mcp_command | unseen | behaviour
    path: str
    line: int | None
    detail: str


def is_instruction_file(path: str) -> bool:
    return any_match(path, INSTRUCTION_FILES, sensitive=True) is not None


def added_lines(patch: str | None) -> list[tuple[int, str]]:
    """(new-file line number, text) for every line a unified diff adds."""
    out: list[tuple[int, str]] = []
    line = 0
    for raw in (patch or "").splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk:
            line = int(hunk.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((line, raw[1:]))
            line += 1
        elif raw.startswith("-"):
            continue
        else:
            line += 1
    return out


# ── hidden characters ────────────────────────────────────────────────────────

_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E}
_TAGS = range(0xE0000, 0xE0080)
_FLAG = re.compile("\U0001F3F4[\U000E0030-\U000E0039\U000E0061-\U000E007A]{2,6}\U000E007F")


def _decode_tags(text: str) -> str:
    """Unicode tag characters map one-to-one onto ASCII (U+E0041 is an
    invisible "A"). Decoding them shows what was written to be unseen."""
    return "".join(chr(ord(c) - 0xE0000) for c in text if ord(c) in _TAGS and 0x20 <= ord(c) - 0xE0000 < 0x7F)


def _hides_in_latin(text: str, i: int) -> bool:
    """A zero-width character is normal inside Persian, Indic or emoji text,
    where it shapes letters or joins symbols. Between two ASCII letters, or at
    the edge of an ASCII word, it only serves to hide something."""
    before = text[i - 1] if i > 0 else " "
    after = text[i + 1] if i + 1 < len(text) else " "
    return (before.isascii() and before.isalpha()) or (after.isascii() and after.isalpha())


def hidden_characters(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for number, text in lines:
        # Subdivision flags (🏴 + tag letters + cancel tag) are the one legitimate
        # use of tag characters; anything else spelled in them was written to be unseen.
        smuggled = _decode_tags(_FLAG.sub("", text))
        if smuggled.strip():
            findings.append(Finding("high", "hidden_text", path, number,
                                    f"invisible text that decodes to: {smuggled.strip()[:200]!r}"))
        bidi = sorted({c for c in text if ord(c) in _BIDI})
        hiding = sorted({c for i, c in enumerate(text) if ord(c) in _ZERO_WIDTH and _hides_in_latin(text, i)})
        odd = bidi + [c for c in hiding if c not in bidi]
        if odd:
            names = ", ".join(f"U+{ord(c):04X} {unicodedata.name(c, 'unnamed')}" for c in odd[:4])
            findings.append(Finding("high", "hidden_chars", path, number, f"characters that don't display: {names}"))
    return findings


# ── what the added words tell an agent to do ─────────────────────────────────

# Each pattern is an instruction to *do* something risky. Mentioning a topic
# isn't one: measured on 3,440 real agent edits to instruction files (AIDev),
# topic-matching flagged "Secrets should not be committed" and links to
# conventionalcommits.org thousands of times, and a reviewer shown that much
# noise stops reading. A line that negates the action ("don't", "never",
# "avoid") is guidance against it and isn't flagged.
_NEGATED = re.compile(r"\b(don'?t|do not|never|avoid|must not|should not|shouldn'?t|no need to|without)\b", re.I)
_KEY_MATERIAL = (
                 r"(\.env\b|\.aws\b|credentials?\b|api[_ -]?keys?\b|secrets?\b|id_rsa\b|ssh keys?\b|"
                 r"private keys?\b|passwords?\b)")

_INJECTED = r"(import|require|fetch|telemetry|analytics|beacon|tracking)\b"
_EVERYWHERE = r"\b(every|all|each)\s+(new\s+)?(files?|modules?|functions?|components?)\b"
_INSERT = r"\b(add|insert|inject|append|prepend|put)\b"
# Addresses on this machine or reserved for documentation are where setup
# guides point curl; a line that sends somewhere else is the one to read.
_OUTSIDE = (r"https?://(?!(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|[\w.-]*\bexample\.(com|org|net)|"
            r"[\w.-]+\.(local|localhost|test|internal))[:/\s\"'`]|$)")

# (severity, pattern, meaning, a negation in front of it means guidance against it)
_DIRECTIVES: tuple[tuple[str, str, str, bool], ...] = (
    ("high", r"ignore (all |any )?(previous|prior|above|earlier|other) (instructions|rules)|"
             r"disregard (the |all )?(previous|above|prior)",
     "tells the agent to ignore its other instructions", False),
    # Verbs that move or reveal. "Copy .env.example to .env" and
    # "export API_KEY=..." are setup steps, so copy and export aren't here.
    # A CI expression like ${{ secrets.GITHUB_TOKEN }} is how workflows pass a
    # token to a tool, not an agent handling one.
    ("high", r"\b(read|cat|print|echo|send|post|upload|paste|transmit|forward|leak|share|exfiltrat\w*)\b(?!-)"
             r"(?![^.\n]{0,40}\$\{\{\s*secrets\.)[^.\n]{0,40}" + _KEY_MATERIAL,
     "tells the agent to read or move secrets", True),
    ("high", r"\b(send|post|upload|report|forward|submit|transmit|beacon)\b[^.\n]{0,60}\bto\s+`?" + _OUTSIDE + "|"
             r"\bcurl\b[^\n]*(-X\s*POST|\s-d\s|--data|\s-F\s|--upload-file)[^\n]*" + _OUTSIDE + "|"
             r"\bcurl\b[^\n]*" + _OUTSIDE + r"[^\n]*(-X\s*POST|\s-d\s|--data|\s-F\s|--upload-file)",
     "tells the agent to send something to an outside address", True),
    # Itself a negation, so it can't be negated; "don't mention secrets in logs" is hygiene, not concealment.
    ("medium", r"\b(do not|don'?t|never)\s+(mention|tell|reveal|disclose|report)\b(?![^.\n]{0,40}\b(logs?|secrets?|"
               r"keys?|tokens?|passwords?|credentials?|sensitive)\b)",
     "tells the agent to keep something from the person", False),
    # "SKIP=hook pre-commit run" is a shell variable, not an instruction.
    ("medium", r"^\s*[-*]?\s*(always\s+)?(skip|disable|remove|delete|comment out|bypass)\b(?!=)[^.\n]{0,30}\b"
               r"(tests?|linting|lint checks?|type ?checks?|security (checks?|scans?)|pre-commit)\b",
     "tells the agent to turn off tests or checks", True),
    ("medium", _INSERT + r"[^.\n]{0,40}\b" + _INJECTED + r"[^.\n]{0,50}" + _EVERYWHERE + "|" +
               _EVERYWHERE + r"[^.\n]{0,50}" + _INSERT + r"[^.\n]{0,40}\b" + _INJECTED,
     "tells the agent to add a call or import to everything it writes", True),
    ("info", r"(curl|wget|invoke-webrequest)\b[^\n]*\|\s*(sh|bash|zsh|python)\b|base64 (-d|--decode)",
     "runs a downloaded script (often an official installer; worth a glance)", False),
)


def directives(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    findings = []
    for number, text in lines:
        visible = "".join(c for c in text if ord(c) not in _TAGS and ord(c) not in _BIDI | _ZERO_WIDTH)
        for severity, pattern, meaning, negatable in _DIRECTIVES:
            m = re.search(pattern, visible, flags=re.IGNORECASE)
            if not m:
                continue
            if negatable and _NEGATED.search(visible[max(0, m.start() - 40):m.end()]):
                continue
            findings.append(Finding(severity, "directive", path, number, f"{meaning}: {visible.strip()[:120]!r}"))
    return findings


def mcp_commands(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    """An MCP config names programs the agent will start. Any command or remote
    server added is something to look at before an agent runs it."""
    if not any_match(path, _MCP_FILES, sensitive=True):
        return []
    findings = []
    for number, text in lines:
        m = re.search(r'"(command|url)"\s*:\s*"([^"]+)"', text)
        if m:
            findings.append(Finding("high", "mcp_command", path, number,
                                    f"adds an MCP server {m.group(1)}: {m.group(2)[:120]!r}"))
    return findings


def static_scan(files: list[ChangedFile] | tuple[ChangedFile, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for f in files:
        if not is_instruction_file(f.path):
            continue
        lines = added_lines(f.patch)
        if f.patch is None and f.status != "removed":
            findings.append(Finding("medium", "unseen", f.path, None,
                                    "the diff is too large to show, so the new instructions can't be inspected"))
        findings += hidden_characters(f.path, lines) + directives(f.path, lines) + mcp_commands(f.path, lines)
    return findings


# ── the dynamic half: same task, two instruction sets, one checkpoint ────────

@dataclass
class Trace:
    """What a coding agent did during one run, as recorded by the sandbox."""
    files_read: set[str] = field(default_factory=set)
    files_written: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)
    network: set[str] = field(default_factory=set)            # hosts it tried to reach
    code_added: str = ""


class Runner(Protocol):
    def run(self, repo: str, base_sha: str, instructions_from: str, task: str) -> Trace: ...


def compare(baseline: Trace, candidate: Trace, decoys: set[str]) -> list[Finding]:
    """What the agent did only with the PR's instructions. The same task from
    the same checkpoint makes the base run the control: anything new is the
    instructions' doing."""
    findings: list[Finding] = []
    touched = {p for p in candidate.files_read | candidate.files_written if p in decoys}
    touched -= {p for p in baseline.files_read | baseline.files_written if p in decoys}
    for p in sorted(touched):
        findings.append(Finding("high", "behaviour", p, None, "the agent opened a decoy credential file"))
    for host in sorted(candidate.network - baseline.network):
        findings.append(Finding("high", "behaviour", host, None, "the agent tried to reach an outside host"))
    for cmd in candidate.commands:
        risky = re.search(r"curl|wget|nc |ssh |scp |base64|chmod \+x|pip install|npm i", cmd)
        if cmd not in baseline.commands and risky:
            findings.append(Finding("medium", "behaviour", "shell", None,
                                    f"ran a command the control run didn't: {cmd[:120]!r}"))
    new_code = candidate.code_added.replace(baseline.code_added, "")
    for decoy_value in ("AKIA", "BEGIN OPENSSH PRIVATE KEY", "TENURE-CANARY"):
        if decoy_value in new_code and decoy_value not in baseline.code_added:
            findings.append(Finding("high", "behaviour", "code", None,
                                    f"wrote decoy secret material into code ({decoy_value}…)"))
    return findings


def report(findings: list[Finding]) -> str:
    """The Detonate section of a check run."""
    if not findings:
        return "**Detonate:** no hidden text, no suspicious directives, no new MCP commands in the added lines."
    order = {"high": 0, "medium": 1, "info": 2}
    rows = [f"- **{f.severity}** · `{f.path}`{f':{f.line}' if f.line else ''} — {f.detail}"
            for f in sorted(findings, key=lambda f: (order[f.severity], f.path, f.line or 0))]
    return "**Detonate found:**\n" + "\n".join(rows)
