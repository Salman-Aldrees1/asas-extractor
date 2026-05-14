"""Fallback batched LLM mapping for rows where Pass 1/2 returned std_code=null.

Cached on disk by (parent_caption, line_item) so repeated runs don't re-spend
tokens. Populates std_code in-place; rows still missing after this go to
__unmapped.json.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .coa import CoA, load_coa
from .llm_client import LLMClient


log = logging.getLogger(__name__)


SYSTEM = """You map company-specific financial-statement captions to Standard CoA codes.

You receive a list of caption-with-context items and the CoA list. For each
item return ONE code from the CoA `code` column that best matches, or null if
nothing fits. Output strict JSON: {"results":[{"id":N, "std_code":"..."}, ...]}.
Use null for std_code when no good match exists.
"""


def map_unmapped(items: list[dict], *, coa: CoA | None = None,
                 client: LLMClient, cache_path: Path | None = None,
                 batch_size: int = 50) -> dict[tuple[str, str], str | None]:
    """items: [{"id": str, "parent_caption": str, "line_item": str, "context": str}].
    Returns dict {(parent_caption, line_item): std_code or None}.
    """
    coa = coa or load_coa()
    cache: dict[str, str | None] = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    def _key(it: dict) -> str:
        return f"{it['parent_caption']}||{it['line_item']}"

    results: dict[tuple[str, str], str | None] = {}
    todo: list[dict] = []
    for it in items:
        k = _key(it)
        if k in cache:
            results[(it["parent_caption"], it["line_item"])] = cache[k]
        else:
            todo.append(it)

    if not todo:
        return results

    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        user = (
            "STANDARD CHART OF ACCOUNTS:\n"
            f"{coa.render_for_prompt()}\n\n"
            "ITEMS TO MAP (return std_code from the CoA list verbatim or null):\n"
            f"{json.dumps([{ 'id': j, **{k: v for k, v in it.items() if k != 'id'}} for j, it in enumerate(batch)], ensure_ascii=False)}\n"
        )
        try:
            raw = client.call_json(
                label="coa_map",
                system=SYSTEM,
                user=user,
                max_tokens=4000,
            )
        except Exception as e:
            log.warning("coa map batch failed: %s", e)
            continue
        for r in raw.get("results", []):
            try:
                idx = int(r["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= idx < len(batch)):
                continue
            code = r.get("std_code")
            if code and code not in coa:
                code = None
            it = batch[idx]
            results[(it["parent_caption"], it["line_item"])] = code
            cache[_key(it)] = code

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False),
                              encoding="utf-8")

    return results
