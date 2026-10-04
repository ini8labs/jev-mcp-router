"""Client for TypeSafe AI's Jev "System One" decision model.

Jev does not generate text. It takes a state plus typed questions and answers
each one. We only use `noul` (yes/no) questions here: the API returns the
probability of "yes", and `decide()` turns that into a hard YES/NO.

Same request shape as ../JEV/internal/jev/client.go:
    POST {base}/v1/systemone  {model, state, questions}
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api"
DEFAULT_MODEL = "jev-latest"
INPUT_PRICE_PER_MTOK = 0.042  # TypeSafe list price; output tokens are free
YES_THRESHOLD = 0.5
MAX_QUESTIONS_PER_REQUEST = 40  # large catalogs are split into parallel requests


@dataclass
class JevResult:
    yes: dict[str, bool]                      # question key -> YES/NO
    p_yes: dict[str, float]                   # kept for --debug only
    input_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    requests: int = 0
    fake: bool = False
    errors: list[str] = field(default_factory=list)


class JevError(RuntimeError):
    pass


class Jev:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("TYPESAFE_API_KEY", "")
        self.base_url = (base_url or os.getenv("JEV_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("JEV_MODEL") or DEFAULT_MODEL
        if not self.api_key:
            raise JevError("no OPENROUTER_API_KEY / TYPESAFE_API_KEY set (use JEV_FAKE=1 to run offline)")

    async def decide(self, state: dict, questions: dict[str, str]) -> JevResult:
        """Ask one yes/no question per key. Returns a hard YES/NO per key."""
        keys = list(questions)
        chunks = [keys[i : i + MAX_QUESTIONS_PER_REQUEST] for i in range(0, len(keys), MAX_QUESTIONS_PER_REQUEST)]
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=30) as http:
            parts = await asyncio.gather(*(self._one(http, state, {k: questions[k] for k in c}) for c in chunks))
        out = JevResult(yes={}, p_yes={}, latency_s=time.perf_counter() - start, requests=len(chunks))
        for p_yes, tokens, cost in parts:
            out.p_yes.update(p_yes)
            out.input_tokens += tokens
            out.cost_usd += cost
        out.yes = {k: p >= YES_THRESHOLD for k, p in out.p_yes.items()}
        return out

    async def _one(self, http: httpx.AsyncClient, state: dict, questions: dict[str, str]):
        body = {
            "model": self.model,
            "state": state,
            "questions": {k: {"type": "noul", "instructions": q} for k, q in questions.items()},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(5):
            try:
                r = await http.post(f"{self.base_url}/v1/systemone", json=body, headers=headers)
            except httpx.TransportError as e:
                err: str | None = f"transport: {e}"
            else:
                if r.status_code < 300:
                    data = r.json()
                    p_yes = {k: float(a.get("noul") or 0.0) for k, a in data.get("answers", {}).items()}
                    usage = data.get("usage", {})
                    tokens = int(usage.get("input_tokens", 0))
                    cost = float(usage.get("cost") or tokens * INPUT_PRICE_PER_MTOK / 1e6)
                    return p_yes, tokens, cost
                err = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in (429, 500, 502, 503, 504, 529):
                    raise JevError(err)
            await asyncio.sleep(min(8, 0.5 * 2**attempt) * (1 + random.random() / 2))
        raise JevError(err or "jev: retries exhausted")


class FakeJev:
    """Offline stand-in: keyword overlap between the request and each question.

    Its decisions are NOT Jev's. It exists so the pipeline can run end to end
    without credits. Every result it produces is marked fake=True.
    """

    _STOP = set("the a an of for to in on and or is are be with by this that it its from at as user request tool must "
                "called answer handle what does get list show".split())

    async def decide(self, state: dict, questions: dict[str, str]) -> JevResult:
        req = _words(str(state.get("user_request", "")))
        p_yes = {}
        for k, q in questions.items():
            hits = len(req & _words(q))
            p_yes[k] = min(0.95, 0.15 * hits)
        tokens = sum(len(q) for q in questions.values()) // 4 + len(str(state)) // 4
        return JevResult(
            yes={k: p >= YES_THRESHOLD for k, p in p_yes.items()}, p_yes=p_yes, input_tokens=tokens,
            cost_usd=tokens * INPUT_PRICE_PER_MTOK / 1e6, latency_s=0.0, requests=1, fake=True,
        )


def _words(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if len(w) > 2 and w not in FakeJev._STOP:
            out.add(w.rstrip("s"))
    return out


def make_jev() -> Jev | FakeJev:
    return FakeJev() if os.getenv("JEV_FAKE") == "1" else Jev()
