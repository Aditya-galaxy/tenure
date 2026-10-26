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

> Built for the Nebius × NVIDIA Global AI Hackathon (Coding and Agentic
> Engineering). Running live as the GitHub App
> [Tenure Merge](https://github.com/apps/tenure-merge) on
> [Aditya-galaxy/hyperion](https://github.com/Aditya-galaxy/hyperion).

## See it live

- **What it has decided:** https://tenure-761390104675.us-central1.run.app/ is a
  read-only page with the grants in force, the merges Tenure made with the
  commits it verified, and the audit chain (`/api/state` has the same as JSON).
- **On real pull requests:** Dependabot's PRs on hyperion, 26 Oct 2026.
  - [#39](https://github.com/Aditya-galaxy/hyperion/pull/39)–[#42](https://github.com/Aditya-galaxy/hyperion/pull/42)
    bump GitHub Actions, so they edit CI: *never merged unattended*, whatever is granted.
  - [#43](https://github.com/Aditya-galaxy/hyperion/pull/43) was held until a
    maintainer granted `dependabot` the `deps` class for 30 days. Tenure then
    planned the proof with Nemotron, ran the tests at the base, with the PR and
    reverted, and merged the exact commit it had verified.
  - [#44](https://github.com/Aditya-galaxy/hyperion/pull/44) was verified too (101
    Python tests three ways), and GitHub refused Tenure's merge because CI was
    still running: *CI must pass, even for Tenure*.
  - [#45](https://github.com/Aditya-galaxy/hyperion/pull/45) reverted #43, and Tenure
    withdrew the grant on its own. #44 went back to waiting for a person.

## How it uses Nemotron, Token Factory and Sandboxes

- **Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`), called through
  **Nebius Token Factory**'s OpenAI-compatible API, is the verification agent's
  planner. Given only the **base branch**'s file list and build and CI files,
  plus the names of the top-level folders the PR touches, it writes a recipe:
  a container image, setup commands and one test command. When a setup step
  fails it reads the error and repairs the recipe, a bounded number of times.
  It never sees the PR's contents and never decides a merge: exit codes and the
  test framework's own summary do. It runs at `reasoning_effort=low` (a 95-token
  plan in 2 s, against 5,681 tokens and 27 s without), under a daily token cap
  kept in the state directory.
- **Sandboxes:** every recipe runs from snapshots. The base is built once; from
  that checkpoint the tests run with the PR applied and again after
  `git revert`, so "the change can be undone" is tested, not assumed. Tenure
  supports **Nebius Token Factory Sandboxes** (`ContreeSandbox`, the ConTree
  SDK). They weren't activated on our project before the deadline, so the live
  deployment runs the same interface on **a runner of our own**
  (`tenure/runner.py`): a separate private service with no permissions and no
  secrets, so untrusted code never runs where the keys are. One setting,
  `TENURE_RUNNER_URL`, chooses between them.

## Run it

```bash
pip install -e ".[dev,sandbox]"
pytest -q                                    # offline
python tools/create_app.py                   # creates the GitHub App from deploy/github-app-manifest.json
source ~/.config/tenure/app.env
export NEBIUS_API_KEY=... NEBIUS_PROJECT_ID=...   # Token Factory (and Sandboxes, if activated)
python -m tenure.app                         # the webhook service, :8080
```

[`deploy/README.md`](deploy/README.md) covers pointing the App's webhook at it and
the two rulesets that let Tenure merge past review, and never past CI.
`tools/verify_pr.py owner/repo N` runs the verification agent on one PR by
hand and prints every step.

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
| `webhook.py`, `app.py` | GitHub webhooks: signature-checked, each delivery handled once, acknowledged within GitHub's time limit |
| `github.py` | the GitHub App: installation tokens, PR files, check runs, sha-pinned merges, maintain-vs-write roles |
| `engine.py` | from a webhook to a decision, a check run and (only if everything holds) a merge |
| `outcomes.py`, `ledger.py` | a revert revokes the grant that allowed the merge; a red main at the merge commit suspends it |
| `detonate.py` | any PR touching agent instructions or MCP configs: invisible text decoded, suspicious directives, new MCP commands, all posted on the check run. The sandbox half runs the same task with the old and the new instructions and reports what only the new ones made the agent do |
| `agent.py` | the verification agent. Nemotron plans a build and test recipe from the **base branch only** and repairs it when a step fails. From one sandbox checkpoint, Tenure then runs the tests at the base, with the PR applied, and after `git revert`, and judges the runs by exit codes and the test framework's own summary, never by the model |
| `sandbox.py`, `model.py` | snapshot sandboxes: Nebius Token Factory Sandboxes (ConTree), or our own runner over HTTPS; Nemotron through Token Factory's OpenAI-compatible API, with a daily token cap. No secrets go into a sandbox |
| `runner.py` | the runner: a separate private service, no permissions, no secrets, Rust and Python toolchains, one run at a time |
| `dashboard.py` | the live read-only page and `/api/state` |
| `verify.py` | the verifier interface. Without `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID`, nothing is verified, so nothing merges |

```bash
pip install -e ".[dev,sandbox]"
pytest -q

# the verification agent on one real PR (reads GitHub, writes nothing)
export NEBIUS_API_KEY=... NEBIUS_PROJECT_ID=...
GH_TOKEN=$(gh auth token) python tools/verify_pr.py owner/repo 12
```

The agent only verifies a PR a grant could let through, because sandbox time costs
money and without a grant the PR is held whatever its tests say.

Never-auto by default, in every repository: CI configuration, `.tenure.yml`,
`CODEOWNERS`, `.gitmodules`, licence files, and **instruction files for coding
agents** (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, …) — an agent that edits
those is writing the prompt for the next agent.

## Licence

Apache-2.0.
