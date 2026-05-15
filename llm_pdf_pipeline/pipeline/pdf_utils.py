"""PDF helpers: text extraction with page markers, sha256, page render."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Iterable

import pdfplumber

log = logging.getLogger(__name__)


def sha256_of(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_text_by_page(pdf_path: Path | str) -> list[str]:
    """Return a list of page texts (1-indexed externally; index 0 is page 1).

    Empty strings for pages where pdfplumber returned None — caller decides
    whether to escalate to vision.
    """
    pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            txt = page.extract_text() or ""
            pages.append(txt)
            # Log progress every 20 pages so the SSE stream shows the job is alive
            n = i + 1
            if n == 1 or n % 20 == 0 or n == total:
                log.info("PDF text pass: page %d / %d", n, total)
    return pages


# ---------------- word-cluster (row-aware) text extraction ----------------

def _cluster_words_into_rows(words, y_tol: float = 3.0):
    """Group pdfplumber words into visual rows by y-coordinate.

    Uses a running-bucket algorithm: a word joins the first row whose
    reference `top` is within `y_tol`, otherwise starts a new row.
    """
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        placed = False
        for r in rows:
            if abs(r[0]["top"] - w["top"]) <= y_tol:
                r.append(w)
                placed = True
                break
        if not placed:
            rows.append([w])
    for r in rows:
        r.sort(key=lambda w: w["x0"])
    return rows


def _row_to_line(row: list[dict], big_gap_pts: float = 8.0) -> str:
    """Join words of a row, inserting extra spaces where x-gap between
    consecutive words is wide (indicating a table column break)."""
    out_parts: list[str] = []
    prev_x1 = None
    for w in row:
        if prev_x1 is not None:
            gap = w["x0"] - prev_x1
            if gap > big_gap_pts:
                out_parts.append("   ")  # 3-space = column break marker
            else:
                out_parts.append(" ")
        out_parts.append(w["text"])
        prev_x1 = w["x1"]
    return "".join(out_parts).strip()


def extract_row_aware_text_by_page(pdf_path: Path | str) -> list[str]:
    """Return per-page text where each visual row is on its own line and wide
    horizontal gaps between words are flagged with 3-space indent.

    This preserves grid structure that `extract_text()` mangles when column
    headers span multiple vertical lines or when table cells are separated by
    whitespace (no borders). Used for Stage 2 per-section extraction so the
    LLM can reason over wide tables (PPE rollforward, segment reporting, etc.)
    without vision.
    """
    pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        log.info("Parsing PDF: %d pages (row-aware mode)", total)
        for i, page in enumerate(pdf.pages):
            words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=True,
                extra_attrs=[],
            )
            if not words:
                pages.append("")
            else:
                rows = _cluster_words_into_rows(words, y_tol=3.0)
                lines = [_row_to_line(r) for r in rows]
                pages.append("\n".join(l for l in lines if l))
            # Log progress every 15 pages so the SSE stream shows the job is alive
            n = i + 1
            if n % 15 == 0 or n == total:
                log.info("Parsed page %d / %d", n, total)
    return pages


def assemble_text_for_scout(pages: list[str], max_chars_per_page: int = 4000) -> str:
    """Concatenate pages with explicit page markers for the scout prompt.

    Truncates very long pages so we keep the prompt cheap. Scout only needs
    headings + first lines to locate sections, not full table content.
    """
    chunks = []
    for i, txt in enumerate(pages, start=1):
        snippet = txt[:max_chars_per_page]
        chunks.append(f"=== PAGE {i} ===\n{snippet}")
    return "\n\n".join(chunks)


def assemble_text_for_pages(pages: list[str], page_numbers: Iterable[int]) -> str:
    """Concatenate the full text of selected pages (1-indexed) for Stage 2."""
    chunks = []
    for p in page_numbers:
        if 1 <= p <= len(pages):
            chunks.append(f"=== PAGE {p} ===\n{pages[p - 1]}")
    return "\n\n".join(chunks)


# ---------------- primary-statement slicing ----------------

# Phrases that indicate the START of a particular primary statement.
# Used to drop any preceding content on the same page (e.g. auditor-report
# tail, TOC fragments) before sending to the LLM.
_STATEMENT_START_ANCHORS: dict[str, tuple[str, ...]] = {
    "balance_sheet": (
        "statement of financial position",
        "balance sheet",
    ),
    "income_statement": (
        "statement of profit or loss",
        "statement of profit and loss",
        "statement of income",
        "income statement",
    ),
    "comprehensive_income": (
        "statement of comprehensive income",
        "statement of profit or loss and other comprehensive income",
    ),
    "cash_flow": (
        "statement of cash flows",
        "cash flow statement",
    ),
    "equity_changes": (
        "statement of changes in equity",
        "statement of changes in shareholders",
    ),
}

# Phrases that indicate the START of the narrative notes block.
# When one of these is found AFTER the statement anchor, we truncate the
# section text there so the LLM does not drift into accounting-policy prose.
_NOTES_START_ANCHORS: tuple[str, ...] = (
    "notes to the financial statements",
    "notes to the consolidated financial statements",
    "significant accounting policies",
    "summary of significant accounting policies",
    "material accounting policy information",
    "basis of preparation",
    "1. corporate information",
    "1 corporate information",
    "1. general information",
    "1 general information",
    "1. reporting entity",
)


def trim_primary_statement_text(text: str, kind: str) -> str:
    """Trim per-section text for a primary-statement kind so it covers
    only the statement itself, dropping any trailing narrative-note content
    that happens to share the same PDF page.

    Returns the input unchanged if no anchors are found or ``kind`` is not
    a primary-statement kind.  Matching is case-insensitive.
    """
    starts = _STATEMENT_START_ANCHORS.get(kind)
    if not starts:
        return text
    low = text.lower()
    # Find earliest start anchor (so we drop preceding content).
    start_idx = -1
    for anchor in starts:
        i = low.find(anchor)
        if i != -1 and (start_idx == -1 or i < start_idx):
            start_idx = i
    if start_idx == -1:
        # No explicit title anchor — keep full text (scout found this page
        # for a reason; don't risk dropping everything).
        start_idx = 0
    # Find earliest notes-start anchor AFTER the statement start.
    end_idx = len(text)
    for anchor in _NOTES_START_ANCHORS:
        i = low.find(anchor, start_idx + 1)
        if i != -1 and i < end_idx:
            end_idx = i
    return text[start_idx:end_idx].rstrip()


def page_count(pdf_path: Path | str) -> int:
    with pdfplumber.open(str(pdf_path)) as pdf:
        return len(pdf.pages)
