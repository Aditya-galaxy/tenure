# Evaluation on real agent pull requests

## Part A: what coding agents change (`aidev_classes.py`)

Data: [AIDev](https://huggingface.co/datasets/hao-li/AIDev) v4 (Li, Zhang and
Hassan), which covers 71,677 pull requests opened by six coding agents in GitHub
repositories with more than 100 stars, including every commit's file-level
changes. Of these, 69,654 PRs have file rows. Each PR is classified by Tenure's
deterministic classifier under the default policy.

```
python eval/aidev_classes.py        # needs data/aidev/*.parquet, ~5 min
```

Results are in [`results/aidev_part_a.md`](results/aidev_part_a.md), with raw counts
and merged-only counts in `results/aidev_part_a.json`.

| Agent | PRs | docs | tests | deps (patch) | deps | code | never | touch CI | touch agent instructions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Codex | 35,504 | 5.9% | 13.7% | 0.0% | 0.2% | 73.1% | 7.1% | 4.9% | 2.38% |
| Copilot | 21,537 | 6.7% | 1.3% | 0.2% | 0.5% | 67.4% | 24.1% | 16.6% | 8.96% |
| Devin | 6,160 | 7.6% | 1.2% | 0.3% | 0.6% | 76.7% | 13.5% | 11.2% | 2.19% |
| Cursor | 4,054 | 7.7% | 1.3% | 0.1% | 0.5% | 78.0% | 12.4% | 8.4% | 4.61% |
| Claude Code | 1,913 | 3.8% | 1.0% | 0.2% | 0.2% | 66.7% | 28.2% | 14.2% | 16.88% |
| Jules | 486 | 8.6% | 1.0% | 0.2% | 0.6% | 71.0% | 18.5% | 13.8% | 3.50% |
| **all** | **69,654** | 6.4% | 7.6% | 0.1% | 0.3% | 71.7% | 13.9% | 9.6% | 4.94% |

**What it shows**

- **Agents differ, so one rule for all of them is wrong.** 28% of Claude
  Code's PRs and 24% of Copilot's touch a path Tenure never merges unattended
  (CI config, agent instructions, MCP configs, licences, the policy file). For Codex
  the figure is 7%. Codex opens far more test-only PRs (13.7%) than any other
  agent (about 1%). Grants scoped to one agent and one class are what fit that
  spread.
- **The classes that are easy to earn are real but small.** Docs, test-only and
  patch-level dependency changes make up about 14% of all agent PRs. That is
  the share Tenure could merge without a person once an agent has a track
  record. Code changes are 72% and stay with people until the verification
  agent has proved a change and a person has granted the class.
- **Agents edit their own instructions and CI.** 4.9% of agent PRs (3,438)
  change an agent instruction file or an MCP config. For Claude Code it is
  16.9%. 9.6% touch CI config. These are the changes that shape what the next
  agent run does, which is why Tenure never merges them unattended.

**Detonate on the 3,438 PRs that edit agent instructions**

| Most severe finding | PRs |
|---|---:|
| none | 3,226 (93.8%) |
| info: pipes a downloaded script into a shell (mostly official installers such as rustup and uv) | 88 |
| medium: a directive, or a diff the dataset left out | 50 |
| high | 74 |

Of the 74 "high" PRs, 54 add an MCP server, which is a program the agent will
launch; the check reports the command. 19 flag secrets handling, mostly
Claude Code permission rules like `"Read(**/.env.production)"`. From one
added line, Detonate can't tell whether a rule like that is in an allow list
or a deny list. That is the largest remaining source of noise. The last PR
sends data to an outside host.

**No PR in the dataset hides text.** No tag-character text was found, and no
bidi or zero-width character was used inside English words. We looked for the
Rules File Backdoor pattern and did not find it, so this evaluation measures
Detonate's **noise on ordinary edits**, not its detection rate. The first
version of the rules raised 8,406 findings on this data. Most of them were
lines like "Secrets should not be committed", links to conventionalcommits.org,
Persian text that needs zero-width non-joiners, and emoji joined by zero-width
joiners. The rules now ask for an instruction to *act*, skip negated guidance,
ignore loopback and documentation hosts, and accept zero-width characters where
a script or emoji needs them. Every false positive we fixed is now a regression
test in `tests/test_detonate.py`. Detection is covered by the attack-shaped
tests in the same file; the sandbox half (`compare()`) is the check that
doesn't depend on wording.

**Caveats**

- The dataset has no previous filename for renames, so a file renamed out of a
  sensitive path is judged by its new name only.
- A PR's files are the union across its commits. A file added and then removed
  counts by its last status.
- AIDev omits patches for large diffs. Where a manifest's diff is missing,
  the change can't be shown to be only a version bump, so it isn't counted as
  one. That is the stricter reading, and it may undercount patch bumps.
- The classes come from paths and diffs alone. Part A says nothing about
  whether these PRs were *good*. Part B, which replays merges and later reverts
  per agent, is where outcomes come in.

## Part B: replaying real merges and real reverts (`aidev_replay.py`)

```
GH_TOKEN=$(gh auth token) python eval/aidev_reverts.py   # ~45 min, cached under data/aidev/github/
python eval/aidev_replay.py                               # seconds
```

**What this measures.** The sample is every repository with at least 20 merged agent PRs: 314 repos, of which
309 are still reachable, holding 34,891 merged agent PRs. For each of them, `aidev_reverts.py` asks GitHub which merged agent PRs were
later reverted. It checks both revert PRs ("Reverts owner/repo#N") and `git revert` commits
("This reverts commit …"), and ties each revert back to its PR. That finds **212 PRs reverted within
30 days of merging**.

`aidev_replay.py` then replays every merge in time order under each policy in
[`replay.py`](replay.py). A policy learns about a revert only when the revert
happens, so it can't use next month's revert to hold a PR today. Full tables, including per-agent and per-class revert rates, are in
[`results/aidev_part_b.md`](results/aidev_part_b.md).

| Policy | merged without a person | later reverted among them | leak rate | reverted PRs a person saw first |
|---|---:|---:|---:|---:|
| merge when green | 34,891 (100%) | 212 | 0.61% | 0% |
| path rules only (Tenure's never-auto list, no earned trust) | 31,058 (89.0%) | 187 | 0.60% | 12% |
| **Tenure**: docs, tests and patch bumps; 5 clean merges a week old earn a grant | **5,366 (15.4%)** | **1** | **0.02%** | **>99%** |
| Tenure, same, n=10 | 5,183 (14.9%) | 0 | 0.00% | 100% |
| Tenure, code grantable too (the verifier's job; see below) | 20,268 (58.1%) | 33 | 0.16% | 84% |
| never auto-merge | 0 | 0 | n/a | 100% |

Two repositories hold 44% of these PRs (mochilang/mochi and MontrealAI/AGI-Alpha-Agent-v0). Without them:
merge-when-green leaks 1.07%. Tenure's conservative grants merge 3.6% without a
person with one leak (0.14%). With code grantable, Tenure merges 40.5% at a 0.41% leak rate.

**What it shows**

- **Static rules don't find the risky PRs.** The never-auto list sends 11% of merges to a person but
  catches only 12% of the PRs that were later reverted. Reverts land in ordinary code, which is exactly
  what a path list can't judge.
- **Earned grants are safe where they apply.** Across 5,366 docs, test and patch-bump PRs merged under a grant, one was later reverted:
  a Devin edit to CONTRIBUTING.md, reverted two minutes after it merged. Not one of the 4,462 test-only
  agent PRs in the sample was reverted.
- **Revoking on revert does most of the work.** With code grantable and no verification at all, grants
  plus revocation merge 58% of PRs without a person and still show a person 84% of the PRs that were later
  reverted. The leak rate is a quarter of merge-when-green's. This works because reverts cluster: the first revert in a key takes the grant
  away, and the PRs that follow wait for a person while the key earns its record back.
  This row is an upper bound on grants alone. In the product, a code grant
  also needs the verification agent's sandbox proof for every PR.
- **Per-agent scoping made no measurable difference here.** Trust pooled across agents gives almost
  the same result (throughput within 2%, at most one more leak), even though 85 of the 309 repos use more than one agent.
  Revert rates differ across agents in this sample (Claude Code 0.26%, Copilot 0.46%, Codex 0.61%,
  Cursor 0.82%, Devin 0.93%). The replay still doesn't show that per-agent keys prevent leaks. We keep them for
  accountability, because a grant should name what it trusts. We don't claim them as a safety result.

**Caveats**

- **Revert counts are a lower bound.** Only explicit reverts are found. Fixes that quietly undo a change,
  partial reverts made by hand, and repos that became unreachable (5) are missed. Our rates (0.3–0.9% per
  agent) are far below the 6–15% reported by Kraishan (arXiv 2609.17598), whose definition covers broader
  post-merge maintenance. Compare the policies with each other, not with that paper.
- **A revert isn't always a defect.** dotnet/aspire#10334, for example, was reverted inside a dependency
  upgrade that superseded it.
- **There is no CI signal in the data.** Every merged PR is treated as green and as passing verification, so this
  replay tests the grant-and-revocation layer alone.
- **Only merged PRs are replayed.** PRs people closed without merging aren't in it. Merge-when-green would have
  merged the green ones among them, so its real leak is likely higher than shown.
- Maintainers are assumed to accept every grant proposal as soon as it's made, and grant expiry isn't
  modelled.
