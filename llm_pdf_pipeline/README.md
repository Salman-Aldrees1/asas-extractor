# llm_pdf_pipeline (v2)

Two-pass LLM extractor that turns an audited annual-report PDF into an xlsx
mirroring the reference contract `Almajed_FY2025_Financial_Data_updated.xlsx`
(README + Master Data + per-statement filtered views + Notes & Disclosures +
Standard CoA).

Cross-company comparability comes from the fixed Standard CoA in
`taxonomy/standard_coa.yaml`, not from runtime "understanding" of the PDF.

## Quick start

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
.venv/bin/pip install -r llm_pdf_pipeline/requirements.txt
.venv/bin/python -m llm_pdf_pipeline.cli extract Almajed_FY2025_Annual.pdf
```

Outputs under `llm_pdf_pipeline/outputs/`:

- `<pdf>__data.xlsx` — the deliverable (10 sheets).
- `<pdf>__primary.json` — Pass-1 raw output (face statements).
- `<pdf>__notes.json` — Pass-2 raw output (notes hierarchy).
- `<pdf>__raw.json` — combined debug payload + flat Master Rows.
- `<pdf>__unmapped.json` — rows the LLM couldn't map to a CoA code + sanity-check warnings.
- `<pdf>__cost.json` — token + USD usage per call.
- `coa_cache.json` — persistent fallback-mapping cache.

## Pipeline

1. **Pass 1 — `extract_primary.py`**: one LLM call. Input: row-aware PDF text + Standard CoA. Output: every face line of BS / IS / CF / Equity with `note`, both periods, and `std_code`.
2. **Pass 2 — `extract_notes.py`**: one LLM call. Input: PDF text + Pass-1 result + CoA. Output: every note hierarchically, each row anchored to a face caption (`parent_face`) with `Std Parent Code` + `Std Item Code`. Includes re-anchoring rules for segments, RP, dividends, IPO, capital commitments, fair value, etc.
3. **Pass 3 (optional) — `coa_mapper.py`**: batched fallback mapping for any row where the LLM left `std_code=null`. Cached on disk.
4. **`builder.py`**: merges Pass 1 + Pass 2 into a flat list of 15-column `MasterRow`s and runs lightweight sanity checks (BS identity, GP identity, CF↔BS cash tie).
5. **`xlsx_writer.py`**: writes the 10-sheet workbook.

## Schema

15 columns (xlsx):

| # | Column           | Notes                                                  |
|---|------------------|--------------------------------------------------------|
| A | Parent Statement | Balance Sheet / Income Statement / Cash Flow Statement / Statement of Changes in Equity / Disclosure |
| B | Parent Section   | e.g. "Non-Current Assets"                              |
| C | Parent Caption   | exact face caption                                     |
| D | Std Parent Code  | from `standard_coa.yaml`                               |
| E | Std Parent Name  | from `standard_coa.yaml`                               |
| F | Note Number      | "5", "6.1", "32(a)"                                    |
| G | Note Section     | "NBV by Class", "Movement", "Detail", ...              |
| H | Note Sub-Section | optional                                               |
| I | Line Item        | company-specific caption verbatim                      |
| J | Std Item Code    | from `standard_coa.yaml`                               |
| K | Std Item Name    | from `standard_coa.yaml`                               |
| L | Cross-Reference  | other CoA codes this row also relates to              |
| M | Row Type         | Primary / Note Detail / Disclosure                     |
| N | <FY current>     | numeric (IS/CF expenses negative; BS natural sign)     |
| O | <FY prior>       | numeric                                                |

## Standard CoA

`taxonomy/standard_coa.yaml` is the cross-company key. Treated as immutable:
the LLM picks codes from this list verbatim or returns null. Unknown captions
land in `__unmapped.json` for review — do NOT auto-extend the taxonomy.
