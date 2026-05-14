"""LLM Pass 1: extract face-statement line items + period labels + std codes.

One LLM call. Input: the full PDF text (row-aware) + the CoA list.
Output: PrimaryExtraction.
"""
from __future__ import annotations

import json
import logging

from .coa import CoA, load_coa
from .llm_client import LLMClient
from .schemas import PrimaryExtraction, PrimaryRow


log = logging.getLogger(__name__)


SYSTEM = """You are an expert financial-statement extractor for Tadawul-listed Saudi companies.

You receive the full text of an audited annual report PDF (row-aware: visual rows preserved, wide column gaps marked with extra whitespace).

Your job (Pass 1 of 2): extract every line item that appears on the FACE of the four primary statements:
  - Balance Sheet (a.k.a. Statement of Financial Position)
  - Income Statement (a.k.a. Statement of Profit or Loss / Statement of Comprehensive Income — capture both PL and OCI lines)
  - Cash Flow Statement
  - Statement of Changes in Equity (capture each movement step as a row; details handled in Pass 2)

You DO NOT extract note breakdowns / details here. Only what is printed on the face of the statement.

For each line item you must:
  1. Capture the company-specific caption verbatim.
  2. Capture the section heading printed on the face (e.g. "Non-Current Assets", "Operating activities", "Operating Expenses").
  3. Capture the note reference if printed (e.g. "5", "6.1", "32(a)"). null if absent.
  4. Capture both period values as plain numbers in the source's reporting unit.
       - Negatives in parentheses → negative number.
       - "-" or "–" → 0.
       - For Income Statement and Cash Flow rows, expenses must be NEGATIVE numbers.
       - For Balance Sheet rows, use natural sign as printed.
  5. Map the caption to ONE Standard CoA code from the taxonomy provided. If no good match, use null.

Also identify and return at the top level:
  - company name (legal name)
  - currency (typically "SAR")
  - units_multiplier: 1 if amounts are in actual riyals, 1000 if "in thousands", 1000000 if "in millions"
  - period_current_label, period_prior_label: short labels like "2025" / "2024" or "FY2025" / "FY2024"
  - approval_date: free text e.g. "3 February 2026" if present, else ""

Capture subtotals AND totals as their own rows (Total Non-Current Assets, Total Assets, Gross Profit, Operating Profit, Net Profit, Total Equity, etc.) — they have CoA codes too.

Equity (Statement of Changes in Equity) Pass 1 rule: capture only the closing balance per equity component (one row each), with std_code = the corresponding BS-EQ-* code. Movement rollforward goes to Pass 2.

Cash Flow Pass 1 rule: capture every line printed on the face, including reconciliation lines (Profit before tax, Operating before working capital changes, Cash flows generated from operating activities, etc.).

Output schema (strict JSON):

{
  "company": "...",
  "currency": "SAR",
  "units_multiplier": 1,
  "period_current_label": "2025",
  "period_prior_label": "2024",
  "approval_date": "3 February 2026",
  "balance_sheet": [
    {"section":"Non-Current Assets","caption":"Property, Plant and Equipment","note":"5","value_current":140417645,"value_prior":141245869,"std_code":"BS-NCA-PPE"}
  ],
  "income_statement": [...],
  "cash_flow": [...],
  "equity": [...]
}

Rules:
  - Preserve source order within each statement.
  - Do not invent line items.
  - If a line is printed without a number for one period, use null for that period.
  - std_code MUST be a code from the CoA list verbatim, or null.
"""


def run_primary(*, pdf_text: str, coa: CoA | None = None, client: LLMClient,
                max_chars: int = 220_000) -> PrimaryExtraction:
    coa = coa or load_coa()
    text = pdf_text
    if len(text) > max_chars:
        log.warning("primary: pdf text %d chars > limit %d, truncating",
                    len(text), max_chars)
        text = text[:max_chars]

    user = (
        "STANDARD CHART OF ACCOUNTS (use codes from the `code` column verbatim, or null):\n"
        f"{coa.render_for_prompt()}\n\n"
        "PDF TEXT:\n"
        f"{text}\n"
    )

    raw = client.call_json(
        label="primary",
        system=SYSTEM,
        user=user,
        max_tokens=20000,
    )
    return PrimaryExtraction.model_validate(raw)
