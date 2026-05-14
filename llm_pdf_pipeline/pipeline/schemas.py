"""Pydantic schemas for v2 pipeline.

Two LLM passes produce these structured payloads. They are merged into a
flat list of `MasterRow`s by `builder.py` for the xlsx writer.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


StatementKind = Literal[
    "Balance Sheet",
    "Income Statement",
    "Cash Flow Statement",
    "Statement of Changes in Equity",
    "Disclosure",
]

RowType = Literal["Primary", "Note Detail", "Disclosure"]


# -------- Pass 1: face statements --------

class PrimaryRow(BaseModel):
    """One line on the face of a primary statement."""
    section: str = ""                      # e.g. "Non-Current Assets"
    caption: str                           # company-specific caption verbatim
    note: Optional[str] = None             # "5", "6.1", "32(a)" or None
    value_current: Optional[float] = None
    value_prior: Optional[float] = None
    std_code: Optional[str] = None         # Standard CoA code or None


class PrimaryExtraction(BaseModel):
    company: str = ""
    currency: str = "SAR"
    units_multiplier: int = 1              # 1 / 1_000 / 1_000_000
    period_current_label: str              # "2025" / "FY2025" / "31 December 2025"
    period_prior_label: str
    approval_date: str = ""
    balance_sheet: list[PrimaryRow] = Field(default_factory=list)
    income_statement: list[PrimaryRow] = Field(default_factory=list)
    cash_flow: list[PrimaryRow] = Field(default_factory=list)
    equity: list[PrimaryRow] = Field(default_factory=list)


# -------- Pass 2: notes --------

class ParentAnchor(BaseModel):
    statement: StatementKind
    caption: str                           # face caption this row anchors to
    std_parent_code: Optional[str] = None  # CoA code of parent


class NoteRow(BaseModel):
    line_item: str
    std_code: Optional[str] = None
    value_current: Optional[float] = None
    value_prior: Optional[float] = None
    cross_reference: Optional[str] = None  # other Std Codes related
    parent_anchor: Optional[ParentAnchor] = None  # row-level override (Note 28, equity rollforward)


class NoteSection(BaseModel):
    section: str = ""                      # "NBV by Class", "Movement", "Detail", ...
    sub_section: str = ""
    rows: list[NoteRow] = Field(default_factory=list)


class NoteBlock(BaseModel):
    note_number: str                       # "5", "6.1", "32(a)"
    title: str = ""
    parent_face: list[ParentAnchor] = Field(default_factory=list)
    sections: list[NoteSection] = Field(default_factory=list)
    row_type: RowType = "Note Detail"      # "Disclosure" if no face anchor


class NotesExtraction(BaseModel):
    notes: list[NoteBlock] = Field(default_factory=list)


# -------- Final flat row (xlsx schema) --------

class MasterRow(BaseModel):
    parent_statement: str = ""
    parent_section: str = ""
    parent_caption: str = ""
    std_parent_code: str = ""
    std_parent_name: str = ""
    note_number: str = ""
    note_section: str = ""
    note_sub_section: str = ""
    line_item: str = ""
    std_item_code: str = ""
    std_item_name: str = ""
    cross_reference: str = ""
    row_type: RowType = "Primary"
    value_current: Optional[float] = None
    value_prior: Optional[float] = None

    def as_xlsx_row(self) -> list:
        return [
            self.parent_statement, self.parent_section, self.parent_caption,
            self.std_parent_code, self.std_parent_name,
            self.note_number, self.note_section, self.note_sub_section,
            self.line_item, self.std_item_code, self.std_item_name,
            self.cross_reference, self.row_type,
            self.value_current, self.value_prior,
        ]


XLSX_HEADERS = [
    "Parent Statement", "Parent Section", "Parent Caption",
    "Std Parent Code", "Std Parent Name",
    "Note Number", "Note Section", "Note Subsection",
    "Line Item", "Std Item Code", "Std Item Name",
    "Cross Reference", "Row Type",
]
