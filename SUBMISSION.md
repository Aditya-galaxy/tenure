# Tenure — submission

**Nebius × NVIDIA Global AI Hackathon · Track: Coding and Agentic Engineering.**
Solo entry by Aditya (GitHub `Aditya-galaxy`).

| | |
|---|---|
| Live demo | https://tenure-761390104675.us-central1.run.app/ (read-only; JSON at `/api/state`) |
| The App | https://github.com/apps/tenure-merge |
| Running on | https://github.com/Aditya-galaxy/hyperion (PRs #39–#45) |
| Code | https://github.com/Aditya-galaxy/tenure (Apache-2.0) |
| Video | _link to be added_ |
| Feedback on Nebius and NVIDIA tools | [NEBIUS_FEEDBACK.md](NEBIUS_FEEDBACK.md) |

## In one sentence

GitHub's Agent Merge merges as soon as GitHub allows; **Tenure decides what
GitHub allows**: which agent-written pull requests may merge without a person,
on whose authority, for how long, and it takes that right away when a merge
goes wrong.

## The problem

Coding agents now open more pull requests than people can review. Every vendor
draws the same line: agents propose, people click merge. That keeps review as
the bottleneck and leaves the real question unanswered: *which* agent PRs could
safely skip review, and who decided that?

Our replay of 34,891 real merged agent PRs (below) shows the two easy answers
both fail. Merging when CI is green let every later-reverted PR through. A list
of risky paths caught only 12% of them, because reverts land in ordinary code.

## What Tenure does

1. **Holds every agent PR by default,** and says why on a check run. An agent is
   identified by GitHub's numeric bot ID, never a name anyone can type.
2. **A person grants autonomy,** with a comment on a PR:
   `/tenure grant dependabot deps 30d @owner because …`. A grant names one
   repository, one agent and one kind of change; it has an owner and always
   expires. Only maintainers can grant; a bot's comment is never a command.
3. **Under a grant, the verification agent proves the PR.** Nemotron 3 Super,
   through Nebius Token Factory, plans a build-and-test recipe from the base
   branch only, and repairs it when a step fails. From one sandbox checkpoint
   the tests run at the base, with the PR, and after `git revert`. Exit codes
   and the test framework's own counts decide; the model never does.
4. **Tenure merges exactly the commit it verified,** past the review rule but
   never past CI.
5. **Evidence takes the grant away:** a revert of a Tenure merge, or a red main
   at its merge commit. Nobody has to notice.
6. **Everything is in a hash-chained audit log,** whose head is printed on every
   check run so the log can't be shortened unnoticed.

Never merged unattended, whatever is granted: changes to CI, agent
instructions, MCP configs, licences and Tenure's own policy file.

## It ran, on real pull requests (26 Oct 2026)

On [hyperion](https://github.com/Aditya-galaxy/hyperion), with Dependabot as the agent:

| PR | What happened |
|---|---|
| #39–#42 | GitHub Actions bumps edit CI: **never merged unattended** |
| #43 | Held, then granted (`deps`, 30 days, owned by @Aditya-galaxy). Verified: tests pass at the base, with the PR and reverted. **Merged by Tenure** at the verified commit |
| #44 | Verified the same way (101 Python tests, three runs). **GitHub refused Tenure's merge because CI was still running:** the rule that binds Tenure too |
| #45 | Reverted #43. **Tenure withdrew the grant on its own;** #44 went back to waiting for a person |

The live page shows each of these as it happened.

## Evidence from 71,677 real agent PRs

From the AIDev dataset ([`eval/README.md`](eval/README.md)):

- **Agents differ.** 28% of Claude Code's PRs and 24% of Copilot's touch a path
  Tenure never merges unattended, against 7% for Codex. One rule for "AI PRs"
  is wrong in both directions.
- **Replaying 34,891 merges and 212 later reverts in time order:**

  | Policy | merged without a person | leak (later reverted) |
  |---|---:|---:|
  | merge when green | 100% | 0.61% |
  | path rules only | 89% | 0.60% |
  | **Tenure** (docs, tests, patch bumps; grant after 5 clean merges) | **15.4%** | **0.02%** |
  | Tenure with code grantable | 58% | 0.16% |

  Revoking on revert does most of the work: reverts cluster, and the first one
  takes the grant away. Per-agent scoping made no measurable difference in this
  sample; we keep it for accountability and don't claim it as a safety result.
  Revert counts are a lower bound (explicit reverts only).

## How it's built

- A GitHub App (Python, Starlette): signature-checked webhooks, each delivery
  handled once, check runs, sha-pinned merges, maintain-vs-write roles.
- The verification agent: **Nemotron 3 Super on Nebius Token Factory** plans and
  repairs; sandboxes run base, PR and revert from one snapshot. Nebius Token
  Factory Sandboxes weren't activated on our project before the deadline (see
  the feedback), so the live deployment runs the same interface on a private
  runner of our own with no permissions and no secrets. One setting switches
  back to Nebius Sandboxes.
- A daily token cap, kept across restarts: past it, the model isn't called and
  the PR is held, exactly as if Token Factory were down.
- Detonate: for PRs touching agent instructions or MCP configs, decodes
  invisible text and flags new directives and MCP commands. Tuned on the 3,438
  such PRs in AIDev until 93.8% of them came out clean.
- 226 tests, CI on Python 3.11 and 3.13.

## What isn't there yet

- Nebius Sandboxes on the live deployment (activation pending).
- Grants proposed automatically from a track record: today a person grants,
  and the replay's "5 clean merges" rule is simulated, not shipped.
- One repository live. The App installs per repository; nothing about it is
  hyperion-specific.
- The planner sees top-level folders only; a monorepo with deep packages may
  get the main suite instead of the right one.

## Feedback on Nebius and NVIDIA tools

The full write-up is in [NEBIUS_FEEDBACK.md](NEBIUS_FEEDBACK.md). In short: Token
Factory and Nemotron were quick to adopt and plan repositories well once
`reasoning_effort` bounds the reasoning, which otherwise used a whole 2,000-token
budget and returned no answer. Sandboxes were blocked by project activation
with no self-serve signal beyond a 403; the `whoami` permissions map is what
finally explained it.

---

## Before submitting

Delete this section once every box is ticked.

- [ ] Merge Tenure PR #11 so `main` is what's deployed.
- [ ] Record the video (under 3 minutes, YouTube, public) and put its link in the table above.
- [ ] Make the repository public; check the licence shows in the About section.
- [ ] Devpost: name the track (Coding and Agentic Engineering), paste the description, the demo URL and the feedback, and submit by Thu 29 Oct (hard deadline Fri 30 Oct, 22:30 IST).
- [ ] Keep both services running and free for judges until 15 Dec.
