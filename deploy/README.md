# Creating the GitHub App

Tenure acts as a GitHub App on the repositories it's installed on. The
manifest in this folder asks for the least that the code uses:

| Permission | Why |
|---|---|
| Contents: write | merging a pull request needs it |
| Pull requests: write | reading PR files, commenting |
| Checks: write | the Tenure check run on each agent PR |
| Actions: read, Metadata: read | seeing build results on the default branch |

Events: `pull_request`, `issue_comment`, `workflow_run`, `check_suite`, which
are exactly the ones `tenure/webhook.py` handles.

## Steps

1. **Create the App.** One command opens your browser on GitHub's own
   review page, with the name, permissions and events above filled in from
   the manifest. Nothing exists until you click **Create GitHub App** there.

   ```bash
   python tools/create_app.py
   ```

   GitHub sends your browser back to this machine, and the App's id, private
   key and webhook secret are saved to `~/.config/tenure/` (owner-only), never
   to the repository or the terminal. If the name "Tenure" is taken, change it
   on GitHub's page before creating; nothing else depends on the name.
2. **Install it** on the repositories to govern, from the link the last page
   gives you. Choose **Only select repositories** and start with hyperion-quant.
3. **Run Tenure:**

   ```bash
   source ~/.config/tenure/app.env
   python -m tenure.app                                      # listens on 127.0.0.1:8080
   ```

4. **Point the webhook at it.** The App is created with a placeholder address
   that goes nowhere. For development, open a tunnel to `http://127.0.0.1:8080`
   (for example `cloudflared tunnel --url http://127.0.0.1:8080`), then:

   ```bash
   source ~/.config/tenure/app.env
   python tools/set_webhook.py https://<the tunnel's host>/webhook
   ```

   Run it again with the deployed address once Tenure is on Nebius.
5. **Let Tenure merge past the review rule, and nothing else.** On the
   repository's `main`, create two rulesets:
   - **Ruleset A:** requires a pull request with 1 approval. *Bypass list:* the Tenure App.
   - **Ruleset B:** requires the CI status check. *No bypass*, so even Tenure's merges need green CI.

The private key and the webhook secret are credentials: keep them out of the
repository and out of chat.
