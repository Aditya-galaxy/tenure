"""
Which agent wrote a pull request — decided by something nobody can type.

A PR's author is identified by GitHub's numeric user ID, and only when GitHub
reports the account as a Bot. Never by login, branch name or commit trailer:

  * logins are inconsistent — Copilot's is plain `Copilot`, with no `[bot]`;
  * branch prefixes are unreliable — Codex opened a PR with none at all;
  * and all three are text a person with write access can type. Trusting one
    would let that person borrow an agent's earned right to skip review.

App bot accounts cannot be impersonated, so their IDs are the one signal that
holds. The IDs below were read from real PRs through the GitHub API on
2026-09-25. An account not listed is not an agent here, and Tenure does not
govern its PRs.
"""

from __future__ import annotations

KNOWN_AGENTS: dict[int, str] = {
    198982749: "copilot",        # login "Copilot"
    209825114: "claude-code",    # claude[bot]
    199175422: "codex",          # chatgpt-codex-connector[bot]
    206951365: "cursor",         # cursor[bot]
    158243242: "devin",          # devin-ai-integration[bot]
    161369871: "jules",          # google-labs-jules[bot]
    49699333: "dependabot",      # dependabot[bot]
    29139614: "renovate",        # renovate[bot]
}


def agent_of(author_id: int, author_type: str,
             extra: dict[int, str] | None = None) -> str | None:
    """The agent's name, or None when this is not an agent's PR.

    `extra` lets a deployment add bots by numeric ID (a self-hosted Renovate,
    say). It cannot rename a known agent: an ID already in KNOWN_AGENTS keeps
    its name, so configuration can't make one agent inherit another's grants.
    """
    if author_type != "Bot":
        return None
    if author_id in KNOWN_AGENTS:
        return KNOWN_AGENTS[author_id]
    return (extra or {}).get(author_id)
