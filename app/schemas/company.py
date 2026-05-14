from __future__ import annotations

from pydantic import BaseModel


class CompanyCreateRequest(BaseModel):
    name: str
    ticker: str
    sector: str = "Unknown"


class CompanyResponse(BaseModel):
    id: int
    name: str
    ticker: str
    sector: str
