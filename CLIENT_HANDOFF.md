# Asas Financials Extraction Workspace — Handoff

## Overview

This repository is the extraction-focused workspace originally used during the early Asas Financials MVP phase. The main platform work has since moved to a separate repository. This codebase remains useful for PDF/Excel extraction experiments, sample data review, ingestion checks, and validation of financial statement parsing logic.

The repository should be treated as a development and research workspace, not as the primary product application. It contains extraction scripts, parsing prototypes, sample source files, intermediate outputs, and an older FastAPI-based MVP shell that can still be used locally to test upload and ingestion flows.

## Current Stack

- Language: Python
- Data processing: extraction scripts, parsing utilities, and ingestion modules
- PDF/table parsing: `pdfplumber` and related extraction utilities
- Excel support: `openpyxl`
- Optional local API shell: FastAPI, SQLAlchemy, SQLite

## Main Repository Areas

### PDF and Excel extraction

The main value of this repository is the extraction work: parsing annual reports, comparing extracted values against Excel files, normalizing financial metrics, and producing validation/debug outputs.

### Ingestion checks

The older FastAPI app includes a local PDF upload and ingestion flow. This can be used for local testing, but it is not the current production platform. The current product repository should be treated as the source of truth for platform UI/API development.

### Extraction prototypes

The repository includes multiple extraction approaches and test outputs. Some modules are experimental and were kept to compare parsing strategies across different company reports and filing formats.

### Sample data and outputs

Sample PDFs, Excel files, and generated outputs are included for development reference. Before sharing or deploying anywhere, decide which sample files are still needed and which should be removed or moved to external storage.

## Local Usage

Set up a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the local API shell if needed:

```bash
uvicorn app.api.main:app --reload
```

Open locally:

- Local app shell: `http://127.0.0.1:8000/`
- API docs: `http://127.0.0.1:8000/docs`

## Environment Variables

The local app can run with defaults for development, but the following variables may be configured:

- `DATABASE_URL` — database connection string. Defaults to local SQLite.
- `APP_SECRET_KEY` — JWT signing secret. Should be changed outside local development.
- `ACCESS_TOKEN_EXPIRE_MINUTES` — token lifetime.

Do not commit local `.env` files with real secrets.

## Important Directories

- `app/` — older local FastAPI MVP shell and ingestion integration code
- `app/api/` — API routes and local application entrypoint
- `app/ingestion/` — upload and ingestion workflow
- `app/storage/` — database setup and models
- `app/validation/` — data quality checks
- `data/` — local database files and uploaded files for development
- `docs/` — project notes, extraction findings, and technical planning documents
- `excel/` — sample Excel source files
- `pdf/`, `pdf-samples/` — sample PDF inputs used for testing
- `robust_pdf_extraction/` — extraction prototype and tests
- `scripts/` — utilities, probes, and data checks
- `output/` — generated extraction and validation artifacts

## Known Limitations

- This is not the current main platform repository.
- PDF extraction is not yet production-grade across all company/report layouts.
- Some extraction utilities are format-specific and require further generalization.
- Generated outputs should be reviewed before being reused as source data.
- Local SQLite databases and sample files may contain development-only state.
- The older API/dashboard shell is retained mainly for local testing and historical context.

## Recommended Next Steps

1. Keep only the sample PDFs/Excel files that are needed for regression testing.
2. Add a controlled regression set for PDF extraction with expected outputs.
3. Document the accepted output schema for extracted financial statements.
4. Keep platform/UI work in the separate current product repository.

## Transfer Notes

For a clean transfer, this repository should be positioned as the extraction workspace only. The separate platform repository should be used for product UI, user flows, deployment, and ongoing application development.
