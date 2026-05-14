"""Merge Pass-1 and Pass-2 outputs into a flat list of MasterRows
matching the xlsx schema in the example file.

Also performs lightweight post-build sanity checks (no LLM).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable

from .coa import CoA, load_coa
from .schemas import (
    MasterRow,
    NotesExtraction,
    PrimaryExtraction,
    PrimaryRow,
)


log = logging.getLogger(__name__)


# Map our PrimaryExtraction field names to the xlsx Parent Statement label.
_STATEMENT_LABELS = {
    "balance_sheet": "Balance Sheet",
    "income_statement": "Income Statement",
    "cash_flow": "Cash Flow Statement",
    "equity": "Statement of Changes in Equity",
}


# ---- Axis / movement / maturity / ageing row detection -------------------
#
# Axis rows describe a DIMENSION (opening balance, additions, maturity bucket,
# sensitivity ±1%, ageing bucket, net-debt rollforward line) rather than a
# standalone metric. They must NOT carry a std_item_code because several axis
# rows of the same parent would otherwise collide on one DB key.

_AXIS_PATTERNS = [
    # opening / closing balances, rollforward dates
    r"^(as at|at)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b",
    r"^(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d+,?(\s*\d{4})?$",
    r"^\d+\s+(january|february|march|april|may|june|july|august|september|october|november|december)(\s+\d{4})?$",
    r"^opening\s+balance", r"^closing\s+balance",
    r"^balance\s+at\s+",
    r"^net\s+debt\s+(at|as\s+at)\b",
    r"^at\s+\d+\s+(january|december)",
    # movement lines
    r"^additions?(\s*,?\s*net)?$", r"^additions?\s*[-–]\s+",
    r"^disposals?$", r"^disposals?\s*[-–]\s+",
    r"^transfers?(\s+from\s+cwip)?$",
    r"^charge\s+for\s+the\s+year",
    r"^modifications?$", r"^modifications?\s*[-–]\s+",
    r"^terminations?$", r"^terminations?\s*[-–]\s+",
    r"^payments?$", r"^payments?\s*[-–]\s+",
    r"^benefits\s+paid$",
    r"^interest\s+payments?\s*[-–]?",
    r"^interest\s+cost\b",
    r"^current\s+service\s+cost$",
    r"^remeasurement\b",
    r"^financing\s+cash\s+flows?\s*[-–]\s+",
    r"^acquisition\s+cost$", r"^accumulated\s+depreciation$",
    r"^net\s+book\s+value$", r"^carrying\s+amount$",
    # maturity buckets
    r"^less\s+than\s+(1|one)\s+(year|months?)",
    r"^over\s+\d+\s+years?",
    r"^\d+\s*[-–]\s*\d+\s*(years?|days|months)\b",
    r"^\+\s*\d+\s*days?",
    r"^not\s+due$",
    r"^total\s*\(less\s+than\s+\d+\s+year",
    r"^total\s*\(\d+\s+to\s+\d+\s+years?",
    r"^total\s*\(over\s+\d+\s+years?",
    # sensitivity ±%
    r"^[+\-]?\s*\d+(\.\d+)?%\s+(increase|decrease)\b",
    r"^\d+%\s+(increase|decrease)\b",
    # ECL ageing matrix columns
    r"^gross\s+carrying\s+amount$",
    r"^loss\s+allowance$",
    r"\s+-\s+gross\s+carrying\s+amount$",
    r"\s+-\s+loss\s+allowance$",
    r"^specific\s+provision\s+-\s+",
    # generic total/subtotal-only rows (no own metric)
    r"^total$",
    r"^subtotal$",
]
_AXIS_RE = re.compile("|".join(_AXIS_PATTERNS), re.IGNORECASE)


def _is_axis(line_item: str) -> bool:
    if not line_item:
        return False
    s = line_item.strip().lower()
    return bool(_AXIS_RE.search(s))


def _slug(s: str) -> str:
    """Lowercase ASCII slug for building unique std_item_code suffixes."""
    if not s:
        return ""
    norm = unicodedata.normalize("NFKD", s)
    norm = norm.encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    # Compact very long slugs.
    if len(norm) > 48:
        norm = norm[:48].rstrip("-")
    return norm or "x"


def _row_from_primary(stmt_label: str, p: PrimaryRow, coa: CoA) -> MasterRow:
    code = p.std_code or ""
    name = coa.name_for(code)
    return MasterRow(
        parent_statement=stmt_label,
        parent_section=p.section or "",
        parent_caption=p.caption,
        std_parent_code=code,
        std_parent_name=name,
        note_number=p.note or "",
        note_section="",
        note_sub_section="",
        line_item=p.caption,
        std_item_code=code,
        std_item_name=name,
        cross_reference="",
        row_type="Primary",
        value_current=p.value_current,
        value_prior=p.value_prior,
    )


def _primary_lookup(primary: PrimaryExtraction) -> dict[str, PrimaryRow]:
    """caption (lower) → PrimaryRow, for resolving parent_face anchors when
    the LLM gives the caption but not the code."""
    out: dict[str, PrimaryRow] = {}
    for field in _STATEMENT_LABELS:
        for r in getattr(primary, field):
            out.setdefault(r.caption.strip().lower(), r)
    return out


def build(primary: PrimaryExtraction, notes: NotesExtraction,
          *, coa: CoA | None = None) -> list[MasterRow]:
    coa = coa or load_coa()
    rows: list[MasterRow] = []

    # For uniqueness of std_item_code within the file we track which codes
    # are already taken by any row. Primary rows that share a CoA code (e.g.
    # two line items mapped to BS-CL-OTH, or recurring / non-recurring G&A)
    # get a suffixed code "<code>-<slug>" so they don't overwrite each
    # other at ingestion. The first occurrence keeps the bare CoA code.
    used_codes: set[str] = set()

    # Pass-1 face rows.
    for field, label in _STATEMENT_LABELS.items():
        for p in getattr(primary, field):
            mr = _row_from_primary(label, p, coa)
            if mr.std_item_code:
                if mr.std_item_code in used_codes:
                    slug = _slug(mr.line_item)
                    base = mr.std_parent_code or mr.std_item_code
                    candidate = f"{base}-{slug}" if slug else base
                    n = 2
                    while candidate in used_codes:
                        candidate = f"{base}-{slug}-{n}" if slug else f"{base}-{n}"
                        n += 1
                    # Preserve original code as cross_reference so ingestion
                    # can still relate this row back to the canonical CoA.
                    if mr.cross_reference:
                        mr.cross_reference = f"{mr.cross_reference}, {mr.std_item_code}"
                    else:
                        mr.cross_reference = mr.std_item_code
                    mr.std_item_code = candidate
                    # Keep the CoA name for display.
                used_codes.add(mr.std_item_code)
            rows.append(mr)

    # Pass-2 note rows.
    primary_idx = _primary_lookup(primary)

    def _unique_code(base: str, line_item: str) -> str:
        """Return a code unique within the file, derived from `base` + slug.
        If the chosen slug also collides, append a numeric suffix."""
        if not base:
            return ""
        slug = _slug(line_item)
        candidate = f"{base}-{slug}" if slug else base
        n = 2
        while candidate in used_codes:
            candidate = f"{base}-{slug}-{n}" if slug else f"{base}-{n}"
            n += 1
        used_codes.add(candidate)
        return candidate

    for note in notes.notes:
        # Skip purely-narrative notes (no numeric rows AND no face anchor).
        has_data = any(s.rows for s in note.sections)
        if not has_data and not note.parent_face:
            log.debug("skipping narrative-only note %s (%s)",
                      note.note_number, note.title)
            continue

        # Resolve parent anchor(s). We emit the note ONCE (under the first
        # anchor). Any extra anchors are carried in `cross_reference` so that
        # downstream ingestion can still relate this note to multiple face
        # captions without creating duplicate rows.
        has_row_anchors = any(
            r.parent_anchor is not None for s in note.sections for r in s.rows
        )
        anchors = list(note.parent_face)
        if not anchors and not has_row_anchors and note.row_type != "Disclosure":
            log.warning("note %s has no parent_face but row_type=%s; treating as Disclosure",
                        note.note_number, note.row_type)
            note.row_type = "Disclosure"

        primary_anchor = anchors[0] if anchors else None
        extra_anchor_codes: list[str] = []
        for a in anchors[1:]:
            code_x = a.std_parent_code or ""
            if not code_x:
                pr_x = primary_idx.get(a.caption.strip().lower())
                if pr_x and pr_x.std_code:
                    code_x = pr_x.std_code
            if code_x and code_x not in extra_anchor_codes:
                extra_anchor_codes.append(code_x)

        if primary_anchor is not None:
            stmt = primary_anchor.statement
            parent_caption = primary_anchor.caption
            parent_code = primary_anchor.std_parent_code or ""
            pr = primary_idx.get(parent_caption.strip().lower())
            parent_section = pr.section if pr else ""
            if not parent_code and pr and pr.std_code:
                parent_code = pr.std_code
            parent_name = coa.name_for(parent_code)
        else:
            stmt = "Disclosure"
            parent_caption = note.title or ""
            parent_code = ""
            parent_section = ""
            parent_name = ""

        for sec in note.sections:
            for r in sec.rows:
                # Row-level parent_anchor override (used by Note 28 risk
                # tables and the synthetic equity-rollforward block).
                if r.parent_anchor is not None:
                    row_stmt = r.parent_anchor.statement
                    row_parent_caption = r.parent_anchor.caption
                    row_parent_code = r.parent_anchor.std_parent_code or ""
                    pr_row = primary_idx.get(row_parent_caption.strip().lower())
                    row_parent_section = pr_row.section if pr_row else parent_section
                    if not row_parent_code and pr_row and pr_row.std_code:
                        row_parent_code = pr_row.std_code
                    row_parent_name = coa.name_for(row_parent_code)
                else:
                    row_stmt = stmt
                    row_parent_caption = parent_caption
                    row_parent_code = parent_code
                    row_parent_section = parent_section
                    row_parent_name = parent_name

                llm_code = r.std_code or ""

                # Decide the row's own Std Item Code.
                # 1) Axis / movement / maturity / sensitivity / rollforward
                #    rows must not carry any code — they describe a
                #    dimension, not a metric.
                # 2) If the LLM gave the same code as the parent
                #    (which happens a lot), treat that as "inherited"
                #    and mint a unique "<parent>-<slug>" code.
                # 3) Otherwise keep the LLM code but if it collides with
                #    an already-used code, disambiguate with a slug.
                if _is_axis(r.line_item):
                    own_code = ""
                elif not llm_code:
                    own_code = ""
                elif llm_code == row_parent_code:
                    own_code = _unique_code(row_parent_code, r.line_item)
                elif llm_code in used_codes:
                    own_code = _unique_code(llm_code, r.line_item)
                else:
                    own_code = llm_code
                    used_codes.add(own_code)

                # Compose cross_reference: start with what the LLM gave,
                # then append extra anchor codes (if any), and — when we
                # replaced an LLM-inherited code — also the original code.
                xrefs: list[str] = []
                if r.cross_reference:
                    xrefs.append(r.cross_reference)
                for c in extra_anchor_codes:
                    if c and c != row_parent_code and c not in xrefs:
                        xrefs.append(c)
                if llm_code and llm_code != own_code and llm_code != row_parent_code and llm_code not in xrefs:
                    xrefs.append(llm_code)
                cross_ref = ", ".join(xrefs)

                # Prefer CoA name for known codes; fall back to parent name
                # for minted "<parent>-<slug>" codes so the row still shows
                # meaningful context.
                own_name = coa.name_for(own_code)
                if own_code and not own_name:
                    own_name = coa.name_for(row_parent_code)

                rows.append(MasterRow(
                    parent_statement=row_stmt,
                    parent_section=row_parent_section,
                    parent_caption=row_parent_caption,
                    std_parent_code=row_parent_code,
                    std_parent_name=row_parent_name,
                    note_number=note.note_number or "",
                    note_section=sec.section or "",
                    note_sub_section=sec.sub_section or "",
                    line_item=r.line_item,
                    std_item_code=own_code,
                    std_item_name=own_name,
                    cross_reference=cross_ref,
                    row_type=note.row_type,
                    value_current=r.value_current,
                    value_prior=r.value_prior,
                ))

        # If the note has no sections (pure narrative disclosure), still
        # emit one anchor row so it shows up in the Notes & Disclosures
        # sheet.
        if not note.sections:
            rows.append(MasterRow(
                parent_statement=stmt,
                parent_section=parent_section,
                parent_caption=parent_caption,
                std_parent_code=parent_code,
                std_parent_name=parent_name,
                note_number=note.note_number or "",
                note_section="Narrative",
                note_sub_section="",
                line_item=note.title or "(narrative disclosure)",
                std_item_code="",
                std_item_name="",
                cross_reference=", ".join(extra_anchor_codes),
                row_type=note.row_type,
                value_current=None,
                value_prior=None,
            ))

    # Post-build validation: std_item_code must be unique across non-empty
    # values (Lovable §5.1). Empty codes (axis rows) are allowed to repeat.
    seen: dict[str, MasterRow] = {}
    collisions: list[tuple[str, MasterRow, MasterRow]] = []
    for r in rows:
        if not r.std_item_code:
            continue
        if r.std_item_code in seen:
            collisions.append((r.std_item_code, seen[r.std_item_code], r))
        else:
            seen[r.std_item_code] = r
    if collisions:
        log.warning("std_item_code collisions: %d (these will cause upsert overwrites on import)",
                    len(collisions))
        for code, a, b in collisions[:10]:
            log.warning("  code=%s  A=%r/%r  B=%r/%r", code,
                        a.parent_caption, a.line_item,
                        b.parent_caption, b.line_item)

    return rows


# ---------------- sanity checks ----------------

def _sum_by_code(rows: Iterable[MasterRow], code: str, period: str) -> float | None:
    """Return value of a Primary row with std_item_code == code, period 'cur'/'prior'."""
    for r in rows:
        if r.row_type == "Primary" and r.std_item_code == code:
            return r.value_current if period == "cur" else r.value_prior
    return None


def sanity_checks(rows: list[MasterRow]) -> list[dict]:
    """Return a list of warning dicts (empty if all checks pass)."""
    warnings: list[dict] = []
    for period in ("cur", "prior"):
        ta = _sum_by_code(rows, "BS-A-TOT", period)
        te = _sum_by_code(rows, "BS-EQ-TOT", period)
        tl = _sum_by_code(rows, "BS-L-TOT", period)
        if None not in (ta, te, tl):
            diff = (te + tl) - ta
            if abs(diff) > max(1.0, abs(ta) * 1e-4):
                warnings.append({
                    "check": "BS identity (Assets = Equity + Liabilities)",
                    "period": period,
                    "assets": ta, "equity": te, "liabilities": tl,
                    "diff": diff,
                })
        rev = _sum_by_code(rows, "IS-REV", period)
        cor = _sum_by_code(rows, "IS-COR", period)
        gp = _sum_by_code(rows, "IS-GP", period)
        if None not in (rev, cor, gp):
            diff = (rev + cor) - gp  # cor is negative by convention
            if abs(diff) > max(1.0, abs(gp) * 1e-4):
                warnings.append({
                    "check": "IS identity (GP = Revenue + COR)",
                    "period": period,
                    "revenue": rev, "cor": cor, "gp": gp, "diff": diff,
                })
        cf_end = _sum_by_code(rows, "CF-END", period)
        bs_cash = _sum_by_code(rows, "BS-CA-CASH", period)
        if None not in (cf_end, bs_cash):
            diff = cf_end - bs_cash
            if abs(diff) > max(1.0, abs(bs_cash) * 1e-4):
                warnings.append({
                    "check": "CF↔BS cash tie",
                    "period": period,
                    "cf_end": cf_end, "bs_cash": bs_cash, "diff": diff,
                })
    return warnings


def collect_unmapped(rows: list[MasterRow], *, include_axis: bool = True) -> list[dict]:
    """Rows with empty Std Item Code. By default axis rows are included so
    that Pass 3 can see all candidates; pass `include_axis=False` to get
    only the rows that really need a CoA review (used for the report)."""
    out = []
    for r in rows:
        if r.std_item_code or r.row_type == "Disclosure":
            continue
        if not include_axis and _is_axis(r.line_item):
            continue
        out.append({
            "parent_statement": r.parent_statement,
            "parent_caption": r.parent_caption,
            "note_number": r.note_number,
            "note_section": r.note_section,
            "line_item": r.line_item,
            "value_current": r.value_current,
            "value_prior": r.value_prior,
            "row_type": r.row_type,
        })
    return out
