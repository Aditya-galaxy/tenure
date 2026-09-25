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

1. **A public URL for the webhook.** For development, a tunnel to
   `http://127.0.0.1:8080` works (for example `cloudflared tunnel --url
   http://127.0.0.1:8080`). Put `<that URL>/webhook` into `hook_attributes.url`.
2. **Create the App** from the manifest at *Settings → Developer settings →
   GitHub Apps → New GitHub App*, copying the permissions and events above. The
   manifest flow can also do it in one step.
3. **Set a webhook secret** in the App settings, and generate a **private key**.
4. **Install it** on the repositories to govern (start with hyperion-quant).
5. **Run Tenure:**

   ```bash
   export TENURE_APP_ID=<app id>
   export TENURE_PRIVATE_KEY_PATH=/path/to/private-key.pem   # never commit it
   export TENURE_WEBHOOK_SECRET=<the secret from step 3>
   python -m tenure.app                                      # listens on 127.0.0.1:8080
   ```

6. **Let Tenure merge past the review rule, and nothing else.** On the
   repository's `main`, create two rulesets:
   - **Ruleset A:** requires a pull request with 1 approval. *Bypass list:* the Tenure App.
   - **Ruleset B:** requires the CI status check. *No bypass*, so even Tenure's merges need green CI.

The private key and the webhook secret are credentials: keep them out of the
repository and out of chat.
