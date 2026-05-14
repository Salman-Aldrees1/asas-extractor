"""PostgreSQL persistence for extraction history and xlsx blobs.

Falls back gracefully to no-op when DATABASE_URL is not set (local dev).
"""
from __future__ import annotations

import logging
import os
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


def _enabled() -> bool:
    return bool(_DATABASE_URL) and _HAS_PG


def _connect():
    url = _DATABASE_URL
    # Render uses postgres:// but psycopg2 requires postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db() -> None:
    if not _enabled():
        log.warning("DATABASE_URL not set or psycopg2 missing — history is in-memory only")
        return
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS extractions (
                        id           TEXT PRIMARY KEY,
                        filename     TEXT,
                        company      TEXT,
                        period       TEXT,
                        status       TEXT,
                        error        TEXT,
                        rows_count   INTEGER,
                        cost_usd     REAL,
                        tokens_in    INTEGER,
                        tokens_out   INTEGER,
                        sanity_warn  INTEGER,
                        unmapped     INTEGER,
                        created_at   TIMESTAMPTZ DEFAULT NOW(),
                        xlsx_data    BYTEA
                    )
                """)
            conn.commit()
        log.info("DB ready")
    except Exception as exc:
        log.error("DB init failed: %s", exc)


def upsert_extraction(
    job_id: str,
    filename: str,
    status: str,
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
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extractions
                        (id, filename, company, period, status, error,
                         rows_count, cost_usd, tokens_in, tokens_out,
                         sanity_warn, unmapped, xlsx_data)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        company     = EXCLUDED.company,
                        period      = EXCLUDED.period,
                        status      = EXCLUDED.status,
                        error       = EXCLUDED.error,
                        rows_count  = EXCLUDED.rows_count,
                        cost_usd    = EXCLUDED.cost_usd,
                        tokens_in   = EXCLUDED.tokens_in,
                        tokens_out  = EXCLUDED.tokens_out,
                        sanity_warn = EXCLUDED.sanity_warn,
                        unmapped    = EXCLUDED.unmapped,
                        xlsx_data   = COALESCE(EXCLUDED.xlsx_data, extractions.xlsx_data)
                    """,
                    (
                        job_id, filename, company, period, status, error,
                        result.get("rows") if result else None,
                        result.get("cost_usd") if result else None,
                        result.get("tokens_in") if result else None,
                        result.get("tokens_out") if result else None,
                        result.get("sanity_warnings") if result else None,
                        result.get("unmapped_rows") if result else None,
                        psycopg2.Binary(xlsx_data) if xlsx_data else None,
                    ),
                )
            conn.commit()
    except Exception as exc:
        log.error("DB upsert failed: %s", exc)


def fetch_history() -> list[dict]:
    if not _enabled():
        return []
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, filename, company, period, status, error,
                           rows_count, cost_usd, tokens_in, tokens_out,
                           sanity_warn, unmapped,
                           to_char(created_at AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD HH24:MI') || ' UTC' AS created_at,
                           (xlsx_data IS NOT NULL) AS has_xlsx
                    FROM extractions
                    ORDER BY created_at DESC
                    LIMIT 200
                    """
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        log.error("DB fetch_history failed: %s", exc)
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
        log.error("DB fetch_xlsx failed: %s", exc)
    return None
