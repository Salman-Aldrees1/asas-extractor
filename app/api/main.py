from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_admin
from app.core.security import create_access_token, hash_password, verify_password
from app.ingestion.service import process_uploaded_pdf
from app.schemas.auth import LoginRequest, TokenResponse, UserResponse
from app.schemas.company import CompanyCreateRequest, CompanyResponse
from app.storage.database import Base, engine, get_db
from app.storage.models import Company, FinancialMetric, IngestionJob, JobStatus, User, UserRole, ValidationResult

app = FastAPI(title="Asas Financials API", version="0.1.0")

WEB_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "web"
RAW_UPLOADS = Path(__file__).resolve().parents[2] / "data" / "raw"
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"
RAW_UPLOADS.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _metric_source_rank(source: str | None) -> int:
    normalized = (source or "").lower()
    if normalized == "pdf_ingestion":
        return 3
    if normalized == "seed":
        return 1
    if normalized:
        return 2
    return 0


def _prefer_candidate_metric(candidate: FinancialMetric, current: FinancialMetric) -> bool:
    candidate_rank = (_metric_source_rank(candidate.source), candidate.id)
    current_rank = (_metric_source_rank(current.source), current.id)
    return candidate_rank > current_rank


def _snapshot_seed_rows() -> dict[tuple[str, str, int], float]:
    snapshot = _read_json(OUTPUT_DIR / "excel_financials_snapshot.json")
    balance_sheet = snapshot.get("balance_sheet", {})
    income = snapshot.get("income_statement", {})
    cash_flow = snapshot.get("cash_flow", {})

    rows: dict[tuple[str, str, int], float] = {}

    map_income = {
        "Revenue": "Revenue",
        "Cost of Revenue": "Cost of Revenue",
        "Gross Profit": "Gross Profit",
        "Operating profit": "Operating Profit",
        "Net profit for the period before Zakat and Tax": "Net Profit Before Tax",
        "Net profit for the period": "Net Profit",
    }
    for src, label in map_income.items():
        yearly = income.get(src, {})
        for period, value in yearly.items():
            if "FY-" not in period:
                continue
            year = int(period.replace("FY-", "").split(" ")[0])
            key = ("income_statement", label, year)
            candidate = float(value)
            existing = rows.get(key)
            if existing is None or abs(candidate) >= abs(existing):
                rows[key] = candidate

    map_cf = {
        "Net cash flows generated from operating activities": "Operating Cash Flow",
        "Net cash flows used in investing activities": "Investing Cash Flow",
        "Net cash flows used in financing activities": "Financing Cash Flow",
        "Cash and cash equivalents at the end of the year": "Cash and Cash Equivalents",
    }
    for src, label in map_cf.items():
        yearly = cash_flow.get(src, {})
        for period, value in yearly.items():
            if "FY-" not in period:
                continue
            year = int(period.replace("FY-", "").split(" ")[0])
            key = ("cash_flow", label, year)
            candidate = float(value)
            existing = rows.get(key)
            if existing is None or abs(candidate) >= abs(existing):
                rows[key] = candidate

    map_bs = {
        "Total Non-Current Assets": "Total Non-Current Assets",
        "Total Current Assets": "Total Current Assets",
        "Total Assets": "Total Assets",
        "Total Equity": "Total Equity",
        "Total non-current liabilities": "Total Non-Current Liabilities",
        "Total Current Liabilities": "Total Current Liabilities",
        "Total Liabilities": "Total Liabilities",
    }
    for src, label in map_bs.items():
        yearly = balance_sheet.get(src, {})
        for period, value in yearly.items():
            if "FY-" not in period:
                continue
            year = int(period.replace("FY-", "").split(" ")[0])
            key = ("balance_sheet", label, year)
            candidate = float(value)
            existing = rows.get(key)
            if existing is None or abs(candidate) >= abs(existing):
                rows[key] = candidate

    return rows


def _adjust_demo_value(base_value: float, metric: str, year: int, min_year: int, company_seed_index: int) -> float:
    base_scale = [0.76, 0.9, 1.0, 1.14, 1.31]
    year_drift = [0.012, 0.006, 0.0, -0.004, -0.009]
    margin_bias = [-0.05, -0.02, 0.0, 0.025, 0.045]

    idx = company_seed_index % len(base_scale)
    scale = base_scale[idx]
    drift = year_drift[idx]
    bias = margin_bias[idx]

    year_factor = 1 + (year - min_year) * drift
    metric_factor = 1.0
    if metric in {"Gross Profit", "Operating Profit", "Net Profit"}:
        metric_factor += bias
    elif metric == "Operating Cash Flow":
        metric_factor += bias * 0.55
    elif metric == "Investing Cash Flow":
        metric_factor -= bias * 0.4
    elif metric == "Financing Cash Flow":
        metric_factor += bias * 0.25

    return round(base_value * scale * year_factor * metric_factor, 2)


def _seed_company_metrics(db: Session, company_id: int, company_seed_index: int) -> None:
    baseline = _snapshot_seed_rows()
    if not baseline:
        return

    min_year = min(year for _, _, year in baseline.keys())
    target: dict[tuple[str, str, int], float] = {}
    for (statement, metric, year), value in baseline.items():
        target[(statement, metric, year)] = _adjust_demo_value(value, metric, year, min_year, company_seed_index)

    existing_seed_rows = db.execute(
        select(FinancialMetric)
        .where(FinancialMetric.company_id == company_id)
        .where(FinancialMetric.source == "seed")
        .order_by(FinancialMetric.id.asc())
    ).scalars().all()

    by_key: dict[tuple[str, str, int], list[FinancialMetric]] = {}
    for row in existing_seed_rows:
        key = (row.statement, row.metric, row.year)
        by_key.setdefault(key, []).append(row)

    for key, adjusted_value in target.items():
        statement, metric, year = key
        bucket = by_key.pop(key, [])
        if bucket:
            keep = bucket[0]
            keep.value = adjusted_value
            keep.currency = "SAR"
            keep.source = "seed"
            for duplicate in bucket[1:]:
                db.delete(duplicate)
            continue

        db.add(
            FinancialMetric(
                company_id=company_id,
                statement=statement,
                metric=metric,
                year=year,
                value=adjusted_value,
                currency="SAR",
                source="seed",
            )
        )

    for stale_rows in by_key.values():
        for stale in stale_rows:
            db.delete(stale)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)

    db = next(get_db())
    try:
        admin = db.execute(select(User).where(User.email == "admin@asas.local")).scalar_one_or_none()
        if not admin:
            db.add(
                User(
                    email="admin@asas.local",
                    hashed_password=hash_password("admin123"),
                    role=UserRole.admin,
                )
            )

        user = db.execute(select(User).where(User.email == "user@asas.local")).scalar_one_or_none()
        if not user:
            db.add(
                User(
                    email="user@asas.local",
                    hashed_password=hash_password("user123"),
                    role=UserRole.user,
                )
            )

        company_seeds = [
            ("Asas Demo Co", "ASAS", "Financial Services"),
            ("Saudi Retail Group", "SRG", "Consumer"),
            ("Gulf Manufacturing", "GLF", "Industrials"),
            ("Riyadh Logistics", "RLG", "Logistics"),
            ("Najd Telecom", "NJD", "Technology"),
        ]
        for idx, (name, ticker, sector) in enumerate(company_seeds):
            company = db.execute(select(Company).where(Company.ticker == ticker)).scalar_one_or_none()
            if not company:
                company = Company(name=name, ticker=ticker, sector=sector)
                db.add(company)
                db.flush()
            _seed_company_metrics(db, company.id, idx)

        ingestion_only_ticker = "PDFONLY"
        ingestion_only_company = db.execute(
            select(Company).where(Company.ticker == ingestion_only_ticker)
        ).scalar_one_or_none()
        if not ingestion_only_company:
            ingestion_only_company = Company(
                name="PDF Ingestion Only",
                ticker=ingestion_only_ticker,
                sector="Validation Sandbox",
            )
            db.add(ingestion_only_company)
            db.flush()

        db.execute(
            delete(FinancialMetric)
            .where(FinancialMetric.company_id == ingestion_only_company.id)
            .where(FinancialMetric.source == "seed")
        )

        db.commit()
    finally:
        db.close()


app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


@app.get("/")
def web_index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/dashboard")
def web_dashboard() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/login")
def web_login() -> FileResponse:
    return FileResponse(WEB_DIR / "login.html")


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(subject=str(user.id), role=user.role.value)
    return TokenResponse(access_token=token, role=user.role.value)


@app.get("/auth/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse(id=current_user.id, email=current_user.email, role=current_user.role.value)


@app.get("/companies", response_model=list[CompanyResponse])
def list_companies(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CompanyResponse]:
    rows = db.execute(select(Company).order_by(Company.name.asc())).scalars().all()
    return [CompanyResponse(id=c.id, name=c.name, ticker=c.ticker, sector=c.sector) for c in rows]


@app.post("/companies", response_model=CompanyResponse)
def create_company(
    payload: CompanyCreateRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> CompanyResponse:
    existing = db.execute(select(Company).where(Company.ticker == payload.ticker)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Ticker already exists")

    company = Company(name=payload.name, ticker=payload.ticker, sector=payload.sector)
    db.add(company)
    db.commit()
    db.refresh(company)
    return CompanyResponse(id=company.id, name=company.name, ticker=company.ticker, sector=company.sector)


@app.get("/companies/{company_id}/metrics")
def company_metrics(
    company_id: int,
    statement: str | None = None,
    metric: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    query = select(FinancialMetric).where(FinancialMetric.company_id == company_id)
    if statement:
        query = query.where(FinancialMetric.statement == statement)
    if metric:
        query = query.where(FinancialMetric.metric == metric)

    rows = db.execute(query.order_by(FinancialMetric.year.asc())).scalars().all()
    return [
        {
            "id": r.id,
            "statement": r.statement,
            "metric": r.metric,
            "year": r.year,
            "value": r.value,
            "currency": r.currency,
            "source": r.source,
        }
        for r in rows
    ]


@app.get("/companies/{company_id}/quality/latest")
def company_quality_latest(
    company_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    row = db.execute(
        select(ValidationResult)
        .where(ValidationResult.company_id == company_id)
        .order_by(ValidationResult.created_at.desc())
    ).scalars().first()
    if not row:
        return {
            "company_id": company_id,
            "match_rate": None,
            "total_checks": 0,
            "matched": 0,
            "mismatched": 0,
            "missing": 0,
        }

    return {
        "company_id": company_id,
        "match_rate": (row.match_rate if row.total_checks > 0 else None),
        "total_checks": row.total_checks,
        "matched": row.matched,
        "mismatched": row.mismatched,
        "missing": row.missing,
        "created_at": row.created_at.isoformat(),
    }


@app.get("/companies/{company_id}/view")
def company_view(
    company_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    rows = db.execute(
        select(FinancialMetric)
        .where(FinancialMetric.company_id == company_id)
        .order_by(FinancialMetric.statement.asc(), FinancialMetric.metric.asc(), FinancialMetric.year.asc())
    ).scalars().all()

    source_breakdown: dict[str, int] = {}
    years = sorted({row.year for row in rows})
    statements: dict[str, dict[str, dict[int, FinancialMetric]]] = {}
    duplicates_dropped = 0
    for row in rows:
        source_breakdown[row.source] = source_breakdown.get(row.source, 0) + 1
        statement_bucket = statements.setdefault(row.statement, {})
        metric_bucket = statement_bucket.setdefault(row.metric, {})
        existing = metric_bucket.get(row.year)
        if not existing:
            metric_bucket[row.year] = row
            continue
        if _prefer_candidate_metric(row, existing):
            metric_bucket[row.year] = row
        duplicates_dropped += 1

    table: list[dict] = []
    for statement, metrics in statements.items():
        for metric, values_by_year in metrics.items():
            values = {str(year): (values_by_year[year].value if year in values_by_year else None) for year in years}
            table.append({"statement": statement, "metric": metric, "values": values})

    dedup_points = sum(len(values_by_year) for metrics in statements.values() for values_by_year in metrics.values())

    return {
        "company": {
            "id": company.id,
            "name": company.name,
            "ticker": company.ticker,
            "sector": company.sector,
        },
        "years": years,
        "table": table,
        "stats": {
            "raw_points": len(rows),
            "dedup_points": dedup_points,
            "duplicates_dropped": duplicates_dropped,
            "source_breakdown": source_breakdown,
        },
    }


@app.get("/dashboard/overview")
def dashboard_overview(
    company_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    metrics = db.execute(
        select(FinancialMetric).where(FinancialMetric.company_id == company_id)
    ).scalars().all()

    grouped: dict[str, dict[int, FinancialMetric]] = {}
    for row in metrics:
        year_bucket = grouped.setdefault(row.metric, {})
        existing = year_bucket.get(row.year)
        if not existing or _prefer_candidate_metric(row, existing):
            year_bucket[row.year] = row

    series = {
        metric: [
            {"year": year, "value": metric_row.value}
            for year, metric_row in sorted(values_by_year.items(), key=lambda item: item[0])
        ]
        for metric, values_by_year in grouped.items()
    }

    return {"company_id": company_id, "series": series}


@app.get("/ingestion/jobs")
def list_jobs(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),  # Admin only
):
    rows = db.execute(select(IngestionJob).order_by(IngestionJob.created_at.desc())).scalars().all()
    return [
        {
            "id": j.id,
            "company_id": j.company_id,
            "file_name": j.file_name,
            "status": j.status.value,
            "message": j.message,
            "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in rows
    ]


@app.post("/ingestion/upload")
def upload_pdf(
    company_id: int = Form(...),
    pdf_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_name = f"{company.ticker}_{timestamp}_{pdf_file.filename}"
    file_path = RAW_UPLOADS / file_name
    file_path.write_bytes(pdf_file.file.read())

    job = IngestionJob(
        company_id=company_id,
        file_name=pdf_file.filename,
        file_path=str(file_path),
        status=JobStatus.uploaded,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    processed = process_uploaded_pdf(db=db, company_id=company_id, job=job)
    return {
        "job_id": processed.id,
        "status": processed.status.value,
        "message": processed.message,
    }
