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


class TokenFactory:
    def __init__(self, api_key: str, model: str = SUPER, base_url: str = ENDPOINT,
                 client: httpx.Client | None = None) -> None:
        self.model = model
        self.http = client or httpx.Client(base_url=base_url, timeout=120,
                                           headers={"Authorization": f"Bearer {api_key}"})

    @classmethod
    def from_env(cls) -> TokenFactory:
        key = os.environ.get("NEBIUS_API_KEY")
        if not key:
            raise ModelError("NEBIUS_API_KEY isn't set")
        return cls(key, model=os.environ.get("TENURE_MODEL", SUPER))

    def complete(self, system: str, user: str, *, max_tokens: int = 4000) -> str:
        try:
            r = self.http.post("chat/completions", json={
                "model": self.model, "max_tokens": max_tokens, "temperature": 0,
                "reasoning_effort": REASONING_EFFORT,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        except httpx.HTTPError as exc:
            raise ModelError(f"Token Factory unreachable: {exc}") from exc
        if r.status_code != 200:
            raise ModelError(f"Token Factory answered {r.status_code}: {r.text[:200]}")
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
