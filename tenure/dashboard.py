"""
The page a visitor sees: what Tenure has decided, live, and nothing it holds.

    GET /            this page
    GET /api/state   the same, as JSON

Read-only. It shows the grants in force, the merges Tenure made, and the audit
chain (recent entries and whether the whole chain still verifies). Everything
on it is already public on the repositories Tenure governs; no key, token or
secret is ever read here.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

APP_URL = "https://github.com/apps/tenure-merge"
CODE_URL = "https://github.com/Aditya-galaxy/tenure"
RECENT = 40


def state(engine: Any, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    ok, broken_at = engine.audit.verify()
    records = engine.audit.records()
    grants = [{"repo": g.repo, "agent": g.agent, "class": g.change_class, "owner": g.owner,
               "granted_by": g.granted_by, "reason": g.reason, "granted_at": g.granted_at.isoformat(),
               "expires_at": g.expires_at.isoformat(), "suspended": g.suspended,
               "expired": g.expires_at <= now} for g in engine.store.all()]
    merges = [{"repo": m.repo, "pr": m.pr, "title": m.title, "verified_commit": m.head_sha,
               "merge_commit": m.merge_sha, "agent": m.agent, "class": m.change_class, "at": m.at}
              for m in engine.ledger.all()]
    return {"audit": {"verifies": ok, "broken_at": broken_at, "entries": len(records),
                      "head": engine.audit.head},
            "grants": grants, "merges": merges,
            "recent": [{"ts": r.get("ts"), "kind": r.get("kind"), "payload": r.get("payload")}
                       for r in records[-RECENT:]][::-1]}


def _pr_link(repo: str | None, pr: Any) -> str:
    if not repo or pr in (None, ""):
        return ""
    return f'<a href="https://github.com/{html.escape(repo)}/pull/{int(pr)}">{html.escape(repo)}#{int(pr)}</a>'


def _describe(kind: str, p: dict[str, Any]) -> str:
    """One line a person can read, for each kind of audit entry."""
    e = html.escape
    repo = p.get("repo") or (p.get("scope") or [None])[0]
    if kind == "decision":
        verdict = {"auto_merge": "merge", "hold": "hold for a person", "never": "never merge unattended"}.get(
            p.get("verdict", ""), p.get("verdict", ""))
        reasons = "; ".join(p.get("reasons") or [])
        return (f"{_pr_link(repo, p.get('pr'))}: <b>{e(verdict)}</b> · {e(p.get('agent', ''))}, "
                f"{e(p.get('class', ''))} · {e(reasons)}")
    if kind in ("grant", "renew"):
        _, agent, cls = (p.get("scope") or [None, p.get("agent", ""), p.get("change_class", "")])[:3]
        until = str(p.get("expires_at") or "")[:10]
        return (f"{'Granted' if kind == 'grant' else 'Renewed'}: <b>{e(agent)}</b> may merge <b>{e(cls)}</b> "
                f"in {e(repo or '')} until {e(until)}, by @{e(p.get('granted_by') or p.get('by', ''))}, "
                f"owner @{e(p.get('owner', ''))}: {e(p.get('reason', ''))}")
    if kind == "revoke":
        _, agent, cls = (p.get("scope") or ["", "", ""])[:3]
        return f"<b>Revoked</b> {e(agent)}'s {e(cls)} grant in {e(repo or '')}: {e(p.get('reason', ''))}"
    if kind == "merged":
        return f"<b>Merged</b> {_pr_link(repo, p.get('pr'))} at the verified commit {e(str(p.get('head', ''))[:7])}"
    if kind == "merge_refused":
        return f"GitHub refused the merge of {_pr_link(repo, p.get('pr'))}: {e(p.get('why', ''))}"
    if kind == "fired":
        return f"A grant was used for {e(str(p.get('pr', '')))}"
    if kind in ("halt", "resume"):
        return f"<b>{e(kind.title())}</b> in {e(repo or '')} by @{e(p.get('by', ''))}: {e(p.get('reason', ''))}"
    if kind == "refused":
        return f"Refused a command: {e(str(p.get('why') or p.get('reason') or p)[:200])}"
    return e(str(p)[:240])


def page(engine: Any) -> str:
    s = state(engine)
    e = html.escape
    audit = s["audit"]
    chain = ("verifies end to end" if audit["verifies"]
             else f"BROKEN at entry {audit['broken_at']}")
    grants = "".join(
        f"<tr><td>{e(g['repo'])}</td><td>{e(g['agent'])}</td><td>{e(g['class'])}</td>"
        f"<td>@{e(g['owner'])}</td><td>{e(g['expires_at'][:10])}</td>"
        f"<td>{'expired' if g['expired'] else ('suspended: ' + e(g['suspended'])) if g['suspended'] else 'in force'}"
        f"</td></tr>" for g in s["grants"]) or '<tr><td colspan="6">None in force: every agent PR waits for a person.</td></tr>'
    merges = "".join(
        f"<tr><td>{_pr_link(m['repo'], m['pr'])}</td><td>{e(m['title'])}</td><td>{e(m['agent'])} · {e(m['class'])}</td>"
        f"<td><code>{e(m['verified_commit'][:7])}</code></td><td>{e(m['at'][:16].replace('T', ' '))}</td></tr>"
        for m in s["merges"]) or '<tr><td colspan="5">None yet.</td></tr>'
    recent = "".join(
        f"<li><time>{e((r['ts'] or '')[:16].replace('T', ' '))}</time> {_describe(r['kind'] or '', r['payload'] or {})}</li>"
        for r in s["recent"]) or "<li>Nothing yet.</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tenure: who may merge what</title>
<style>
 :root {{ --bg:#0f1720; --panel:#16212d; --ink:#e8eef4; --soft:#9fb0c0; --accent:#7fd1c7; --bad:#ff8f8f; }}
 * {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--ink);
   font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif }}
 main {{ max-width:1040px; margin:0 auto; padding:28px 16px 60px }}
 h1 {{ font-size:28px; margin:0 0 6px }} h2 {{ font-size:18px; margin:28px 0 10px }}
 p.lead {{ color:var(--soft); margin:0 0 18px; max-width:760px }}
 a {{ color:var(--accent) }} code {{ font-size:13px }}
 .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px }}
 .tile {{ background:var(--panel); border-radius:10px; padding:14px 16px }}
 .tile b {{ display:block; font-size:22px }} .tile span {{ color:var(--soft); font-size:13px }}
 .bad {{ color:var(--bad) }}
 table {{ width:100%; border-collapse:collapse; background:var(--panel); border-radius:10px; overflow:hidden }}
 th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #22303e; vertical-align:top }}
 th {{ color:var(--soft); font-weight:600; font-size:13px }}
 .scroll {{ overflow-x:auto }}
 ul.log {{ list-style:none; padding:0; margin:0; background:var(--panel); border-radius:10px }}
 ul.log li {{ padding:8px 12px; border-bottom:1px solid #22303e }} time {{ color:var(--soft); margin-right:8px }}
</style></head><body><main>
<h1>Tenure</h1>
<p class="lead">A GitHub App that decides which pull requests written by agents may merge without a person.
Agent PRs are held until a maintainer grants that agent one kind of change, for a while, with an owner.
Under a grant, Tenure proves the PR in a sandbox (the tests pass, and the change reverts cleanly) before
merging the exact commit it checked. A revert takes the grant away. The model plans the proof; it never
decides a merge. This page is live and read-only.</p>
<div class="tiles">
 <div class="tile"><b class="{'' if audit['verifies'] else 'bad'}">{e(chain)}</b><span>audit chain, {audit['entries']} entries, head <code>{e(audit['head'][:16])}</code></span></div>
 <div class="tile"><b>{len([g for g in s['grants'] if not g['expired']])}</b><span>grants in force</span></div>
 <div class="tile"><b>{len(s['merges'])}</b><span>merges Tenure made</span></div>
</div>
<h2>Grants in force</h2>
<div class="scroll"><table><tr><th>Repository</th><th>Agent</th><th>Change</th><th>Owner</th><th>Until</th><th>State</th></tr>{grants}</table></div>
<h2>Merges Tenure made</h2>
<div class="scroll"><table><tr><th>PR</th><th>Title</th><th>Agent · change</th><th>Verified commit</th><th>When (UTC)</th></tr>{merges}</table></div>
<h2>Recent decisions and events</h2>
<ul class="log">{recent}</ul>
<p class="lead" style="margin-top:22px">Code: <a href="{CODE_URL}">{CODE_URL}</a> · The App:
<a href="{APP_URL}">{APP_URL}</a> · The same data as JSON: <a href="/api/state">/api/state</a></p>
</main></body></html>"""
