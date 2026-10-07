"""
Point Tenure's GitHub App webhook at a URL: a tunnel while developing, the
Nebius Serverless endpoint once deployed.

    source ~/.config/tenure/app.env
    python tools/set_webhook.py https://<host>/webhook
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tenure.github import GitHubApp


def main() -> int:
    url = sys.argv[1]
    if not url.startswith("https://") or not url.rstrip("/").endswith("/webhook"):
        sys.exit("the URL must be https:// and end in /webhook")
    app = GitHubApp(os.environ["TENURE_APP_ID"], Path(os.environ["TENURE_PRIVATE_KEY_PATH"]).read_text())
    app.set_webhook(url=url)
    print(f"webhook → {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
