"""End-to-end runner for v2: PDF in → xlsx + raw json + unmapped + cost."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import builder
from .coa import load_coa
from .coa_mapper import map_unmapped
from .extract_notes import run_notes
from .extract_primary import run_primary
from .llm_client import CostLedger, LLMClient
from .pdf_utils import (
    extract_row_aware_text_by_page,
    extract_text_by_page,
    sha256_of,
)
from .schemas import NotesExtraction, PrimaryExtraction
from .xlsx_writer import write_xlsx


log = logging.getLogger(__name__)


def _assemble(pages: list[str]) -> str:
    return "\n\n".join(f"=== PAGE {i + 1} ===\n{p}" for i, p in enumerate(pages))


def extract_pdf(pdf_path: Path | str, output_dir: Path | str) -> dict:
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base = pdf_path.stem

    log.info("hashing %s", pdf_path)
    sha = sha256_of(pdf_path)

    log.info("extracting page text (row-aware)")
    pages_row = extract_row_aware_text_by_page(pdf_path)
    # Also build a default-text version for fallback (some PDFs have flow
    # better than row-aware). We send row-aware to the LLM by default.
    pages_default = extract_text_by_page(pdf_path)
    text_row = _assemble(pages_row)
    text_default = _assemble(pages_default)
    log.info("PDF: %d pages, row-aware text=%d chars, default text=%d chars",
             len(pages_row), len(text_row), len(text_default))

    coa = load_coa()
    ledger = CostLedger()
    client = LLMClient(ledger=ledger)

    # ---- Pass 1 ----
    primary_path = output_dir / f"{base}__primary.json"
    if primary_path.exists():
        log.info("Pass 1: loading cached %s (delete to force re-run)", primary_path.name)
        primary = PrimaryExtraction.model_validate_json(primary_path.read_text(encoding="utf-8"))
    else:
        log.info("Pass 1: primary statements")
        primary = run_primary(pdf_text=text_row, coa=coa, client=client)
        primary_path.write_text(primary.model_dump_json(indent=2), encoding="utf-8")
    log.info("primary: company=%r currency=%s units=%s periods=%s/%s",
             primary.company, primary.currency, primary.units_multiplier,
             primary.period_current_label, primary.period_prior_label)
    log.info("primary rows: BS=%d IS=%d CF=%d Eq=%d",
             len(primary.balance_sheet), len(primary.income_statement),
             len(primary.cash_flow), len(primary.equity))

    # ---- Pass 2 ----
    notes_path = output_dir / f"{base}__notes.json"
    if notes_path.exists():
        log.info("Pass 2: loading cached %s (delete to force re-run)", notes_path.name)
        notes = NotesExtraction.model_validate_json(notes_path.read_text(encoding="utf-8"))
    else:
        log.info("Pass 2: notes")
        notes = run_notes(pdf_text=text_row, primary=primary, coa=coa, client=client)
        notes_path.write_text(notes.model_dump_json(indent=2), encoding="utf-8")
    total_note_rows = sum(len(s.rows) for n in notes.notes for s in n.sections)
    log.info("notes: %d notes, %d row entries", len(notes.notes), total_note_rows)

    # ---- Build flat rows ----
    rows = builder.build(primary, notes, coa=coa)
    log.info("master rows: %d", len(rows))

    # ---- Optional fallback CoA mapping ----
    # Skip axis rows: they deliberately have empty std_item_code because
    # they describe a dimension (Additions, Jan 1, 1% increase, etc.) rather
    # than a metric, and assigning them a CoA code would cause collisions
    # with their parent row on ingestion.
    unmapped = builder.collect_unmapped(rows, include_axis=False)
    if unmapped:
        log.info("Pass 3: fallback CoA mapping for %d rows", len(unmapped))
        items = [
            {"id": str(i),
             "parent_caption": u["parent_caption"],
             "line_item": u["line_item"],
             "context": f"{u['parent_statement']} / note {u['note_number']} / {u['note_section']}"}
            for i, u in enumerate(unmapped)
        ]
        cache_path = output_dir / "coa_cache.json"
        result = map_unmapped(items, coa=coa, client=client, cache_path=cache_path)
        # Apply back to rows — never touch axis rows.
        applied = 0
        for r in rows:
            if r.std_item_code:
                continue
            if builder._is_axis(r.line_item):
                continue
            code = result.get((r.parent_caption, r.line_item))
            if code:
                # Pass 3 might hand out a code that is already used as a
                # primary or as another row's code. In that case derive a
                # unique "<code>-<slug>" variant (same policy as the builder).
                if any(x.std_item_code == code for x in rows):
                    slug = builder._slug(r.line_item)
                    code_base = code
                    candidate = f"{code_base}-{slug}" if slug else code_base
                    n = 2
                    existing = {x.std_item_code for x in rows if x.std_item_code}
                    while candidate in existing:
                        candidate = f"{code_base}-{slug}-{n}" if slug else f"{code_base}-{n}"
                        n += 1
                    if r.cross_reference:
                        r.cross_reference = f"{r.cross_reference}, {code}"
                    else:
                        r.cross_reference = code
                    code = candidate
                r.std_item_code = code
                r.std_item_name = coa.name_for(code) or coa.name_for(r.std_parent_code)
                applied += 1
        log.info("Pass 3: applied %d codes", applied)

    # ---- Sanity checks + unmapped report ----
    warnings = builder.sanity_checks(rows)
    final_unmapped = builder.collect_unmapped(rows, include_axis=False)
    (output_dir / f"{base}__unmapped.json").write_text(
        json.dumps({
            "sanity_warnings": warnings,
            "unmapped_rows": final_unmapped,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if warnings:
        for w in warnings:
            log.warning("SANITY: %s", w)

    # ---- Raw combined json (debug) ----
    (output_dir / f"{base}__raw.json").write_text(
        json.dumps({
            "pdf_filename": pdf_path.name,
            "pdf_sha256": sha,
            "primary": primary.model_dump(),
            "notes": notes.model_dump(),
            "master_rows": [r.model_dump() for r in rows],
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ---- xlsx ----
    xlsx_path = output_dir / f"{base}__data.xlsx"
    write_xlsx(
        xlsx_path,
        rows=rows,
        company=primary.company or pdf_path.stem,
        period_current=primary.period_current_label or "Current",
        period_prior=primary.period_prior_label or "Prior",
        currency=primary.currency or "SAR",
        approval_date=primary.approval_date or "",
        units_multiplier=primary.units_multiplier or 1,
        coa=coa,
    )
    log.info("xlsx written: %s", xlsx_path)

    # ---- Cost ----
    cost = {
        "tokens_in": ledger.tokens_in,
        "tokens_out": ledger.tokens_out,
        "cost_usd": round(ledger.cost_usd, 4),
        "calls": ledger.calls,
    }
    (output_dir / f"{base}__cost.json").write_text(
        json.dumps(cost, indent=2), encoding="utf-8"
    )
    log.info("done: $%0.4f, %d rows, %d sanity warnings, %d unmapped",
             ledger.cost_usd, len(rows), len(warnings), len(final_unmapped))
    return {
        "pdf": str(pdf_path),
        "xlsx": str(xlsx_path),
        "company": primary.company or pdf_path.stem,
        "period_current": primary.period_current_label or "Current",
        "period_prior": primary.period_prior_label or "Prior",
        "rows": len(rows),
        "sanity_warnings": len(warnings),
        "sanity_warning_details": warnings,
        "unmapped_rows": len(final_unmapped),
        "unmapped_row_details": final_unmapped,
        "cost_usd": round(ledger.cost_usd, 4),
        "tokens_in": ledger.tokens_in,
        "tokens_out": ledger.tokens_out,
    }
