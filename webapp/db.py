"""PostgreSQL persistence: 4-table star schema for multi-company financial data.

Tables
------
coa_accounts      – 156-entry master CoA (seeded from YAML, shared globally)
companies         – one row per real-world company, with aliases for name matching
extractions       – one row per PDF upload (audit trail + xlsx blob)
financial_values  – fact table: one row per (company × fiscal_year × line_item)

Falls back to no-op when DATABASE_URL is not set (local dev without a DB).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DATABASE_URL = os.getenv("DATABASE_URL", "")

try:
    import psycopg2
    import psycopg2.extras
    _HAS_PG = True
except ImportError:
    _HAS_PG = False


# ── Connection ─────────────────────────────────────────────────────────────────

def _enabled() -> bool:
    return bool(_DATABASE_URL) and _HAS_PG


def _connect():
    url = _DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Schema bootstrap ───────────────────────────────────────────────────────────

def init_db() -> None:
    if not _enabled():
        log.warning("DATABASE_URL not set — all DB operations are no-ops")
        return
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS coa_accounts (
                        code        TEXT PRIMARY KEY,
                        name        TEXT NOT NULL,
                        category    TEXT,
                        statement   TEXT,
                        sort_order  INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS companies (
                        company_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        name        TEXT NOT NULL,
                        aliases     TEXT[],
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS extractions (
                        id                  TEXT PRIMARY KEY,
                        company_id          UUID REFERENCES companies(company_id),
                        filename            TEXT,
                        period_current      TEXT,
                        period_prior        TEXT,
                        fiscal_year_current INTEGER,
                        fiscal_year_prior   INTEGER,
                        status              TEXT,
                        error               TEXT,
                        rows_count          INTEGER,
                        cost_usd            REAL,
                        tokens_in           INTEGER,
                        tokens_out          INTEGER,
                        sanity_warn         INTEGER,
                        unmapped            INTEGER,
                        created_at          TIMESTAMPTZ DEFAULT NOW(),
                        xlsx_data           BYTEA
                    );

                    CREATE TABLE IF NOT EXISTS financial_values (
                        id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        company_id       UUID NOT NULL REFERENCES companies(company_id),
                        extraction_id    TEXT NOT NULL REFERENCES extractions(id),
                        fiscal_year      INTEGER NOT NULL,
                        parent_statement TEXT,
                        parent_section   TEXT,
                        parent_caption   TEXT,
                        std_parent_code  TEXT,
                        std_parent_name  TEXT,
                        note_number      TEXT,
                        note_section     TEXT,
                        note_sub_section TEXT,
                        line_item        TEXT,
                        std_item_code    TEXT,
                        std_item_name    TEXT,
                        cross_reference  TEXT,
                        row_type         TEXT,
                        value            DECIMAL(20, 2)
                    );

                    CREATE INDEX IF NOT EXISTS idx_fv_company_year
                        ON financial_values(company_id, fiscal_year);
                    CREATE INDEX IF NOT EXISTS idx_fv_std_item
                        ON financial_values(std_item_code);
                    CREATE INDEX IF NOT EXISTS idx_fv_extraction
                        ON financial_values(extraction_id);
                """)
            conn.commit()
        log.info("DB schema ready")
        _seed_coa()
    except Exception as exc:
        log.error("DB init failed: %s", exc)


def _seed_coa() -> None:
    """Load the 156-entry CoA YAML into coa_accounts (idempotent)."""
    try:
        import yaml
        yaml_path = Path(__file__).resolve().parent.parent / \
            "llm_pdf_pipeline" / "taxonomy" / "standard_coa.yaml"
        if not yaml_path.exists():
            log.warning("CoA YAML not found at %s", yaml_path)
            return
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        accounts = data.get("accounts", [])
        with _connect() as conn:
            with conn.cursor() as cur:
                for i, acct in enumerate(accounts):
                    cur.execute("""
                        INSERT INTO coa_accounts (code, name, category, statement, sort_order)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (code) DO UPDATE SET
                            name       = EXCLUDED.name,
                            category   = EXCLUDED.category,
                            statement  = EXCLUDED.statement,
                            sort_order = EXCLUDED.sort_order
                    """, (
                        acct.get("code"), acct.get("name"),
                        acct.get("category"), acct.get("statement"), i,
                    ))
            conn.commit()
        log.info("CoA seeded: %d accounts", len(accounts))
    except Exception as exc:
        log.error("CoA seed failed: %s", exc)


# ── Company registry ───────────────────────────────────────────────────────────

def get_or_create_company(name: str) -> Optional[str]:
    """Return UUID string for a company, creating one if it doesn't exist.

    Matching strategy (Phase 1 — simple):
    1. Exact match on canonical name
    2. Match if `name` appears in the aliases array
    3. Create new company if no match
    """
    if not _enabled() or not name:
        return None
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                # Exact name match
                cur.execute(
                    "SELECT company_id FROM companies WHERE name = %s", (name,)
                )
                row = cur.fetchone()
                if row:
                    return str(row["company_id"])

                # Alias match
                cur.execute(
                    "SELECT company_id FROM companies WHERE %s = ANY(aliases)", (name,)
                )
                row = cur.fetchone()
                if row:
                    # Add as alias for future lookups
                    cur.execute(
                        "UPDATE companies SET aliases = array_append(aliases, %s)"
                        " WHERE company_id = %s",
                        (name, row["company_id"]),
                    )
                    conn.commit()
                    return str(row["company_id"])

                # Create new
                cur.execute(
                    "INSERT INTO companies (name, aliases) VALUES (%s, %s)"
                    " RETURNING company_id",
                    (name, [name]),
                )
                company_id = str(cur.fetchone()["company_id"])
                conn.commit()
                log.info("Created company: %s → %s", name, company_id)
                return company_id
    except Exception as exc:
        log.error("get_or_create_company failed: %s", exc)
        return None


def list_companies() -> list[dict]:
    if not _enabled():
        return []
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT company_id::text, name, aliases FROM companies ORDER BY name"
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        log.error("list_companies failed: %s", exc)
        return []


# ── Extractions ────────────────────────────────────────────────────────────────

def _parse_year(label: str) -> Optional[int]:
    """Extract a 4-digit calendar year from a period label string."""
    if not label:
        return None
    m = re.search(r'\b((?:19|20)\d{2})\b', label)
    return int(m.group(1)) if m else None


def upsert_extraction(
    job_id: str,
    filename: str,
    status: str,
    company_id: Optional[str] = None,
    company: str = "",
    period: str = "",
    error: str = "",
    result: Optional[dict] = None,
    xlsx_path: Optional[str] = None,
) -> None:
    if not _enabled():
        return

    xlsx_data: Optional[bytes] = None
    if xlsx_path:
        try:
            xlsx_data = Path(xlsx_path).read_bytes()
        except Exception:
            pass

    period_current = result.get("period_current", "") if result else ""
    period_prior   = result.get("period_prior",   "") if result else ""

    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extractions
                        (id, company_id, filename, period_current, period_prior,
                         fiscal_year_current, fiscal_year_prior,
                         status, error, rows_count, cost_usd,
                         tokens_in, tokens_out, sanity_warn, unmapped, xlsx_data)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        company_id          = COALESCE(EXCLUDED.company_id, extractions.company_id),
                        period_current      = EXCLUDED.period_current,
                        period_prior        = EXCLUDED.period_prior,
                        fiscal_year_current = EXCLUDED.fiscal_year_current,
                        fiscal_year_prior   = EXCLUDED.fiscal_year_prior,
                        status              = EXCLUDED.status,
                        error               = EXCLUDED.error,
                        rows_count          = EXCLUDED.rows_count,
                        cost_usd            = EXCLUDED.cost_usd,
                        tokens_in           = EXCLUDED.tokens_in,
                        tokens_out          = EXCLUDED.tokens_out,
                        sanity_warn         = EXCLUDED.sanity_warn,
                        unmapped            = EXCLUDED.unmapped,
                        xlsx_data           = COALESCE(EXCLUDED.xlsx_data, extractions.xlsx_data)
                    """,
                    (
                        job_id,
                        company_id or None,
                        filename,
                        period_current,
                        period_prior,
                        _parse_year(period_current),
                        _parse_year(period_prior),
                        status,
                        error,
                        result.get("rows")          if result else None,
                        result.get("cost_usd")       if result else None,
                        result.get("tokens_in")      if result else None,
                        result.get("tokens_out")     if result else None,
                        result.get("sanity_warnings") if result else None,
                        result.get("unmapped_rows")  if result else None,
                        psycopg2.Binary(xlsx_data)   if xlsx_data else None,
                    ),
                )
            conn.commit()
    except Exception as exc:
        log.error("upsert_extraction failed: %s", exc)


# ── Financial values (fact table) ──────────────────────────────────────────────

def save_financial_values(
    company_id: str,
    extraction_id: str,
    fiscal_year_current: Optional[int],
    fiscal_year_prior: Optional[int],
    master_rows: list[dict],
) -> None:
    """Insert all MasterRow data into financial_values.

    Each MasterRow with two periods (value_current + value_prior) becomes
    TWO rows in financial_values — one per fiscal year.
    Rows with NULL values for a period are skipped for that period.
    """
    if not _enabled() or not company_id or not master_rows:
        return

    _FIELDS = (
        "parent_statement", "parent_section", "parent_caption",
        "std_parent_code", "std_parent_name",
        "note_number", "note_section", "note_sub_section",
        "line_item", "std_item_code", "std_item_name",
        "cross_reference", "row_type",
    )

    def _row_tuple(mr: dict, fiscal_year: int, value) -> tuple:
        return (
            company_id, extraction_id, fiscal_year,
            mr.get("parent_statement"), mr.get("parent_section"),
            mr.get("parent_caption"), mr.get("std_parent_code"),
            mr.get("std_parent_name"), mr.get("note_number"),
            mr.get("note_section"), mr.get("note_sub_section"),
            mr.get("line_item"), mr.get("std_item_code"),
            mr.get("std_item_name"), mr.get("cross_reference"),
            mr.get("row_type"), value,
        )

    rows_to_insert: list[tuple] = []
    for mr in master_rows:
        vc = mr.get("value_current")
        vp = mr.get("value_prior")
        if fiscal_year_current is not None and vc is not None:
            rows_to_insert.append(_row_tuple(mr, fiscal_year_current, vc))
        if fiscal_year_prior is not None and vp is not None:
            rows_to_insert.append(_row_tuple(mr, fiscal_year_prior, vp))

    if not rows_to_insert:
        return

    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO financial_values
                        (company_id, extraction_id, fiscal_year,
                         parent_statement, parent_section, parent_caption,
                         std_parent_code, std_parent_name,
                         note_number, note_section, note_sub_section,
                         line_item, std_item_code, std_item_name,
                         cross_reference, row_type, value)
                    VALUES %s
                    """,
                    rows_to_insert,
                )
            conn.commit()
        log.info("Saved %d financial_values rows for extraction %s",
                 len(rows_to_insert), extraction_id)
    except Exception as exc:
        log.error("save_financial_values failed: %s", exc)


# ── Queries ────────────────────────────────────────────────────────────────────

def fetch_history() -> list[dict]:
    if not _enabled():
        return []
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT e.id, e.filename,
                           COALESCE(c.name, '') AS company,
                           e.company_id::text,
                           e.period_current || CASE WHEN e.period_prior IS NOT NULL
                               THEN ' / ' || e.period_prior ELSE '' END AS period,
                           e.status, e.error,
                           e.rows_count, e.cost_usd,
                           e.tokens_in, e.tokens_out,
                           e.sanity_warn, e.unmapped,
                           to_char(e.created_at AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD HH24:MI') || ' UTC' AS created_at,
                           (e.xlsx_data IS NOT NULL) AS has_xlsx
                    FROM extractions e
                    LEFT JOIN companies c ON e.company_id = c.company_id
                    ORDER BY e.created_at DESC
                    LIMIT 200
                """)
                return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        log.error("fetch_history failed: %s", exc)
        return []


def fetch_xlsx(job_id: str) -> Optional[bytes]:
    if not _enabled():
        return None
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT xlsx_data FROM extractions WHERE id = %s", (job_id,)
                )
                row = cur.fetchone()
                if row and row["xlsx_data"]:
                    return bytes(row["xlsx_data"])
    except Exception as exc:
        log.error("fetch_xlsx failed: %s", exc)
    return None
