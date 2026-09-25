# Tenure

**Decides which agent-written pull requests may merge without a human — and
takes that right away when a merge goes wrong.**

Coding agents now open more pull requests than people can review. Every
vendor draws the same line: agents propose, humans click merge. That keeps the
review queue as the bottleneck, and leaves one question unanswered: *which*
agent PRs could safely skip review, and who decided that?

Tenure answers it the way access is granted anywhere it matters. Merge
authority is **earned per repository, per agent and per kind of change**. It
is **granted by a named person, always expires, and is withdrawn by
evidence** — a revert, a broken main — without anyone having to notice.

> **Status: in development, private.** The policy core below is built and
> tested. The GitHub App, sandbox verification and model integration are next.
> Built for the Nebius × NVIDIA Global AI Hackathon.

## The rules it is built on

1. **An agent is identified by something nobody can type** — GitHub's numeric
   user ID, with `type: Bot`. Never a login, branch name or commit trailer.
2. **The model only makes things stricter.** A model's opinion can move a PR
   to a stricter class or flag it; it can never loosen a decision.
3. **An agent can't change the test it's judged by.** Tests run from the base
   branch, and CI config is never merged unattended.
4. **Policy lives on the default branch,** and a PR touching it is never
   merged unattended.
5. **Only people grant autonomy, and only people with authority** — maintain
   or admin, checked when the command arrives. A bot's comment is never a
   command.
6. **Evidence only takes autonomy away.** Every grant expires; an owner who
   loses access takes the grant's force with them; changed rules void old
   grants until a person renews them.
7. **Merge exactly what was verified** — pinned to the verified commit.
8. **Everything is recorded** in a hash-chained audit log.

## What's here now

| Module | Does |
|---|---|
| `identity.py` | agent by numeric bot ID (Copilot, Claude Code, Codex, Cursor, Devin, Jules, Dependabot, Renovate) |
| `config.py` | `.tenure.yml`: repositories can add never-auto paths, never remove the built-in ones |
| `classify.py` | `docs < tests < deps_patch < deps < code < never`, from files and diffs |
| `grants.py` | earned authority: scope, owner, expiry, pinned rules, suspension |
| `policy.py` | the pure decision: `AUTO_MERGE`, `HOLD`, `NEVER` or `IGNORE`, with every reason |
| `commands.py`, `halt.py` | `/tenure grant · revoke · halt · resume · status` |
| `audit.py` | tamper-evident record of every decision and change |

```bash
pip install -e ".[dev]"
pytest -q
```

Never-auto by default, in every repository: CI configuration, `.tenure.yml`,
`CODEOWNERS`, `.gitmodules`, licence files, and **instruction files for coding
agents** (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, …) — an agent that edits
those is writing the prompt for the next agent.

## Licence

Apache-2.0.
