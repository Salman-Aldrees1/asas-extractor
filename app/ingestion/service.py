from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, delete
from sqlalchemy.orm import Session

from extract_financials import (
    build_summary,
    extract_financial_data_smart,
    pdf_to_text,
    read_excel_data,
    validate,
)

from app.storage.models import FinancialMetric, IngestionJob, JobStatus, ValidationResult


def _extract_from_uploaded_pdf(pdf_path: Path) -> dict:
    return extract_financial_data_smart(pdf_path)


def _upsert_metric(
    db: Session,
    company_id: int,
    statement: str,
    metric: str,
    year: int,
    value: float,
    source: str,
) -> None:
    db.execute(
        delete(FinancialMetric).where(
            and_(
                FinancialMetric.company_id == company_id,
                FinancialMetric.statement == statement,
                FinancialMetric.metric == metric,
                FinancialMetric.year == year,
            )
        )
    )
    db.add(
        FinancialMetric(
            company_id=company_id,
            statement=statement,
            metric=metric,
            year=year,
            value=value,
            currency="SAR",
            source=source,
        )
    )


def process_uploaded_pdf(db: Session, company_id: int, job: IngestionJob) -> IngestionJob:
    job.status = JobStatus.processing
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        validation_error: str | None = None
        try:
            source = _extract_from_uploaded_pdf(Path(job.file_path))
        except Exception as exc:
            raise ValueError(f"PDF extraction failed: {exc}") from exc

        if not source or not source.get("statement_of_profit_or_loss"):
            raise ValueError("PDF extraction returned no statement_of_profit_or_loss data")

        pnl = source.get("statement_of_profit_or_loss", {})
        cfs = source.get("statement_of_cash_flows", {})

        metric_map = {
            "revenue": ("income_statement", "Revenue"),
            "gross_profit": ("income_statement", "Gross Profit"),
            "operating_profit": ("income_statement", "Operating Profit"),
            "net_profit": ("income_statement", "Net Profit"),
        }
        for payload_key, (statement, label) in metric_map.items():
            yearly = pnl.get(payload_key, {})
            for year in yearly.keys():
                if year not in ["source_label"]:  # Skip non-year keys
                    try:
                        _upsert_metric(
                            db,
                            company_id,
                            statement,
                            label,
                            int(year),
                            float(yearly[year]),
                            source="pdf_ingestion",
                        )
                    except (ValueError, TypeError):
                        continue

        cf_map = {
            "net_cash_from_operating": "Operating Cash Flow",
            "net_cash_used_in_investing": "Investing Cash Flow",
            "net_cash_used_in_financing": "Financing Cash Flow",
        }
        for payload_key, label in cf_map.items():
            yearly = cfs.get(payload_key, {})
            for year in yearly.keys():
                if year not in ["source_label"]:  # Skip non-year keys
                    try:
                        _upsert_metric(
                            db,
                            company_id,
                            "cash_flow",
                            label,
                            int(year),
                            float(yearly[year]),
                            source="pdf_ingestion",
                        )
                    except (ValueError, TypeError):
                        continue

        try:
            excel_data = read_excel_data()
            validation_details = validate(source, {"income_statement_summary": {}}, excel_data)
            validation_summary = build_summary(validation_details)
            db.add(
                ValidationResult(
                    company_id=company_id,
                    job_id=job.id,
                    total_checks=validation_summary.get("total_checks", 0),
                    matched=validation_summary.get("matched", 0),
                    mismatched=validation_summary.get("mismatched", 0),
                    missing=validation_summary.get("missing", 0),
                    match_rate=float(validation_summary.get("match_rate", 0.0)),
                )
            )
        except Exception as exc:
            validation_error = str(exc)
            db.add(
                ValidationResult(
                    company_id=company_id,
                    job_id=job.id,
                    total_checks=0,
                    matched=0,
                    mismatched=0,
                    missing=0,
                    match_rate=0.0,
                )
            )

        job.status = JobStatus.completed
        if validation_error:
            job.message = f"PDF processed; validation unavailable: {validation_error}"
        else:
            job.message = "PDF processed and metrics updated"

    except Exception as exc:
        job.status = JobStatus.failed
        job.message = f"Ingestion failed: {exc}"

    job.finished_at = datetime.utcnow()
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
