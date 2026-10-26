# Feedback on Nebius Token Factory, Nemotron and Sandboxes

Written while building Tenure for the Nebius × NVIDIA Global AI Hackathon,
September and October 2026. Everything here happened on this project; numbers
are from our own logs.

## What we used

| Tool | For |
|---|---|
| Token Factory, OpenAI-compatible chat API | every call Tenure makes to a model |
| `nvidia/nemotron-3-super-120b-a12b` | the verification agent's planner: reads a repository's build and CI files and writes a build-and-test recipe; repairs it when a step fails |
| Token Factory Sandboxes, through `contree-sdk` 0.3.6 | built against, as the place untrusted PR code runs; not usable on our project in time (below) |

## What worked well

- **The first call just worked.** The OpenAI-compatible endpoint
  (`https://api.tokenfactory.us-central1.nebius.com/v1/`) took a plain
  `httpx` POST and a bearer key. The first live call answered in 1.7 s.
- **`GET /v1/models`** listed exactly which Nemotron models the key could
  use (Ultra 550B, Super 120B, Nano 30B, Lightning). That settled the model ID
  without guessing from blog posts.
- **Nemotron 3 Super plans well once its reasoning is bounded.** Given a
  repository's file list and its `Cargo.toml` and CI workflow, it chose
  `rust:latest` and `cargo test --verbose`; given a change under `python/`,
  it chose `python:3.11`, a virtualenv and the right test folder, and repaired
  its own recipe from the error output when a step failed.
- **Prompt caching showed up in `usage`** (`prompt_cache_hit_tokens`), which
  matters for an agent that sends the same repository context on every repair.
- **The Sandboxes API's `GET /sandboxes/v1/whoami`** returned a permissions map
  (`import`, `spawn`, `list`, …). It turned an opaque 403 into a precise answer
  in one call. More APIs should have this.
- **`contree-sdk` reads `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID`**, the same
  variables the model calls use, so one pair of settings covers both.

## What needs work

### 1. Reasoning tokens can silently swallow the whole answer (Nemotron 3 Super)

- **What happened:** our first live planning call returned an empty `content`,
  `finish_reason: "length"`, and `usage.completion_tokens_details.reasoning_tokens: 2000`
  — the model spent the entire `max_tokens` budget reasoning and wrote no answer.
  Our code reported it as "the answer wasn't a JSON object", which sent us
  looking in the wrong place.
- **What fixed it:** `reasoning_effort: "low"`. On the same prompt:

  | Setting | Time | Tokens | Answer |
  |---|---|---|---|
  | default, `max_tokens` 2000 | — | 2,000, all reasoning | none |
  | `reasoning_effort: "low"` | 2 s | 95 | a correct recipe |
  | `max_tokens` 12000 | 27 s | 5,681 | the same recipe |
  | `chat_template_kwargs.enable_thinking: false` | 2 s | 68 | a recipe our validator refused (`rust` with no tag) |

- **Suggestions:** say on the model card that reasoning counts against
  `max_tokens` and that the default effort can use all of it; consider returning
  a distinct `finish_reason` (or a warning) when the budget ran out *before any
  content*; and document `reasoning_effort` and `enable_thinking` for Nemotron on
  Token Factory in one place. Severity: high for agents, which parse the answer.

### 2. Sandboxes weren't usable on our project, and there was no self-serve way to find out why

- **What happened:** with a working Token Factory key and project ID, every
  Sandboxes call failed. The SDK raised `ForbiddenError: You do not have
  permission to perform this action`; the raw API said
  `{"status": 403, "error": "Insufficient permissions: list"}`. `whoami` showed
  every permission `false`. The docs say a project may need Sandboxes
  activation, and to email contree@nebius.com.
- **Impact:** Sandboxes are where Tenure runs untrusted pull-request code, so
  without them every agent PR stays held. With days left, we built a private
  runner of our own behind the same interface, which cost a day and changed
  what we could say we used.
- **Suggestions:** a visible "Sandboxes: not activated — request access"
  state in the console, next to the API keys; hackathon credits that switch
  Sandboxes on for the project they're redeemed in; and an SDK error that says
  "Sandboxes are not activated for this project" instead of a generic 403.
  Severity: high (blocking).

### 3. Smaller things

- The ConTree SDK's image reference is `docker://docker.io/library/<name>`; a
  short note on which registries and tags are allowed would have saved a guess.
- Prices for Sandboxes only appear in the console. A price per CPU-second and
  per GB-second on the docs page would let an agent builder budget before
  signing up.

## Onboarding

Fast for models, slow for Sandboxes. From key to first Nemotron answer was a
few minutes. From key to knowing Sandboxes needed activation was most of a day,
through a 403, the SDK source, and the docs.

## Would we build with these again

Yes for Token Factory and Nemotron: the API is plain, the model plans
repositories well, and `reasoning_effort` makes it cheap and fast enough to sit
in a webhook's path. For Sandboxes, yes once they're switched on: branching
snapshots are exactly the right shape for testing a change, its base and its
revert from one checkpoint, and Tenure's code for them is written and waiting.
