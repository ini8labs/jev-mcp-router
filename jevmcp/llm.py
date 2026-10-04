"""Chat LLM via OpenRouter (OpenAI-compatible), plus an offline fake.

In the Jev path the LLM only fills arguments for the tools Jev said YES to and
writes the final answer. In the baseline path it sees every tool, which is the
normal MCP setup we are comparing against.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import httpx

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
# USD per million tokens (input, output); refreshed from OpenRouter when reachable.
FALLBACK_PRICES = {"anthropic/claude-sonnet-5": (2.0, 10.0), "anthropic/claude-haiku-4.5": (1.0, 5.0),
                   "anthropic/claude-opus-5.5": (4.0, 20.0), "openai/gpt-5-mini": (0.25, 2.0)}
_price_cache: dict[str, tuple[float, float]] = {}


def price(model: str) -> tuple[float, float]:
    if not _price_cache:
        try:
            for m in httpx.get("https://openrouter.ai/api/v1/models", timeout=10).json()["data"]:
                p = m.get("pricing", {})
                _price_cache[m["id"]] = (float(p.get("prompt", 0)) * 1e6, float(p.get("completion", 0)) * 1e6)
        except Exception:
            _price_cache.update(FALLBACK_PRICES)
    return _price_cache.get(model) or FALLBACK_PRICES.get(model, (2.0, 10.0))


def estimate_tokens(obj) -> int:
    """~4 characters per token. Used only when the provider does not report usage."""
    return max(1, len(obj if isinstance(obj, str) else json.dumps(obj)) // 4)


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict]           # [{id, name, arguments: dict}]
    raw_message: dict
    input_tokens: int
    output_tokens: int
    cost_usd: float
    estimated: bool = False


@dataclass
class LLM:
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", DEFAULT_MODEL))
    api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))

    async def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        body = {"model": self.model, "messages": messages, "max_tokens": 2000, "usage": {"include": True}}
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.post("https://openrouter.ai/api/v1/chat/completions", json=body,
                                headers={"Authorization": f"Bearer {self.api_key}"})
        if r.status_code >= 300:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        pin, pout = price(self.model)
        tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        calls = [{"id": c["id"], "name": c["function"]["name"],
                  "arguments": json.loads(c["function"].get("arguments") or "{}")}
                 for c in msg.get("tool_calls") or []]
        return ChatResult(msg.get("content") or "", calls, msg, tin, tout,
                          float(usage.get("cost") or (tin * pin + tout * pout) / 1e6))


@dataclass
class FakeLLM:
    """Offline stand-in. Token counts are estimated from the real request payload,
    so the cost comparison is still meaningful; the tool picks and answers are not."""

    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", DEFAULT_MODEL))

    async def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        request = next(m["content"] for m in messages if m["role"] == "user")
        already_called = any(m["role"] == "tool" for m in messages)
        calls = []
        if tools and not already_called:
            words = set(re.findall(r"[a-z]{4,}", request.lower()))
            for t in tools:
                f = t["function"]
                overlap = len(words & set(re.findall(r"[a-z]{4,}", f["description"].lower())))
                if len(tools) <= 4 or overlap >= 2:  # few tools offered = Jev already chose them
                    calls.append({"id": f"call_{len(calls)}", "name": f["name"], "arguments": _fake_args(f, request)})
            calls = calls[:4]
        content = "" if calls else f"[offline fake answer] Based on {sum(m['role'] == 'tool' for m in messages)} tool results."
        msg = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                 for c in calls]
        tin = estimate_tokens({"messages": messages, "tools": tools})
        tout = estimate_tokens(msg)
        pin, pout = price(self.model)
        return ChatResult(content, calls, msg, tin, tout, (tin * pin + tout * pout) / 1e6, estimated=True)


def _fake_args(fn: dict, request: str) -> dict:
    svc = next((s for s in ("checkout", "payments", "cart", "search", "auth") if s in request.lower()), "checkout")
    is_prom = fn["name"].startswith("prometheus")
    guesses = {
        "query": f'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{{service="{svc}"}}[5m]))'
        if is_prom else f"_time:1h service:{svc} level:error",
        "start": "now-1h", "end": "now", "step": "1m", "time": "now", "limit": 20,
        "metric": "http_request_duration_seconds", "field": "level", "field_name": "level",
    }
    props = fn["parameters"].get("properties", {})
    return {k: guesses[k] for k in fn["parameters"].get("required", []) if k in guesses and k in props}


def make_llm() -> LLM | FakeLLM:
    return FakeLLM() if os.getenv("LLM_FAKE") == "1" else LLM()
