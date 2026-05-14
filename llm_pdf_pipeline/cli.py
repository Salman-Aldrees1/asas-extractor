"""CLI entrypoint.

    .venv/bin/python -m llm_pdf_pipeline.cli extract <pdf_path>
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .pipeline.orchestrator import extract_pdf


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "outputs"


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=True)
    parser = argparse.ArgumentParser(prog="llm_pdf_pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="Run end-to-end extraction on a PDF")
    p.add_argument("pdf", help="Path to PDF (relative to repo root or absolute)")
    p.add_argument("-o", "--output-dir", default=str(DEFAULT_OUTPUT))
    p.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = (REPO_ROOT / pdf_path).resolve()
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    summary = extract_pdf(pdf_path, args.output_dir)
    print()
    print("=" * 60)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
