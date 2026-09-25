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
