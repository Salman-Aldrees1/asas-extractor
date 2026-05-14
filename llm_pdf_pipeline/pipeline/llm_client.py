"""Thin wrapper around Anthropic Messages API with JSON extraction + cost tracking.

Keeps the tiny surface area `call_json(label, system, user)` that the rest of
the pipeline depends on, so swapping providers later is a one-file change.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic


# Claude Opus 4.5 — top-tier extractor, used for the reference deliverable.
# Override via LLM_MODEL env (e.g. claude-haiku-4-5-20251001 for cheap iteration).
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "claude-opus-4-5")
PRICE_IN_PER_MTOK = float(os.environ.get("LLM_PRICE_IN_PER_MTOK", "15.00"))
PRICE_OUT_PER_MTOK = float(os.environ.get("LLM_PRICE_OUT_PER_MTOK", "75.00"))


@dataclass
class CostLedger:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    calls: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, label: str, tokens_in: int, tokens_out: int) -> None:
        cost = (tokens_in * PRICE_IN_PER_MTOK + tokens_out * PRICE_OUT_PER_MTOK) / 1_000_000
        with self._lock:
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            self.cost_usd += cost
            self.calls.append({
                "label": label, "tokens_in": tokens_in, "tokens_out": tokens_out,
                "cost_usd": round(cost, 5),
            })


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", s, re.DOTALL)
    return m.group(1) if m else s


def _extract_json_object(s: str) -> str:
    """Return the first balanced `{...}` JSON object found in s.

    Claude sometimes prefixes the JSON with brief narration even when asked
    for strict JSON. Find the first { and balance braces, respecting strings.
    """
    start = s.find("{")
    if start < 0:
        return s  # let json.loads raise
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]


class LLMClient:
    def __init__(self, ledger: CostLedger | None = None, model: str = DEFAULT_MODEL):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it to .env or export it. "
                "See llm_pdf_pipeline/.env.example."
            )
        self._client = Anthropic(api_key=api_key)
        self._model = model
        self.ledger = ledger or CostLedger()

    def call_json(
        self,
        *,
        label: str,
        system: str,
        user: str,
        max_tokens: int = 8000,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Send a single prompt; expect a JSON object back.

        Anthropic doesn't have a first-class JSON mode, so we instruct
        strictly and then parse tolerantly (strip code fences, isolate the
        first balanced `{...}`).
        """
        system_strict = (
            system.rstrip()
            + "\n\nReturn ONLY a single JSON object. Do not wrap in code fences. "
              "Do not prepend narration. Your entire response must be valid JSON."
        )
        # Anthropic requires streaming for max_tokens above ~21k. Use it
        # unconditionally — `messages.stream` is a context manager that
        # accumulates the same `Message` object on completion.
        with self._client.messages.stream(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_strict,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            final = stream.get_final_message()
        parts = [
            getattr(b, "text", "") for b in final.content
            if getattr(b, "type", None) == "text"
        ]
        text = "".join(parts)
        usage = final.usage
        self.ledger.add(label, usage.input_tokens, usage.output_tokens)

        cleaned = _strip_code_fence(text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # fall back: isolate first {...}
            candidate = _extract_json_object(cleaned)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"LLM returned non-JSON for label={label!r}: {e}\n"
                    f"--- raw response (first 1000 chars) ---\n{text[:1000]}"
                ) from e
