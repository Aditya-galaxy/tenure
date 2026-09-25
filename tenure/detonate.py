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
    severity: str            # high | medium
    kind: str                # hidden_text | hidden_chars | directive | mcp_command | behaviour
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


def _decode_tags(text: str) -> str:
    """Unicode tag characters map one-to-one onto ASCII (U+E0041 is an
    invisible "A"). Decoding them shows what was written to be unseen."""
    return "".join(chr(ord(c) - 0xE0000) for c in text if ord(c) in _TAGS and 0x20 <= ord(c) - 0xE0000 < 0x7F)


def hidden_characters(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for number, text in lines:
        smuggled = _decode_tags(text)
        if smuggled.strip():
            findings.append(Finding("high", "hidden_text", path, number,
                                    f"invisible text that decodes to: {smuggled.strip()[:200]!r}"))
        odd = sorted({c for c in text if ord(c) in _BIDI or ord(c) in _ZERO_WIDTH})
        if odd:
            names = ", ".join(f"U+{ord(c):04X} {unicodedata.name(c, 'unnamed')}" for c in odd[:4])
            findings.append(Finding("high", "hidden_chars", path, number, f"characters that don't display: {names}"))
    return findings


# ── what the added words tell an agent to do ─────────────────────────────────

_DIRECTIVES: tuple[tuple[str, str], ...] = (
    (r"ignore (all |any )?(previous|prior|above|earlier) (instructions|rules)|disregard (the )?(previous|above)",
     "tells the agent to ignore its other instructions"),
    (r"(do not|don'?t|never) (mention|tell|reveal|report|disclose|log|explain)",
     "tells the agent to keep something from the person"),
    (r"(\.env\b|\.aws|credentials?|api[_ -]?keys?|secrets?|tokens?|ssh|id_rsa|private key|password)",
     "refers to secrets or credentials"),
    (r"(curl|wget|invoke-webrequest)\b[^\n]*\|\s*(sh|bash|zsh|python)|base64 (-d|--decode)",
     "downloads and runs something, or decodes hidden content"),
    (r"(skip|disable|remove|delete)\b[^\n]{0,30}\b(tests?|lint(ing|er)?|checks?|ci|security)",
     "tells the agent to turn off tests or checks"),
    (r"https?://(?!github\.com/|docs\.|localhost)[\w.-]+", "points the agent at an outside address"),
    (r"(always|every (file|change|commit)|in all (files|code))\b[^\n]{0,60}\b(add|include|insert|import)",
     "tells the agent to add something to everything it writes"),
)


def directives(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    findings = []
    for number, text in lines:
        visible = "".join(c for c in text if ord(c) not in _TAGS and ord(c) not in _BIDI | _ZERO_WIDTH)
        for pattern, meaning in _DIRECTIVES:
            if re.search(pattern, visible, flags=re.IGNORECASE):
                findings.append(Finding("medium", "directive", path, number, f"{meaning}: {visible.strip()[:120]!r}"))
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
            findings.append(Finding("medium", "directive", f.path, None,
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
    order = {"high": 0, "medium": 1}
    rows = [f"- **{f.severity}** · `{f.path}`{f':{f.line}' if f.line else ''} — {f.detail}"
            for f in sorted(findings, key=lambda f: (order[f.severity], f.path, f.line or 0))]
    return "**Detonate found:**\n" + "\n".join(rows)
