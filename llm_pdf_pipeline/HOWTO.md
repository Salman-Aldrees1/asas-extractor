# HOWTO: сгенерировать Excel из PDF для новой компании

Инструкция написана так, чтобы ты мог прогнать 10+ компаний × 1–5 лет **без
участия ассистента**. Пайплайн полностью самодостаточен, всё что нужно — это
API-ключ Anthropic и PDF-файлы.

Совместим с импортером Lovable (см. `../EXCEL_GENERATION_RULES.md` в корне
репо): все правила §5.1–§5.3 (уникальность `Std Item Code`, axis-строки без
кода, note-detail с собственным кодом) соблюдаются автоматически на стороне
`builder.py`.

---

## 0. Одноразовая подготовка окружения

```bash
# из корня репо salman_fin_platform
python3 -m venv .venv                     # если ещё нет
.venv/bin/pip install -r llm_pdf_pipeline/requirements.txt

# .env в корне репо (НЕ в llm_pdf_pipeline/)
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
```

Проверка:

```bash
.venv/bin/python -m llm_pdf_pipeline.cli --help
```

## 1. Организация входных PDF

Рекомендация: одна папка на компанию, PDF с именем, содержащим год отчёта.

```
pdf-sources/
  flynas/
    flynas_FY2023.pdf
    flynas_FY2024.pdf
    flynas_FY2025.pdf
  acwa/
    acwa_FY2024.pdf
    ...
```

Имя PDF — **stem** — используется как префикс всех выходных файлов
(`<stem>__data.xlsx`, `<stem>__primary.json`, `<stem>__notes.json` и т. д.).
Поэтому желательно, чтобы оно было информативным и уникальным.

## 2. Запуск на один PDF

```bash
.venv/bin/python -m llm_pdf_pipeline.cli extract \
    pdf-sources/flynas/flynas_FY2024.pdf \
    -o llm_pdf_pipeline/outputs/flynas
```

Что произойдёт:

1. Pass 1 (LLM): face-строки BS / IS / CF / Equity → `<stem>__primary.json`
2. Pass 2 (LLM): все ноты иерархически → `<stem>__notes.json`
3. Pass 3 (LLM, batch): fallback CoA-маппинг для строк без кода → дописывается
   в те же rows + `coa_cache.json` в той же папке.
4. `builder.py`: собирает плоский список `MasterRow`, применяет правила
   уникальности кода, axis-детектор, объединение multi-anchor нот в
   `Cross Reference`.
5. `xlsx_writer.py`: пишет `<stem>__data.xlsx` (10 листов).

Полные логи (`-v`):

```bash
.venv/bin/python -m llm_pdf_pipeline.cli extract PDF -o OUT -v
```

## 3. Что появляется в output-папке

| Файл | Зачем |
|------|-------|
| `<stem>__data.xlsx` | **Результат** — загружать в Lovable |
| `<stem>__primary.json` | Кэш Pass 1. При повторном запуске переиспользуется (LLM не вызывается). |
| `<stem>__notes.json` | Кэш Pass 2. То же самое. |
| `<stem>__raw.json` | Полный дебаг-дамп (primary + notes + master rows). |
| `<stem>__unmapped.json` | Строки без `Std Item Code` (после всех трёх проходов) + sanity-warnings (BS identity, GP identity, CF↔BS cash tie). |
| `<stem>__cost.json` | Токены + $ по вызовам. |
| `coa_cache.json` | Общий кэш Pass 3 для всей папки (делится между файлами). |

## 4. Пакетный прогон на одну компанию

Bash-цикл:

```bash
COMPANY=flynas
for pdf in pdf-sources/$COMPANY/*.pdf; do
  echo "=== $pdf ==="
  .venv/bin/python -m llm_pdf_pipeline.cli extract "$pdf" \
      -o "llm_pdf_pipeline/outputs/$COMPANY"
done
```

Пайплайн кэширует Pass 1 / Pass 2 / Pass 3 на диске, поэтому повторный запуск
того же PDF не тратит токены. Удалять кэш нужно, только если хочешь пересечь
LLM на том же файле.

## 5. Повторный прогон после обновления кода (без LLM)

Если `builder.py` / `xlsx_writer.py` / CoA-taxonomy изменились, Excel
перегенерируется бесплатно, пока есть `__primary.json` и `__notes.json`:

```bash
for pdf in pdf-sources/flynas/*.pdf; do
  .venv/bin/python -m llm_pdf_pipeline.cli extract "$pdf" \
      -o llm_pdf_pipeline/outputs/flynas
done
```

В логе будет:

```
Pass 1: loading cached <stem>__primary.json (delete to force re-run)
Pass 2: loading cached <stem>__notes.json   (delete to force re-run)
```

Чтобы форснуть пересобор одного прохода:

```bash
rm llm_pdf_pipeline/outputs/flynas/<stem>__primary.json    # перезапустит Pass 1 + 2
rm llm_pdf_pipeline/outputs/flynas/<stem>__notes.json      # перезапустит только Pass 2
rm llm_pdf_pipeline/outputs/flynas/coa_cache.json          # перезапустит Pass 3 (маппинг)
```

## 6. Проверка качества перед загрузкой

### 6.1 Sanity warnings

Смотри `<stem>__unmapped.json` → поле `sanity_warnings`. Норма — пустой массив.
Возможные warnings:

- **BS identity** (Assets = Equity + Liabilities) — > 0.01% разницы.
- **IS identity** (GP = Revenue + COR) — > 0.01% разницы.
- **CF↔BS cash tie** (CF ending cash = BS cash) — > 0.01% разницы.

Если warning есть — это сигнал, что LLM что-то пропустил или неправильно
распознал знак. Открой соответствующие сырьевые JSON-ы и / или исходный PDF,
поправь вручную в `<stem>__primary.json` или `<stem>__notes.json` и перегенерь
(пункт 5).

### 6.2 Unmapped rows

Поле `unmapped_rows` — те строки, у которых остался пустой `Std Item Code`
после всех трёх проходов (axis-строки сюда уже не попадают — они так и
задуманы). Если тут десятки строк с осмысленными названиями — значит нужно
расширять `taxonomy/standard_coa.yaml` и пересобирать.

### 6.3 Нет дубликатов `Std Item Code`

`builder.py` в конце сборки пишет в лог:

```
std_item_code collisions: <N>
```

Должно быть 0 (или лога нет вовсе). Это главный контракт с импортером
Lovable (§5.1). Если > 0 — открой `<stem>__raw.json` → `master_rows`,
найди пары строк с одинаковым кодом, разберись почему фоллбек не нашёл
уникальный `<parent>-<slug>`.

### 6.4 Быстрый ручной чек xlsx

```bash
.venv/bin/python - <<'PY'
from openpyxl import load_workbook
from collections import Counter
wb = load_workbook('llm_pdf_pipeline/outputs/flynas/flynas_FY2024__data.xlsx', read_only=True)
ws = wb['Master Data']
rows = list(ws.iter_rows(min_row=2, values_only=True))
codes = [r[9] for r in rows if r[9]]
print('total', len(rows), 'with_code', len(codes), 'unique', len(set(codes)), 'dupes', len(codes) - len(set(codes)))
print('by row type:', Counter(r[12] for r in rows))
PY
```

Должно быть `dupes 0`.

## 7. Специфика «несколько лет на одну компанию»

Каждый PDF должен быть **за один отчётный период** (current + comparative
colon). Не склеивай 3–5 лет в один файл. Импортер Lovable помечает второй
столбец как `is_comparative` и позже перетирает его свежим current из
следующего отчёта.

Типичная цепочка:

- `flynas_FY2023.pdf` → столбцы `2023` (current) + `2022` (comparative)
- `flynas_FY2024.pdf` → `2024` + `2023`
- `flynas_FY2025.pdf` → `2025` + `2024`

После загрузки всех трёх в Lovable в БД окажутся 4 года (2022–2025), причём
2023 и 2024 из самых свежих отчётов, что правильно — там часто цифры
переправлены в reclassifications / restatements.

## 8. Когда и как расширять Standard CoA

`taxonomy/standard_coa.yaml` — **единственный** способ добавить новый тип
метрики. Правила:

- **НЕ добавлять** axis-коды (Additions, Jan 1, и т. п.) — они должны
  оставаться без кода.
- **Добавлять** только настоящие метрики, отсутствующие в текущей таксономии
  (например, для горнодобывающей компании: `BS-NCA-MINE` — Mining assets).
- Код должен соответствовать структуре статьи: `BS-NCA-*`, `IS-*`, `CF-OP-*`
  и т. д. — см. существующие.
- После правки taxonomy — удалить `coa_cache.json` для пострадавшей папки и
  перегенерить (пункт 5) — Pass 3 пересоберётся.

Если добавляешь код — **переимпортируй всю историю компании** (2–5 лет
сразу), чтобы коды совпадали между годами (§5.4 правил Lovable).

## 9. Типовые проблемы и что делать

| Симптом | Причина | Фикс |
|---------|---------|------|
| `CF↔BS cash tie` warning | LLM не включил bank deposits в `BS-CA-CASH`, или наоборот разнёс `Restricted cash` | Открой `<stem>__primary.json`, проверь `balance_sheet` → `BS-CA-CASH` и `BS-CA-FA` (short-term deposits). Поправь вручную, перегенери. |
| `BS identity` warning | Пропущена строка на face BS, или сумматоры `BS-EQ-TOT`/`BS-L-TOT`/`BS-A-TOT` неверно извлечены | Аналогично — `<stem>__primary.json` → `balance_sheet`. |
| Много `unmapped_rows` с осмысленными названиями | В CoA нет такого типа метрики | Расширить `taxonomy/standard_coa.yaml`, удалить `coa_cache.json`, перегенерить. |
| `std_item_code collisions: N > 0` | Redder: builder не смог сделать уникальный код (возможно один и тот же `line_item` у двух строк с одним parent) | Посмотри collision-пары в логе; обычно означает, что LLM выдал две идентичные строки. Открой `__notes.json`, убери дубликат. |
| Excel сохранён с именем типа `IS-FIN__data.xlsx` | Баг в коде (был когда-то с перетиранием переменной `base`) | Обнови код, `git pull`. |
| Pass 3 overloaded_error от Anthropic | Серверы перегружены | Повторить через 30 секунд — кэш уже частично применён, Pass 3 догонит остальное. |
| Строка на face BS имеет кривое название / неверный знак | Row-aware PDF парсер иногда режет длинные captions | `<stem>__primary.json` → поправь вручную → перегенерь (Pass 1 кэш заберёт твою правку). |

## 10. Чек-лист перед отдачей Excel в Lovable

- [ ] `<stem>__unmapped.json` → `sanity_warnings` пустой или warnings разобраны.
- [ ] `unmapped_rows` — только axis-адьячные или реально отсутствующие в CoA.
- [ ] В логе нет `std_item_code collisions: N > 0`.
- [ ] В xlsx заголовки двух годовых колонок — строго `YYYY` (никаких `FY2024`).
- [ ] Валюта и units в README-листе совпадают с PDF.
- [ ] Approval date вытащен (строка `Approval Date:` в README).

## 11. Что под капотом (для справки)

Структура пакета:

```
llm_pdf_pipeline/
  cli.py                      # argparse entrypoint
  pipeline/
    orchestrator.py           # 3-пасс + сборка + xlsx
    extract_primary.py        # Pass 1 prompt + schema
    extract_notes.py          # Pass 2 prompt + schema
    coa_mapper.py             # Pass 3 batched fallback
    builder.py                # merge + axis detection + unique-code policy
    xlsx_writer.py            # 10-sheet workbook
    coa.py / schemas.py / llm_client.py / pdf_utils.py
  taxonomy/
    standard_coa.yaml         # фиксированная CoA
  outputs/                    # куда всё льётся по умолчанию
```

Ключевые константы/функции:

- `builder._AXIS_PATTERNS` — регексы для определения axis-строк. Если в новой
  компании появляются axis-паттерны, которых нет в списке (например,
  специфический банковский rollforward), дописать сюда — и перегенерить (без
  LLM, бесплатно).
- `builder._unique_code()` — алгоритм `<parent>-<slug>` для дизамбигуации.
- `orchestrator.extract_pdf()` — оркестратор, читать начинать отсюда.

## 12. Полный пример end-to-end

```bash
# 1. подготовка (одноразово)
python3 -m venv .venv
.venv/bin/pip install -r llm_pdf_pipeline/requirements.txt
echo "ANTHROPIC_API_KEY=sk-ant-xxx" >> .env

# 2. разложить PDF
mkdir -p pdf-sources/newco
cp ~/Downloads/NewCo_FY2024_Annual_Report.pdf pdf-sources/newco/newco_FY2024.pdf

# 3. прогон
.venv/bin/python -m llm_pdf_pipeline.cli extract \
    pdf-sources/newco/newco_FY2024.pdf \
    -o llm_pdf_pipeline/outputs/newco

# 4. проверка
cat llm_pdf_pipeline/outputs/newco/newco_FY2024__unmapped.json | python3 -m json.tool | head -40

# 5. если sanity clean — грузить в Lovable
open llm_pdf_pipeline/outputs/newco/newco_FY2024__data.xlsx
```

Стоимость одного прогона: обычно $0.20–0.50 за PDF на 50–80 страниц
(Claude Sonnet), плюс Pass 3 (кэшируется между файлами одной компании, так
что второй и третий год практически бесплатны на Pass 3).
