"""tools/create_app.py keeps the App's credentials private."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import create_app


def test_credentials_are_saved_owner_only_and_a_missing_secret_is_set(tmp_path, monkeypatch):
    set_to = {}
    monkeypatch.setattr(create_app, "HOME", tmp_path / "cfg")
    monkeypatch.setattr(create_app.GitHubApp, "set_webhook", lambda self, **kw: set_to.update(kw))
    env = create_app.save({"id": 42, "pem": "-----BEGIN KEY-----", "webhook_secret": None})
    for path in (env, tmp_path / "cfg" / "private-key.pem"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "cfg").stat().st_mode) == 0o700
    text = env.read_text()
    assert "TENURE_APP_ID=42" in text and f"TENURE_WEBHOOK_SECRET={set_to['secret']}" in text
    assert len(set_to["secret"]) == 64
