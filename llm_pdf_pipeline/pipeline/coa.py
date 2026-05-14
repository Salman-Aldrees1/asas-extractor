"""Standard Chart of Accounts loader + lookup helpers."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


COA_PATH = Path(__file__).resolve().parent.parent / "taxonomy" / "standard_coa.yaml"


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    category: str
    statement: str


class CoA:
    def __init__(self, accounts: list[Account]):
        self.accounts = accounts
        self._by_code = {a.code: a for a in accounts}

    def __contains__(self, code: str) -> bool:
        return code in self._by_code

    def get(self, code: str | None) -> Account | None:
        if not code:
            return None
        return self._by_code.get(code)

    def name_for(self, code: str | None) -> str:
        a = self.get(code)
        return a.name if a else ""

    def codes_for_statement(self, statement: str) -> list[Account]:
        return [a for a in self.accounts if a.statement == statement]

    def render_for_prompt(self) -> str:
        """Compact tabular rendering for inclusion in LLM prompts."""
        lines = ["code\tname\tcategory\tstatement"]
        for a in self.accounts:
            lines.append(f"{a.code}\t{a.name}\t{a.category}\t{a.statement}")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def load_coa(path: Path | str | None = None) -> CoA:
    p = Path(path) if path else COA_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    accounts = [Account(**row) for row in data.get("accounts", [])]
    return CoA(accounts)
