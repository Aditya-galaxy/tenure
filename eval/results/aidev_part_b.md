309 repositories, 34,891 merged agent PRs (7 without file rows left out; 1 reverted only after 30 days, counted as not reverted).

### All repositories

| Policy | merged unattended | later reverted among them | leak rate | reverted PRs a person saw first |
|---|---:|---:|---:|---:|
| merge when green | 34,891 (100.0%) | 212 | 0.61% | 0 of 212 (0%) |
| path rules only | 31,058 (89.0%) | 187 | 0.60% | 25 of 212 (12%) |
| Tenure: n=5, docs/tests/patch bumps, per agent | 5,366 (15.4%) | 1 | 0.02% | 211 of 212 (100%) |
| Tenure: n=5, same, trust pooled across agents | 5,389 (15.4%) | 1 | 0.02% | 211 of 212 (100%) |
| Tenure: n=3, docs/tests/patch bumps, per agent | 5,522 (15.8%) | 2 | 0.04% | 210 of 212 (99%) |
| Tenure: n=10, docs/tests/patch bumps, per agent | 5,183 (14.9%) | 0 | 0.00% | 212 of 212 (100%) |
| Tenure: n=5, code too (needs the verifier), per agent | 20,268 (58.1%) | 33 | 0.16% | 179 of 212 (84%) |
| Tenure: n=5, code too, trust pooled across agents | 20,744 (59.5%) | 34 | 0.16% | 178 of 212 (84%) |
| never auto-merge | 0 (0.0%) | 0 | n/a | 212 of 212 (100%) |

### Without the two largest (mochilang/mochi, MontrealAI/AGI-Alpha-Agent-v0: 15,476 PRs)

| Policy | merged unattended | later reverted among them | leak rate | reverted PRs a person saw first |
|---|---:|---:|---:|---:|
| merge when green | 19,415 (100.0%) | 207 | 1.07% | 0 of 207 (0%) |
| path rules only | 16,210 (83.5%) | 182 | 1.12% | 25 of 207 (12%) |
| Tenure: n=5, docs/tests/patch bumps, per agent | 700 (3.6%) | 1 | 0.14% | 206 of 207 (100%) |
| Tenure: n=5, same, trust pooled across agents | 723 (3.7%) | 1 | 0.14% | 206 of 207 (100%) |
| Tenure: n=3, docs/tests/patch bumps, per agent | 844 (4.3%) | 2 | 0.24% | 205 of 207 (99%) |
| Tenure: n=10, docs/tests/patch bumps, per agent | 550 (2.8%) | 0 | 0.00% | 207 of 207 (100%) |
| Tenure: n=5, code too (needs the verifier), per agent | 7,858 (40.5%) | 32 | 0.41% | 175 of 207 (85%) |
| Tenure: n=5, code too, trust pooled across agents | 8,334 (42.9%) | 33 | 0.40% | 174 of 207 (84%) |
| never auto-merge | 0 (0.0%) | 0 | n/a | 207 of 207 (100%) |

### Revert rates in this sample

| agent | merged | reverted within 30 days |
|---|---:|---:|
| OpenAI_Codex | 25,036 | 153 (0.61%) |
| Copilot | 5,924 | 27 (0.46%) |
| Devin | 2,249 | 21 (0.93%) |
| Cursor | 1,215 | 10 (0.82%) |
| Claude_Code | 378 | 1 (0.26%) |
| Google_Jules | 89 | 0 (0.00%) |

| change class | merged | reverted within 30 days |
|---|---:|---:|
| code | 24,137 | 183 (0.76%) |
| tests | 4,462 | 0 (0.00%) |
| never | 3,833 | 25 (0.65%) |
| docs | 2,370 | 3 (0.13%) |
| deps | 66 | 1 (1.52%) |
| deps_patch | 23 | 0 (0.00%) |
