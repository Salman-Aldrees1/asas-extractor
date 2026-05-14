"""FastAPI web interface: upload PDF → stream live extraction progress → download xlsx."""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import queue
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse, Response
from starlette.middleware.sessions import SessionMiddleware

# Load .env before importing the pipeline so ANTHROPIC_API_KEY is available.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from llm_pdf_pipeline.pipeline.orchestrator import extract_pdf  # noqa: E402
from webapp import auth, db  # noqa: E402

import os
_SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-secret-change-me-in-prod")

app = FastAPI(title="Asas Financial Extractor")
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET, max_age=60 * 60 * 24 * 30)

_STATIC  = Path(__file__).resolve().parent / "static"
_OUTPUTS = Path(tempfile.gettempdir()) / "asas_outputs"
_OUTPUTS.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _logged_in(request: Request) -> bool:
    return bool(request.session.get("user"))

def _guard(request: Request) -> None:
    """Raise 401 for API routes when not logged in."""
    if not _logged_in(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ── In-memory job store ────────────────────────────────────────────────────────

@dataclass
class _Job:
    id: str
    filename: str = ""
    status: str = "pending"          # pending | running | done | failed
    messages: queue.Queue = field(default_factory=queue.Queue)
    result: Optional[dict] = None
    xlsx_path: Optional[str] = None
    company: str = ""
    period: str = ""
    error: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    )

_jobs: dict[str, _Job] = {}


# ── Logging bridge ─────────────────────────────────────────────────────────────

class _JobLogHandler(logging.Handler):
    def __init__(self, job: _Job) -> None:
        super().__init__()
        self._job = job

    def emit(self, record: logging.LogRecord) -> None:
        self._job.messages.put({
            "type": "log",
            "level": record.levelname,
            "msg": self.format(record),
        })


def _run_extraction(job: _Job, pdf_path: Path) -> None:
    handler = _JobLogHandler(job)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    pipeline_log = logging.getLogger("llm_pdf_pipeline")
    pipeline_log.addHandler(handler)
    pipeline_log.setLevel(logging.INFO)

    output_dir = _OUTPUTS / job.id
    try:
        job.status = "running"
        result = extract_pdf(pdf_path, output_dir)
        job.result   = result
        job.xlsx_path = result["xlsx"]
        job.company  = result.get("company", "")
        job.period   = f"{result.get('period_current', '')} / {result.get('period_prior', '')}".strip(" /")
        job.status   = "done"
        db.upsert_extraction(
            job_id=job.id, filename=job.filename,
            company=job.company, period=job.period,
            status="done", error="",
            result=result, xlsx_path=job.xlsx_path,
        )
        job.messages.put({"type": "done", "result": result})
    except Exception as exc:
        job.status = "failed"
        job.error  = str(exc)
        db.upsert_extraction(
            job_id=job.id, filename=job.filename,
            status="failed", error=str(exc),
        )
        job.messages.put({"type": "error", "msg": str(exc)})
    finally:
        pipeline_log.removeHandler(handler)
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse((_STATIC / "login.html").read_text(encoding="utf-8"))


@app.post("/login")
async def login(request: Request) -> Response:
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if auth.check_credentials(username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=302)
    return RedirectResponse(url="/login?error=1", status_code=302)


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ── Main routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)) -> dict:
    _guard(request)
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 50 MB).")

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(data)
    tmp.close()

    job = _Job(id=str(uuid.uuid4())[:8], filename=file.filename or "upload.pdf")
    _jobs[job.id] = job

    # Register as "running" in DB immediately so it shows in history
    db.upsert_extraction(
        job_id=job.id, filename=job.filename,
        status="running", error="",
    )

    threading.Thread(
        target=_run_extraction,
        args=(job, Path(tmp.name)),
        daemon=True,
    ).start()

    return {"job_id": job.id}


@app.get("/stream/{job_id}")
async def stream(job_id: str, request: Request) -> StreamingResponse:
    if not _logged_in(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")

    async def _generate():
        loop = asyncio.get_running_loop()
        while True:
            try:
                msg = await loop.run_in_executor(
                    None, lambda: job.messages.get(timeout=30)
                )
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/history")
def history(request: Request) -> list:
    _guard(request)
    # DB records (persistent, all sessions)
    db_rows = {r["id"]: r for r in db.fetch_history()}

    # Merge in-memory jobs (covers current session, including running ones
    # not yet flushed to DB, or when DB is not configured)
    for job in sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True):
        if job.id not in db_rows:
            db_rows[job.id] = {
                "id": job.id,
                "filename": job.filename,
                "company": job.company,
                "period": job.period,
                "status": job.status,
                "error": job.error,
                "rows_count": job.result.get("rows") if job.result else None,
                "cost_usd": job.result.get("cost_usd") if job.result else None,
                "tokens_in": job.result.get("tokens_in") if job.result else None,
                "tokens_out": job.result.get("tokens_out") if job.result else None,
                "sanity_warn": job.result.get("sanity_warnings") if job.result else None,
                "unmapped": job.result.get("unmapped_rows") if job.result else None,
                "created_at": job.created_at,
                "has_xlsx": job.xlsx_path is not None,
            }

    return sorted(db_rows.values(), key=lambda r: r.get("created_at", ""), reverse=True)


@app.get("/download/{job_id}")
def download(job_id: str, request: Request) -> Response:
    _guard(request)

    # Try in-memory file first (same session, file still on disk)
    job = _jobs.get(job_id)
    if job and job.xlsx_path:
        path = Path(job.xlsx_path)
        if path.exists():
            return FileResponse(
                path=str(path),
                filename=path.name,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    # Fall back to DB blob
    xlsx_bytes = db.fetch_xlsx(job_id)
    if xlsx_bytes:
        filename = f"{job_id}__data.xlsx"
        if job and job.filename:
            filename = Path(job.filename).stem + "__data.xlsx"
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    raise HTTPException(404, "File not available.")
