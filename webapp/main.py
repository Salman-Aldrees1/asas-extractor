"""FastAPI web interface: upload PDF → stream live extraction progress → download xlsx."""
from __future__ import annotations

import asyncio
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
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

# Load .env before importing the pipeline so ANTHROPIC_API_KEY is available.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from llm_pdf_pipeline.pipeline.orchestrator import extract_pdf  # noqa: E402

app = FastAPI(title="Asas Financial Extractor")

_STATIC = Path(__file__).resolve().parent / "static"
_OUTPUTS = Path(tempfile.gettempdir()) / "asas_outputs"
_OUTPUTS.mkdir(parents=True, exist_ok=True)


# ── In-memory job store ────────────────────────────────────────────────────────

@dataclass
class _Job:
    id: str
    status: str = "pending"          # pending | running | done | failed
    messages: queue.Queue = field(default_factory=queue.Queue)
    result: Optional[dict] = None
    xlsx_path: Optional[str] = None

_jobs: dict[str, _Job] = {}


# ── Logging bridge ─────────────────────────────────────────────────────────────

class _JobLogHandler(logging.Handler):
    """Forwards pipeline log records into the job's message queue."""
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
        job.result = result
        job.xlsx_path = result["xlsx"]
        job.status = "done"
        job.messages.put({"type": "done", "result": result})
    except Exception as exc:
        job.status = "failed"
        job.messages.put({"type": "error", "msg": str(exc)})
    finally:
        pipeline_log.removeHandler(handler)
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.post("/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 50 MB).")

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(data)
    tmp.close()

    job = _Job(id=str(uuid.uuid4())[:8])
    _jobs[job.id] = job

    threading.Thread(
        target=_run_extraction,
        args=(job, Path(tmp.name)),
        daemon=True,
    ).start()

    return {"job_id": job.id}


@app.get("/stream/{job_id}")
async def stream(job_id: str) -> StreamingResponse:
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


@app.get("/download/{job_id}")
def download(job_id: str) -> FileResponse:
    job = _jobs.get(job_id)
    if not job or not job.xlsx_path:
        raise HTTPException(404, "Result not ready.")
    path = Path(job.xlsx_path)
    if not path.exists():
        raise HTTPException(404, "File no longer available.")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
