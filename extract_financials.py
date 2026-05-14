from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pdfplumber
from difflib import SequenceMatcher


ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "pdf"
EXCEL_DIR = ROOT / "excel"
OUTPUT_DIR = ROOT / "output"

NUM_TOKEN_RE = r"\(?-?\d[\d,]*(?:\.\d+)?\)?"


def pdf_to_text(pdf_path: Path, raw: bool = True) -> str:
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
    return "\n".join(text_parts)


def normalize_line(line: str) -> str:
    clean = line.replace("\u00ad", "").replace("�", "")
    clean = clean.replace("¤", "").replace("\x0c", " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def normalize_label(label: str) -> str:
    label = normalize_line(label).lower()
    label = re.sub(r"[^a-z0-9 ]+", " ", label)
    label = re.sub(r"\s+", " ", label).strip()
    return label


def parse_number(token: str) -> float | int | None:
    if token is None:
        return None
    t = token.strip()
    if not t:
        return None
    negative = t.startswith("(") and t.endswith(")")
    t = t.replace("(", "").replace(")", "")
    t = t.replace(",", "")
    if t in {"-", "--"}:
        return None
    try:
        val = float(t)
    except ValueError:
        return None
    if negative:
        val = -val
    if abs(val - round(val)) < 1e-9:
        return int(round(val))
    return val


def extract_section(text: str, start_marker: str, end_markers: list[str]) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    end_positions = [text.find(marker, start + len(start_marker)) for marker in end_markers]
    end_positions = [p for p in end_positions if p >= 0]
    end = min(end_positions) if end_positions else len(text)
    return text[start:end]


def extract_rows_with_two_values(section_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending: list[str] = []

    row_re = re.compile(rf"^(.*?)(?P<v1>{NUM_TOKEN_RE})\s+(?P<v2>{NUM_TOKEN_RE})\s*$")

    for raw_line in section_text.splitlines():
        line = normalize_line(raw_line)
        if not line:
            continue
        if re.fullmatch(r"\d+", line):
            continue

        match = row_re.match(line)
        if match:
            label_part = match.group(1).strip()
            v1 = parse_number(match.group("v1"))
            v2 = parse_number(match.group("v2"))

            label_part = re.sub(r"\s+\d+(?:\.\d+)?\s*$", "", label_part).strip(" -:")
            if pending:
                if label_part:
                    label = " ".join(pending + [label_part]).strip()
                else:
                    label = " ".join(pending).strip()
            else:
                label = label_part
            pending = []

            if label and v1 is not None and v2 is not None:
                rows.append(
                    {
                        "label": label,
                        "value_1": v1,
                        "value_2": v2,
                    }
                )
            continue

        lower = line.lower()
        if lower.startswith("statement of") or lower.startswith("for the year ended") or lower.startswith("as at"):
            pending = []
            continue
        if lower in {"assets", "equity and liabilities", "equity", "current assets", "non-current assets", "operating activities:", "adjustments for:", "investing activities:", "financing activities:"}:
            pending = []
            continue

        pending.append(line)

    return rows


def select_metrics(rows: list[dict[str, Any]], aliases: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}

    for row in rows:
        norm = normalize_label(row["label"])
        for key, key_aliases in aliases.items():
            if key in selected:
                continue
            if any(alias in norm for alias in key_aliases):
                selected[key] = row
                break

    return selected


def extract_pdf_2023_audited(text: str) -> dict[str, Any]:
    bs_section = extract_section(
        text,
        "STATEMENT OF FINANCIAL POSITION",
        ["STATEMENT OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME"],
    )
    is_section = extract_section(
        text,
        "STATEMENT OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
        ["STATEMENT OF CHANGES IN EQUITY"],
    )
    cf_section = extract_section(
        text,
        "STATEMENT OF CASH FLOWS",
        ["NOTES TO THE FINANCIAL STATEMENTS", "1. LEGAL STRUCTURE"],
    )

    bs_rows = extract_rows_with_two_values(bs_section)
    is_rows = extract_rows_with_two_values(is_section)
    cf_rows = extract_rows_with_two_values(cf_section)

    bs_aliases = {
        "property_and_equipment": ["property and equipment"],
        "right_of_use_assets": ["right of use assets"],
        "intangible_assets": ["intangible assets"],
        "fvoci_assets": ["financial assets at fair value"],
        "total_non_current_assets": ["total non current assets"],
        "inventories": ["inventories"],
        "trade_receivables": ["trade receivables"],
        "due_from_related_parties": ["due from related parties"],
        "prepayments_and_other_debit_balances": ["prepayments and other debit balance"],
        "cash_and_cash_equivalents": ["cash and cash equivalents"],
        "total_current_assets": ["total current assets"],
        "total_assets": ["total assets"],
        "total_equity": ["total equity"],
        "total_non_current_liabilities": ["total non current liabilities"],
        "total_current_liabilities": ["total current liabilities"],
        "total_liabilities": ["total liabilities"],
        "total_equity_and_liabilities": ["total equity and liabilities"],
    }

    is_aliases = {
        "revenue": ["revenue"],
        "cost_of_revenue": ["cost of revenue"],
        "gross_profit": ["gross profit"],
        "selling_and_marketing_expenses": ["selling and marketing expenses"],
        "general_and_administrative_expenses": ["general and administrative expenses"],
        "operating_profit": ["operating profit"],
        "other_income": ["other income"],
        "finance_costs": ["finance costs"],
        "profit_before_zakat": ["profit for the year before zakat"],
        "zakat_expense": ["zakat expense"],
        "net_profit": ["net profit for the year"],
    }

    cf_aliases = {
        "net_profit_before_zakat": ["net profit before zakat"],
        "net_cash_from_operating": ["net cash flows generated from operating activities"],
        "net_cash_used_in_investing": ["net cash flows used in investing activities"],
        "net_cash_used_in_financing": ["net cash flows used in financing activities"],
        "cash_end_of_year": ["cash and cash equivalents at the end of the year"],
    }

    bs = select_metrics(bs_rows, bs_aliases)
    income = select_metrics(is_rows, is_aliases)
    cash_flow = select_metrics(cf_rows, cf_aliases)

    def to_years(selected: dict[str, dict[str, Any]], year1: str, year2: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, row in selected.items():
            out[key] = {
                year1: row["value_1"],
                year2: row["value_2"],
                "source_label": row["label"],
            }
        return out

    return {
        "periods": ["2023", "2022"],
        "currency": "SAR",
        "source_type": "audited_financial_statements",
        "statement_of_financial_position": to_years(bs, "2023", "2022"),
        "statement_of_profit_or_loss": to_years(income, "2023", "2022"),
        "statement_of_cash_flows": to_years(cash_flow, "2023", "2022"),
    }


def fuzzy_match(label: str, aliases: list[str], threshold: float = 0.6) -> bool:
    """Check if label matches any alias using fuzzy string matching."""
    norm_label = normalize_label(label)
    for alias in aliases:
        norm_alias = normalize_label(alias)
        ratio = SequenceMatcher(None, norm_label, norm_alias).ratio()
        if ratio >= threshold:
            return True
    return False


def extract_generic_tables(pdf_path: Path) -> dict[str, Any]:
    """Extract financial data from any PDF using generic table extraction."""
    metric_aliases = {
        "revenue": ["revenue", "sales", "turnover", "income", "total revenue"],
        "gross_profit": ["gross profit", "gross margin"],
        "operating_profit": ["operating profit", "operating income", "ebit", "ebitda"],
        "net_profit": ["net profit", "net income", "profit after tax", "earnings"],
        "total_assets": ["total assets", "assets"],
        "total_liabilities": ["total liabilities", "liabilities"],
        "total_equity": ["total equity", "shareholders equity", "equity"],
        "cash_and_equivalents": ["cash", "cash and cash equivalents", "cash equivalents"],
        "operating_cash_flow": ["operating cash flow", "cash from operations"],
        "investing_cash_flow": ["investing cash flow", "cash from investing"],
        "financing_cash_flow": ["financing cash flow", "cash from financing"],
    }

    extracted: dict[str, Any] = {
        "statement_of_profit_or_loss": {},
        "statement_of_financial_position": {},
        "statement_of_cash_flows": {},
    }

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Find header row (first row with mostly text)
                header_row = None
                for row in table:
                    if row and any(isinstance(cell, str) and cell.strip() for cell in row):
                        header_row = row
                        break

                if not header_row:
                    continue

                # Extract years from header
                years = []
                for cell in header_row:
                    if isinstance(cell, str):
                        year_match = re.search(r"20\d{2}", cell)
                        if year_match:
                            years.append(int(year_match.group()))

                # Process data rows
                for row in table:
                    if not row or len(row) < 2:
                        continue

                    label_cell = row[0]
                    if not isinstance(label_cell, str):
                        continue

                    label = normalize_line(label_cell)

                    # Match against metric aliases
                    for metric_key, aliases in metric_aliases.items():
                        if fuzzy_match(label, aliases):
                            # Extract numeric values
                            values = {}
                            for i, cell in enumerate(row[1:]):
                                if i >= len(years):
                                    break
                                val = parse_number(str(cell)) if cell else None
                                if val is not None:
                                    year = years[i] if i < len(years) else 2023
                                    values[str(year)] = val

                            if values:
                                # Determine which statement this belongs to
                                if metric_key in ["revenue", "gross_profit", "operating_profit", "net_profit"]:
                                    extracted["statement_of_profit_or_loss"][metric_key] = values
                                elif metric_key in ["total_assets", "total_liabilities", "total_equity", "cash_and_equivalents"]:
                                    extracted["statement_of_financial_position"][metric_key] = values
                                elif metric_key in ["operating_cash_flow", "investing_cash_flow", "financing_cash_flow"]:
                                    extracted["statement_of_cash_flows"][metric_key] = values

    return {
        "periods": sorted(set(str(y) for v in extracted.values() for y in v.get("_years", []))),
        "currency": "SAR",
        "source_type": "generic_table_extraction",
        "statement_of_profit_or_loss": extracted["statement_of_profit_or_loss"],
        "statement_of_financial_position": extracted["statement_of_financial_position"],
        "statement_of_cash_flows": extracted["statement_of_cash_flows"],
    }


def extract_text_based(text: str) -> dict[str, Any]:
    """Extract financial data from text using regex patterns."""
    metric_aliases = {
        "revenue": ["sales", "revenue", "turnover", "income"],
        "gross_profit": ["gross profit", "gross margin"],
        "operating_profit": ["operating profit", "operating income"],
        "net_profit": ["net profit", "net income", "profit after tax"],
    }

    extracted: dict[str, Any] = {
        "statement_of_profit_or_loss": {},
        "statement_of_financial_position": {},
        "statement_of_cash_flows": {},
    }

    lines = text.splitlines()
    
    # Auto-detect years from text
    years = []
    for line in lines[:50]:  # Check first 50 lines
        year_matches = re.findall(r"20\d{2}", line)
        years.extend([int(y) for y in year_matches])
    
    # Get unique years, sort descending
    unique_years = sorted(set(years), reverse=True)
    if len(unique_years) >= 2:
        year1, year2 = str(unique_years[0]), str(unique_years[1])
    else:
        year1, year2 = "2023", "2022"  # Fallback
    
    # Pattern: Label followed by 2+ numeric values
    pattern = re.compile(rf"^(?P<label>[A-Za-z\s\-\(\)]+)\s+(?P<v1>{NUM_TOKEN_RE})\s+(?P<v2>{NUM_TOKEN_RE})")
    
    for line in lines:
        line = normalize_line(line)
        match = pattern.match(line)
        if not match:
            continue
        
        label = match.group("label").strip()
        v1 = parse_number(match.group("v1"))
        v2 = parse_number(match.group("v2"))
        
        if v1 is None or v2 is None:
            continue
        
        for metric_key, aliases in metric_aliases.items():
            if fuzzy_match(label, aliases):
                extracted["statement_of_profit_or_loss"][metric_key] = {
                    year1: v1,
                    year2: v2,
                    "source_label": label,
                }
                break
    
    return {
        "periods": [year1, year2],
        "currency": "SAR",
        "source_type": "text_based_extraction",
        "statement_of_profit_or_loss": extracted["statement_of_profit_or_loss"],
        "statement_of_financial_position": extracted["statement_of_financial_position"],
        "statement_of_cash_flows": extracted["statement_of_cash_flows"],
    }


def extract_financial_data_smart(pdf_path: Path) -> dict[str, Any]:
    """Try multiple extraction strategies and return the best result."""
    strategies = [
        ("IFRS Audited", lambda: extract_pdf_2023_audited(pdf_to_text(pdf_path))),
        ("Annual Report Summary", lambda: extract_annual_report_2024_summary(pdf_to_text(pdf_path))),
        ("Text-Based", lambda: extract_text_based(pdf_to_text(pdf_path))),
        ("Generic Tables", lambda: extract_generic_tables(pdf_path)),
    ]

    for strategy_name, strategy_func in strategies:
        try:
            result = strategy_func()
            if result and result.get("statement_of_profit_or_loss"):
                print(f"  ✓ {strategy_name} succeeded")
                return result
        except Exception as e:
            print(f"  ✗ {strategy_name} failed: {e}")
            continue

    # If all strategies fail, return empty structure
    print(f"  ✗ All extraction strategies failed")
    return {
        "periods": [],
        "currency": "SAR",
        "source_type": "extraction_failed",
        "statement_of_profit_or_loss": {},
        "statement_of_financial_position": {},
        "statement_of_cash_flows": {},
    }


def extract_annual_report_2024_summary(text: str) -> dict[str, Any]:
    lines = [normalize_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    start_idx = -1
    for i, line in enumerate(lines):
        if "statement 2024 2023 changes" in line.lower():
            start_idx = i
            break

    metrics: dict[str, Any] = {}
    if start_idx >= 0:
        table_lines = lines[start_idx : start_idx + 40]
        row_re = re.compile(
            rf"^(?P<label>[A-Za-z /\-]+?)\s+(?P<v2024>{NUM_TOKEN_RE})\s+(?P<v2023>{NUM_TOKEN_RE})\s+(?P<delta>{NUM_TOKEN_RE})\s+(?P<pct>-?\d+(?:\.\d+)?)%?$"
        )

        label_map = {
            "revenue": "revenue",
            "cost of revenue": "cost_of_revenue",
            "gross profit": "gross_profit",
            "operating profit loss": "operating_profit",
        }

        for line in table_lines:
            m = row_re.match(line)
            if not m:
                continue
            label = normalize_label(m.group("label"))
            if label not in label_map:
                continue
            key = label_map[label]
            metrics[key] = {
                "2024_mn_sar": parse_number(m.group("v2024")),
                "2023_mn_sar": parse_number(m.group("v2023")),
                "delta_mn_sar": parse_number(m.group("delta")),
                "delta_pct": parse_number(m.group("pct")),
                "source_label": m.group("label").strip(),
            }

    return {
        "periods": ["2024", "2023"],
        "currency": "SAR",
        "units": "million",
        "source_type": "annual_report_management_summary",
        "income_statement_summary": metrics,
    }


def read_excel_data() -> dict[str, Any]:
    data: dict[str, Any] = {}

    balance_path = EXCEL_DIR / "al majed oud co ( balance sheet ).xlsx"
    income_path = EXCEL_DIR / "al majed oud co.( income statement ).xlsx"
    cash_path = EXCEL_DIR / "al majed oud co. (cash flow).xlsx"

    # Balance sheet
    bs_df = pd.read_excel(balance_path, sheet_name="Sheet1", header=None)
    bs_headers = bs_df.iloc[0].tolist()
    bs_cols = {h: i for i, h in enumerate(bs_headers) if isinstance(h, str)}
    bs_rows = {str(bs_df.iloc[i, 0]).strip(): i for i in range(len(bs_df)) if pd.notna(bs_df.iloc[i, 0])}

    bs_items = [
        "Total Non-Current Assets",
        "Total Current Assets",
        "Total Assets",
        "Total Equity",
        "Total non-current liabilities",
        "Total Current Liabilities",
        "Total Liabilities",
        "Total Equity and Liabilities",
    ]

    balance_out: dict[str, Any] = {}
    for item in bs_items:
        r = bs_rows[item]
        balance_out[item] = {
            "FY-2022 (Co)": bs_df.iloc[r, bs_cols["FY-2022 (Co)"]],
            "FY-2023": bs_df.iloc[r, bs_cols["FY-2023"]],
            "FY-2024 (Group)": bs_df.iloc[r, bs_cols["FY-2024 (Group)"]],
        }

    # Income statement
    is_df = pd.read_excel(income_path, sheet_name="Sheet 1", header=None)
    is_headers = is_df.iloc[1].tolist()
    is_cols = {h: i for i, h in enumerate(is_headers) if isinstance(h, str)}
    is_rows = {str(is_df.iloc[i, 0]).strip(): i for i in range(len(is_df)) if pd.notna(is_df.iloc[i, 0])}

    is_items = [
        "Revenue",
        "Cost of Revenue",
        "Gross Profit",
        "Operating profit",
        "Net profit for the period before Zakat and Tax",
        "Net profit for the period",
    ]

    income_out: dict[str, Any] = {}
    for item in is_items:
        r = is_rows[item]
        income_out[item] = {
            "FY-2022 (Co)": is_df.iloc[r, is_cols["FY-2022 (Co)"]],
            "FY-2023 (Co)": is_df.iloc[r, is_cols["FY-2023 (Co)"]],
            "FY-2024 (Audited)": is_df.iloc[r, is_cols["FY-2024 (Audited)"]],
            "FY-2023 (Audited-Reclassified)": is_df.iloc[r, is_cols["FY-2023 (Audited-Reclassified)"]],
        }

    # Cash flow
    cf_df = pd.read_excel(cash_path, sheet_name="Sheet1", header=None)
    cf_headers = cf_df.iloc[0].tolist()
    cf_cols = {h: i for i, h in enumerate(cf_headers) if isinstance(h, str)}
    cf_rows = {str(cf_df.iloc[i, 0]).strip(): i for i in range(len(cf_df)) if pd.notna(cf_df.iloc[i, 0])}

    cf_items = [
        "Net profit before Zakat and income tax",
        "Net cash flows generated from operating activities",
        "Net cash flows used in investing activities",
        "Net cash flows used in financing activities",
        "Cash and cash equivalents at the end of the year",
    ]

    cash_out: dict[str, Any] = {}
    for item in cf_items:
        r = cf_rows[item]
        cash_out[item] = {
            "FY-2022": cf_df.iloc[r, cf_cols["FY-2022"]],
            "FY-2023": cf_df.iloc[r, cf_cols["FY-2023"]],
            "FY-2024": cf_df.iloc[r, cf_cols["FY-2024"]],
        }

    data["balance_sheet"] = balance_out
    data["income_statement"] = income_out
    data["cash_flow"] = cash_out
    return data


def compare_values(pdf_val: float | int | None, excel_val: float | int | None, tolerance: float = 0.0) -> dict[str, Any]:
    if pdf_val is None or excel_val is None:
        return {
            "pdf": pdf_val,
            "excel": excel_val,
            "difference": None,
            "status": "missing",
        }

    diff = float(pdf_val) - float(excel_val)
    ok = abs(diff) <= tolerance
    return {
        "pdf": pdf_val,
        "excel": excel_val,
        "difference": diff,
        "status": "match" if ok else "mismatch",
    }


def validate(pdf_data_2024: dict[str, Any], pdf_data_2025: dict[str, Any], excel_data: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {
        "audited_2023_2022_vs_excel": {},
        "annual_report_2024_summary_vs_excel": {},
    }

    # Income statement audited (2023/2022)
    pdf_is = pdf_data_2024["statement_of_profit_or_loss"]
    ex_is = excel_data["income_statement"]

    map_is = {
        "revenue": "Revenue",
        "cost_of_revenue": "Cost of Revenue",
        "gross_profit": "Gross Profit",
        "operating_profit": "Operating profit",
        "profit_before_zakat": "Net profit for the period before Zakat and Tax",
        "net_profit": "Net profit for the period",
    }

    income_checks: dict[str, Any] = {}
    for pdf_key, ex_key in map_is.items():
        if pdf_key not in pdf_is:
            continue
        income_checks[pdf_key] = {
            "2023": compare_values(pdf_is[pdf_key]["2023"], ex_is[ex_key]["FY-2023 (Co)"], 0),
            "2022": compare_values(pdf_is[pdf_key]["2022"], ex_is[ex_key]["FY-2022 (Co)"], 0),
        }

    # Balance sheet audited
    pdf_bs = pdf_data_2024["statement_of_financial_position"]
    ex_bs = excel_data["balance_sheet"]
    map_bs = {
        "total_non_current_assets": "Total Non-Current Assets",
        "total_current_assets": "Total Current Assets",
        "total_assets": "Total Assets",
        "total_equity": "Total Equity",
        "total_non_current_liabilities": "Total non-current liabilities",
        "total_current_liabilities": "Total Current Liabilities",
        "total_liabilities": "Total Liabilities",
        "total_equity_and_liabilities": "Total Equity and Liabilities",
    }

    bs_checks: dict[str, Any] = {}
    for pdf_key, ex_key in map_bs.items():
        if pdf_key not in pdf_bs:
            continue
        bs_checks[pdf_key] = {
            "2023": compare_values(pdf_bs[pdf_key]["2023"], ex_bs[ex_key]["FY-2023"], 0),
            "2022": compare_values(pdf_bs[pdf_key]["2022"], ex_bs[ex_key]["FY-2022 (Co)"], 0),
        }

    # Cash flow audited
    pdf_cf = pdf_data_2024["statement_of_cash_flows"]
    ex_cf = excel_data["cash_flow"]
    map_cf = {
        "net_profit_before_zakat": "Net profit before Zakat and income tax",
        "net_cash_from_operating": "Net cash flows generated from operating activities",
        "net_cash_used_in_investing": "Net cash flows used in investing activities",
        "net_cash_used_in_financing": "Net cash flows used in financing activities",
        "cash_end_of_year": "Cash and cash equivalents at the end of the year",
    }

    cf_checks: dict[str, Any] = {}
    for pdf_key, ex_key in map_cf.items():
        if pdf_key not in pdf_cf:
            continue
        cf_checks[pdf_key] = {
            "2023": compare_values(pdf_cf[pdf_key]["2023"], ex_cf[ex_key]["FY-2023"], 0),
            "2022": compare_values(pdf_cf[pdf_key]["2022"], ex_cf[ex_key]["FY-2022"], 0),
        }

    results["audited_2023_2022_vs_excel"] = {
        "income_statement": income_checks,
        "balance_sheet": bs_checks,
        "cash_flow": cf_checks,
    }

    # Annual report 2024 summary in million SAR vs excel audited/reclassified (also million SAR)
    summary = pdf_data_2025.get("income_statement_summary", {})
    ex_is_2024 = excel_data["income_statement"]
    summary_map = {
        "revenue": "Revenue",
        "cost_of_revenue": "Cost of Revenue",
        "gross_profit": "Gross Profit",
        "operating_profit": "Operating profit",
    }

    summary_checks: dict[str, Any] = {}
    for key, ex_key in summary_map.items():
        if key not in summary:
            continue
        pdf_2024 = summary[key].get("2024_mn_sar")
        pdf_2023 = summary[key].get("2023_mn_sar")
        ex_2024 = round(float(ex_is_2024[ex_key]["FY-2024 (Audited)"]) / 1_000_000, 2)
        ex_2023 = round(float(ex_is_2024[ex_key]["FY-2023 (Audited-Reclassified)"]) / 1_000_000, 2)
        summary_checks[key] = {
            "2024_mn": compare_values(pdf_2024, ex_2024, tolerance=0.02),
            "2023_mn": compare_values(pdf_2023, ex_2023, tolerance=0.02),
        }

    results["annual_report_2024_summary_vs_excel"] = summary_checks

    return results


def build_summary(validation: dict[str, Any]) -> dict[str, Any]:
    total = 0
    matched = 0
    mismatched = 0
    missing = 0

    def walk(obj: Any) -> None:
        nonlocal total, matched, mismatched, missing
        if isinstance(obj, dict):
            if "status" in obj and "pdf" in obj and "excel" in obj:
                total += 1
                if obj["status"] == "match":
                    matched += 1
                elif obj["status"] == "mismatch":
                    mismatched += 1
                else:
                    missing += 1
                return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(validation)
    return {
        "total_checks": total,
        "matched": matched,
        "mismatched": mismatched,
        "missing": missing,
        "match_rate": round((matched / total) * 100, 2) if total else 0,
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_2024_path = PDF_DIR / "6306_0_2024-10-01_16-26-03_En.pdf"
    pdf_2025_path = PDF_DIR / "6306_0_2025-03-27_16-10-52_En.pdf"

    text_2024 = pdf_to_text(pdf_2024_path, raw=True)
    text_2025 = pdf_to_text(pdf_2025_path, raw=True)

    (OUTPUT_DIR / "pdf_2024_raw.txt").write_text(text_2024, encoding="utf-8")
    (OUTPUT_DIR / "pdf_2025_raw.txt").write_text(text_2025, encoding="utf-8")

    extracted_2024 = extract_pdf_2023_audited(text_2024)
    extracted_2025 = extract_annual_report_2024_summary(text_2025)
    excel_data = read_excel_data()

    validation = validate(extracted_2024, extracted_2025, excel_data)
    validation_summary = build_summary(validation)

    extracted_payload = {
        "pdf_2024_10_01": extracted_2024,
        "pdf_2025_03_27": extracted_2025,
    }

    (OUTPUT_DIR / "extracted_financials.json").write_text(
        json.dumps(extracted_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUTPUT_DIR / "excel_financials_snapshot.json").write_text(
        json.dumps(excel_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUTPUT_DIR / "validation_report.json").write_text(
        json.dumps({"summary": validation_summary, "details": validation}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("Extraction and validation completed.")
    print(f"Summary: {validation_summary}")
    print(f"Output files in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
