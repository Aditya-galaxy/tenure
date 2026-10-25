"""
The webhook endpoint.

    TENURE_APP_ID=… TENURE_PRIVATE_KEY_PATH=… TENURE_WEBHOOK_SECRET=… python -m tenure.app

It refuses to start without a webhook secret: an endpoint that acts on
unverified payloads would let anyone post "this PR is from Dependabot".
Deliveries are acknowledged at once (GitHub gives up after ten seconds) and
handled in the background.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .audit import AuditLog
from .engine import Engine
from .github import GitHubApp
from .grants import GrantStore
from .halt import HaltSwitch
from .ledger import Ledger
from .verify import Unverified, Verifier
from .webhook import Deliveries, actions, signature_ok

log = logging.getLogger("tenure")


def create_app(engine: Engine, secret: bytes) -> Starlette:
    if not secret:
        raise RuntimeError("TENURE_WEBHOOK_SECRET is required: without it anyone can forge a webhook")
    deliveries = Deliveries()

    def run(todo: list) -> None:
        for action in todo:
            try:
                engine.handle(action)
            except Exception:
                log.exception("handling %s failed", type(action).__name__)
                engine.audit.record("error", {"action": type(action).__name__, "repr": repr(action)[:500]})

    async def webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if not signature_ok(secret, body, request.headers.get("X-Hub-Signature-256")):
            return JSONResponse({"error": "bad signature"}, status_code=401)
        if not deliveries.first_time(request.headers.get("X-GitHub-Delivery")):
            return JSONResponse({"status": "duplicate"}, status_code=200)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "not JSON"}, status_code=400)
        todo = actions(request.headers.get("X-GitHub-Event", ""), payload)
        return JSONResponse({"status": "accepted", "actions": len(todo)}, status_code=202,
                            background=BackgroundTask(run, todo))

    async def healthz(_: Request) -> JSONResponse:
        ok, broken = engine.audit.verify()
        return JSONResponse({"ok": ok, "audit_broken_at": broken})

    # /health as well: some hosts' front ends keep /healthz for themselves.
    return Starlette(routes=[Route("/webhook", webhook, methods=["POST"]), Route("/healthz", healthz),
                             Route("/health", healthz)])


def from_env() -> Starlette:
    state = Path(os.environ.get("TENURE_STATE_DIR", "state"))
    audit = AuditLog(state / "audit.jsonl")
    gh = GitHubApp(os.environ["TENURE_APP_ID"], Path(os.environ["TENURE_PRIVATE_KEY_PATH"]).read_text())
    engine = Engine(gh.installation, store=GrantStore(state / "grants.json", audit),
                    switch=HaltSwitch(state / "halts.json", audit), ledger=Ledger(state / "merges.jsonl", audit),
                    audit=audit, verifier=_verifier())
    return create_app(engine, os.environ.get("TENURE_WEBHOOK_SECRET", "").encode())


def _verifier() -> Verifier:
    """The verification agent when Nebius credentials are present; otherwise
    nothing is verified, so every agent PR is held."""
    if not (os.environ.get("NEBIUS_API_KEY") and os.environ.get("NEBIUS_PROJECT_ID")):
        return Unverified()
    from .agent import VerificationAgent
    from .model import TokenFactory
    from .sandbox import ContreeSandbox
    return VerificationAgent(ContreeSandbox(), TokenFactory.from_env())


def main() -> None:
    import uvicorn

    uvicorn.run(from_env(), host=os.environ.get("TENURE_HOST", "127.0.0.1"),
                port=int(os.environ.get("TENURE_PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
