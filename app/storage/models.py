from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.storage.database import Base


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class JobStatus(str, enum.Enum):
    uploaded = "uploaded"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.user, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ticker: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    sector: Mapped[str] = mapped_column(String(120), nullable=False, default="Unknown")

    metrics: Mapped[list[FinancialMetric]] = relationship("FinancialMetric", back_populates="company", cascade="all, delete-orphan")
    jobs: Mapped[list[IngestionJob]] = relationship("IngestionJob", back_populates="company", cascade="all, delete-orphan")
    validations: Mapped[list[ValidationResult]] = relationship("ValidationResult", back_populates="company", cascade="all, delete-orphan")
    tier2_metrics: Mapped[list["Tier2Metric"]] = relationship("Tier2Metric", back_populates="company", cascade="all, delete-orphan")


class FinancialMetric(Base):
    __tablename__ = "financial_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    statement: Mapped[str] = mapped_column(String(80), nullable=False)
    metric: Mapped[str] = mapped_column(String(160), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="SAR")
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="ingestion")

    company: Mapped[Company] = relationship("Company", back_populates="metrics")


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.uploaded, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    company: Mapped[Company] = relationship("Company", back_populates="jobs")


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True, nullable=False)
    total_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mismatched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    match_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    company: Mapped[Company] = relationship("Company", back_populates="validations")


class Tier2Metric(Base):
    __tablename__ = "tier2_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True, nullable=False)
    metric_name: Mapped[str] = mapped_column(String(160), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    calculation_method: Mapped[str] = mapped_column(String(255), nullable=True)
    source_metrics: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    company: Mapped[Company] = relationship("Company", back_populates="tier2_metrics")
