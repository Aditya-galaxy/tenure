"""
The language model, through Nebius Token Factory's OpenAI-compatible API.

Tenure asks a model to do two things: work out how to build and test a
repository, and write a short brief for the person reviewing a held PR. It
never asks a model whether to merge. Every answer is parsed and validated by
the caller, and any failure (no key, a timeout, output that doesn't parse)
raises, which the caller turns into "not verified" and a held PR.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx

ENDPOINT = "https://api.tokenfactory.us-central1.nebius.com/v1/"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
# Nemotron 3 Super reasons before it answers, and its reasoning counts against
# max_tokens. Left at the default it spent a whole 2,000-token budget thinking
# about one repository and never wrote the answer. Asked for low effort, it
# planned the same repository in 95 tokens and 2 seconds (24 Oct 2026).
REASONING_EFFORT = os.environ.get("TENURE_REASONING_EFFORT", "low")


class ModelError(RuntimeError):
    pass


class Model(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int = 4000) -> str: ...


class DailyBudget:
    """A hard cap on the tokens Tenure may spend in a day. Anyone can open a
    PR on a public repository, and every verified PR costs model tokens out of
    a fixed credit; past the cap the model isn't called and the PR is held,
    the same way it is when Token Factory is down. Kept in a file, so a
    restart doesn't reset it."""

    def __init__(self, path: Path, tokens_per_day: int) -> None:
        self.path, self.cap = path, tokens_per_day
        self._lock = threading.Lock()

    def _today(self) -> tuple[str, int]:
        today = date.today().isoformat()
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        return today, int(data.get(today, 0))

    def check(self) -> None:
        _, used = self._today()
        if used >= self.cap:
            raise ModelError(f"today's model budget ({self.cap:,} tokens) is used up")

    def spend(self, tokens: int) -> None:
        with self._lock:
            today, used = self._today()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({today: used + max(0, tokens)}))


class TokenFactory:
    def __init__(self, api_key: str, model: str = SUPER, base_url: str = ENDPOINT,
                 client: httpx.Client | None = None, budget: DailyBudget | None = None) -> None:
        self.model = model
        self.budget = budget
        self.http = client or httpx.Client(base_url=base_url, timeout=120,
                                           headers={"Authorization": f"Bearer {api_key}"})

    @classmethod
    def from_env(cls) -> TokenFactory:
        key = os.environ.get("NEBIUS_API_KEY")
        if not key:
            raise ModelError("NEBIUS_API_KEY isn't set")
        state = Path(os.environ.get("TENURE_STATE_DIR", "state"))
        cap = int(os.environ.get("TENURE_DAILY_TOKENS", "300000"))
        budget = DailyBudget(state / "model-budget.json", cap)
        return cls(key, model=os.environ.get("TENURE_MODEL", SUPER), budget=budget)

    def complete(self, system: str, user: str, *, max_tokens: int = 4000) -> str:
        if self.budget:
            self.budget.check()
        try:
            r = self.http.post("chat/completions", json={
                "model": self.model, "max_tokens": max_tokens, "temperature": 0,
                "reasoning_effort": REASONING_EFFORT,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        except httpx.HTTPError as exc:
            raise ModelError(f"Token Factory unreachable: {exc}") from exc
        if r.status_code != 200:
            raise ModelError(f"Token Factory answered {r.status_code}: {r.text[:200]}")
        if self.budget:
            try:
                self.budget.spend(int(r.json().get("usage", {}).get("total_tokens", max_tokens)))
            except (ValueError, TypeError, AttributeError):
                self.budget.spend(max_tokens)              # can't tell: count the most it could have been
        try:
            choice = r.json()["choices"][0]
            content = choice["message"]["content"] or ""
            if not content and choice.get("finish_reason") == "length":
                # Out of tokens before answering: say so, not "not JSON".
                raise ModelError("the model ran out of tokens before answering")
            return content
        except (KeyError, IndexError, ValueError) as exc:
            raise ModelError("Token Factory answer had no message") from exc


def json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model's answer. Reasoning models may think aloud
    before answering or wrap the answer in a code fence; the last object wins."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if not candidates and "{" in text:
        candidates = [text[text.find("{"):text.rfind("}") + 1]]
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ModelError("the answer wasn't a JSON object")
