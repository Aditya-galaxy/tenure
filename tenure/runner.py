"""
The runner: where a pull request's code is built and tested, away from Tenure.

    python -m tenure.runner            # port 8080; deployed as its own service

It is LocalSandbox behind HTTP, deployed as a separate service whose identity
has no permissions and which is given no secrets: no GitHub key, no model key.
Only Tenure's service may call it. Untrusted code runs here and nowhere else.

The container comes with fixed toolchains (Rust, Python, git), so an image a
plan asks for is honoured by family: "rust:1.80" runs with the Rust installed
here, and a family it doesn't have is refused, which holds the PR.

One run at a time; states are directory snapshots under /tmp, and the oldest
are dropped so a long-running instance doesn't fill its memory.
"""

from __future__ import annotations

import base64
import os
import shutil
import threading

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .sandbox import LocalSandbox

FAMILIES = tuple(os.environ.get("TENURE_RUNNER_FAMILIES", "rust,python,debian").split(","))
KEEP_STATES = int(os.environ.get("TENURE_RUNNER_KEEP", "8"))
PASS_THROUGH = ("RUSTUP_HOME", "LANG")    # where the toolchain lives; CARGO_HOME stays per snapshot


def create_app(sandbox: LocalSandbox | None = None) -> Starlette:
    env = {k: v for k, v in os.environ.items() if k in PASS_THROUGH}
    box = sandbox or LocalSandbox(env=env)
    lock = threading.Lock()
    order: list[str] = []

    def prune() -> None:
        """Keep the newest snapshots; an image state is cheap and kept."""
        while len(order) > KEEP_STATES:
            old = order.pop(0)
            path = box.states.pop(old, None)
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)

    async def image(request: Request) -> JSONResponse:
        name = str((await request.json()).get("name", ""))
        if name.split(":", 1)[0] not in FAMILIES:
            return JSONResponse({"error": f"this runner provides {', '.join(FAMILIES)}, not {name!r}"},
                                status_code=422)
        return JSONResponse({"state": box.image(name)})

    async def run(request: Request) -> JSONResponse:
        body = await request.json()
        state = str(body.get("state", ""))
        if state not in box.states:
            return JSONResponse({"error": "unknown state (the runner restarted?)"}, status_code=410)
        files = {k: base64.b64decode(v) for k, v in (body.get("files") or {}).items()}
        timeout = min(int(body.get("timeout", 600)), 3500)

        def go():
            with lock:
                step = box.run(state, str(body.get("shell", "")), files=files, timeout=timeout)
                order.append(step.state)
                prune()
                return step
        step = await run_in_threadpool(go)
        return JSONResponse({"state": step.state, "exit_code": step.exit_code,
                             "stdout": step.stdout[-200_000:], "stderr": step.stderr[-200_000:]})

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "families": list(FAMILIES), "states": len(box.states)})

    return Starlette(routes=[Route("/image", image, methods=["POST"]), Route("/run", run, methods=["POST"]),
                             Route("/health", health)])


def main() -> None:
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("TENURE_HOST", "0.0.0.0"),  # noqa: S104 -- in its own container
                port=int(os.environ.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
