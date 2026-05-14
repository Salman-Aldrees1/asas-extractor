# Fail Report — smoke on random PDFs

Пайплайн `llm_pdf_pipeline` v2 (Opus 4.5), прогон на 5 случайных PDF из
`pdf-samples/`. Здесь — только неудачные кейсы и root cause.

| # | PDF | Pages | Rows | BS/IS/CF/Eq | Notes | Status |
|---|---|---:|---:|---|---:|---|
| 1 | `pdf-samples/7606_0_2025-06-17_21-01-51_En.pdf` (Flynas) | 47 | 464 | 31/17/34/4 | 32 | ✅ ok |
| 2 | `pdf-samples/407_0_2025-05-11_16-52-14_En.pdf` (SADAFCO Q1-25) | 25 | 351 | 30/25/38/9 | 15 | ⚠️ 1 sanity warn |
| 3 | `pdf-samples/370_0_2024-05-16_13-53-57_En.pdf` (Ma'aden Q1-24) | 34 | 396 | **0/0/0/0** | 18 | ❌ Pass-1 fail |
| 4 | `pdf-samples/427_0_2024-03-28_15-29-26_En.pdf` (SPCC FY2023) | 42 | 95 | 6/6/**0/0** | 6 | ❌ wrong doc type |
| 5 | `pdf-samples/453_0_2025-03-27_16-39-47_En.pdf` (Fitaihi FY2024) | 107 | 50 | 17/8/**0**/7 | **1** | ❌ wrong doc type |

---

## FAIL-1 · `427_0_2024-03-28_15-29-26_En.pdf` (Southern Province Cement, FY2023)

**Observed:** 95 master rows, `BS=6 IS=6 CF=0 Eq=0`, 6 notes, cost $0.83.

**Root cause:** **Wrong document type — это Board Report, а не Financial Statements.**

Проверил raw text первых страниц:
```
page 1: "BOARD REPORT / FOR FINANCIAL YEAR 2023" (38 chars)
page 2: "Southern Province Cement Company (SPCC) / A Saudi Joint Stock Company..." (231 chars)
page 3: "The Custodian of the Two Holy Mosques / King Salman bin Abdulaziz..." (209 chars)
page 4: Board members list
page 5: Chairman's letter to shareholders
```

Это **годовой отчёт совета директоров** (corporate narrative + MD&A + governance), а не
аудированная отчётность. Full IFRS statements живут в отдельном файле у эмитента.
pdfplumber вытащил всего **68k chars на 42 страницы** (≈1,600 chars/page vs обычные
~3,000) потому что PDF — это в основном narrative с картинками/портретами.

**Что pipeline всё-таки нашёл:** summary-таблички из "Financial Highlights" секции
(BS=6, IS=6 — это свернутые showcase numbers, а не face statements).

**Вывод:** pipeline отработал корректно на том, что было в PDF. Проблема на уровне
выбора документа, не в коде. Для этого issuer-а нужен отдельный PDF с аудированной
финансовой отчётностью.

**Как детектить на входе (не реализовано):** проверять наличие маркеров
`Statement of Financial Position` + `Statement of Cash Flows` + `Independent Auditor's Report`
в первых ~10 страницах; если нет — отклонять с ошибкой "not a financial statements PDF".

---

## FAIL-2 · `453_0_2025-03-27_16-39-47_En.pdf` (Fitaihi Holding Group, FY2024)

**Observed:** 50 master rows, `BS=17 IS=8 CF=0 Eq=7`, **1 note**, cost $1.78.

**Root cause:** **Wrong document type — опять Board Report, 107 страниц, narrative-heavy.**

Проверил:
```
page 3: "The Annual Report of Board of Directors / For the Fiscal Year ending on 31/12/2024"
page 81: Zakat / VAT / GOSI tables (narrative disclosure tables)
page 91: Audit Committee recommendations
page 101: Stakeholder diversity matrix
```

Полноценного `Statement of Cash Flows` / `Statement of Financial Position` в PDF нет
(grep по 107 страницам — 0 попаданий на "statement of cash flows" и "cash flows from operating").
Есть разрозненные суммарные цифры (BS=17 и IS=8 Pass-1 нашёл) плюс одна частичная таблица,
которую LLM распознал как Note. Отсюда крайне скудная выдача.

**Почему 102k input tokens, но output всего 3k:** LLM корректно обработал весь PDF
текст, но просто не нашёл детальных notes — их физически нет в документе. Pass-2 вернул
минимальный валидный ответ.

**Вывод:** тот же кейс что 427 — Board Report вместо FS. Код отработал штатно.

---

## FAIL-3 (частичный) · `370_0_2024-05-16_13-53-57_En.pdf` (Ma'aden Q1-2024)

**Observed:** 396 master rows, `BS=0 IS=0 CF=0 Eq=0` **в Pass-1**, но 18 notes с 396 note-rows.
0 sanity warnings (потому что все identities считают 0==0). 8 unmapped. Cost $2.87.

**Root cause:** **Pass-1 пропустил face statements**, но Pass-2 корректно извлёк note detail.

Это **interim condensed financial statements Q1 2024** (3-month period ending 31 March 2024).
Possible причины:
1. **Формат condensed statements.** В interim IFRS-отчётах face statements часто печатаются
   в очень сжатом виде (1-2 страницы), без section headers "ASSETS"/"LIABILITIES"/"EQUITY".
   Промпт Pass-1 сейчас опирается на классические разделы full-year audited.
2. **Расположение в PDF.** Face statements могут быть между страниц 3-8 (после auditor's
   report), а Pass-1 отдаёт всё 50k chars — LLM мог "не заметить" краткие statements на фоне
   20+ страниц нот.
3. **Output truncation не виноват** — tokens_out=разумное число.

**Последствия для выдачи:** 
- `Primary (Face) Only: 0` — главная face-витрина пустая
- `Balance Sheet (Linked): 289` — полно note detail но без face anchors к ним
- `Cash Flow (Linked): 0`, `Equity (Linked): 3` — тоже пусто
- Цифры в note rows правдоподобны и std_code проставлены, но без face-рядов bolt-on
  analytics ломается (нельзя построить cross-company comparison по face captions).

**Фикс-кандидаты (не делал, как просил):**
- Усилить Pass-1 промпт: "If condensed/interim format, still emit every face line including sub-totals."
- Добавить детектор interim в orchestrator (если `period_current_label` содержит "March" или номер квартала → поднять temperature/добавить guidance).
- Либо ретрай Pass-1 с явным указанием страниц face statements (нужен pre-scan по markers).

---

## Резюме

| Категория | Кейсов | Доля |
|---|---:|---|
| Pipeline works (full extraction) | 2 (Flynas, SADAFCO) | 40% |
| Pipeline partial (Pass-1 miss on interim format) | 1 (Ma'aden Q1) | 20% |
| Wrong document type (Board Report instead of FS) | 2 (SPCC, Fitaihi) | 40% |

**Истинно pipeline-баг:** 1 из 5 (Ma'aden interim condensed). Остальные — data-selection issue.

**Рекомендации (не выполнены):**
1. **Input validation** в orchestrator: отказывать если в PDF нет `Independent Auditor's Report` + `Statement of Financial Position` markers. Сразу отсекает Board Reports.
2. **Interim support** в Pass-1 промпте: явное guidance по condensed format + 3-month/6-month/9-month период.
3. **Selection helper utility**: скрипт `scripts/filter_annual_statements.py` для массового фильтра PDFs по markers перед batch-processing.
