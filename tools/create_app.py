"""
Create Tenure's GitHub App from deploy/github-app-manifest.json, in one click.

    python tools/create_app.py            # opens your browser

GitHub's manifest flow: this serves a page on 127.0.0.1 that posts the
manifest to GitHub. You review the App there and click "Create GitHub App";
GitHub sends your browser back here with a one-time code, which this exchanges
for the App's id, private key and webhook secret.

The credentials go to ~/.config/tenure/ (directory 0700, files 0600), never
into the repository and never to the terminal:

    ~/.config/tenure/private-key.pem
    ~/.config/tenure/app.env      # TENURE_APP_ID, TENURE_PRIVATE_KEY_PATH, TENURE_WEBHOOK_SECRET

The webhook starts at a placeholder address. Point it at a running Tenure with
tools/set_webhook.py.
"""

from __future__ import annotations

import html
import http.server
import json
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tenure.github import GitHubApp

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("TENURE_CONFIG_DIR", Path.home() / ".config" / "tenure"))
PORT = 8765
PLACEHOLDER_HOOK = "https://tenure-webhook.invalid/webhook"


def save(result: dict) -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    HOME.chmod(0o700)
    key = HOME / "private-key.pem"
    key.touch(mode=0o600)
    key.write_text(result["pem"])
    secret = result.get("webhook_secret") or secrets.token_hex(32)
    env = HOME / "app.env"
    env.touch(mode=0o600)
    env.write_text(f"export TENURE_APP_ID={result['id']}\n"
                   f"export TENURE_PRIVATE_KEY_PATH={key}\n"
                   f"export TENURE_WEBHOOK_SECRET={secret}\n")
    if not result.get("webhook_secret"):
        # GitHub didn't set one: set ours, so every delivery is signed.
        GitHubApp(str(result["id"]), result["pem"]).set_webhook(secret=secret)
    return env


def main() -> int:
    if (HOME / "app.env").exists():
        sys.exit(f"{HOME / 'app.env'} already exists; an App was created before. Remove it to create another.")
    manifest = json.loads((ROOT / "deploy" / "github-app-manifest.json").read_text())
    manifest["hook_attributes"]["url"] = PLACEHOLDER_HOOK
    manifest["redirect_url"] = f"http://127.0.0.1:{PORT}/created"
    state = secrets.token_urlsafe(16)
    done = threading.Event()
    outcome: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def reply(self, body: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<!doctype html><meta charset=utf-8><title>Tenure</title>"
                             f"<body style='font:16px system-ui;max-width:40em;margin:3em auto'>{body}".encode())

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/":
                self.reply(
                    "<h1>Create Tenure's GitHub App</h1><p>GitHub will show you the App's name, permissions and "
                    "events before anything is created.</p>"
                    f"<form method=post action='https://github.com/settings/apps/new?state={state}'>"
                    f"<input type=hidden name=manifest value='{html.escape(json.dumps(manifest), quote=True)}'>"
                    "<button style='font-size:1.1em;padding:.5em 1em'>Continue to GitHub</button></form>")
                return
            if url.path != "/created":
                self.reply("Not found", 404)
                return
            query = parse_qs(url.query)
            if query.get("state", [""])[0] != state or "code" not in query:
                self.reply("That response doesn't match this setup; nothing was saved.", 400)
                return
            r = httpx.post(f"https://api.github.com/app-manifests/{query['code'][0]}/conversions",
                           headers={"Accept": "application/vnd.github+json"}, timeout=30)
            if r.status_code != 201:
                outcome["error"] = f"GitHub answered {r.status_code} when exchanging the code"
                self.reply(outcome["error"], 502)
            else:
                result = r.json()
                outcome.update(id=result["id"], slug=result["slug"], env=save(result))
                self.reply(f"<h1>Created {html.escape(result['name'])}</h1><p>Credentials are saved on this "
                           f"machine. Next, install it on the repositories Tenure should govern (choose "
                           f"<b>Only select repositories</b>):</p><p><a href='https://github.com/apps/"
                           f"{html.escape(result['slug'])}/installations/new'>Install {html.escape(result['name'])}"
                           f"</a></p>")
            done.set()

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Open http://127.0.0.1:{PORT}/ if your browser doesn't open on its own. Waiting for GitHub…", flush=True)
    webbrowser.open(f"http://127.0.0.1:{PORT}/")
    done.wait()
    server.shutdown()
    if "error" in outcome:
        sys.exit(outcome["error"])
    print(f"Created App {outcome['id']} ({outcome['slug']}). Credentials: {outcome['env']} (not shown).")
    print(f"Install it: https://github.com/apps/{outcome['slug']}/installations/new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
