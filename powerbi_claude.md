# USGI PowerBI Chatbot — Project Brief for Claude

**Upload this file at the start of a chat, then paste your error. It is self-contained —
no filesystem access needed.**

Reply to this user in **Hinglish** (Roman-script Hindi + English). Code, paths, logs and
commands stay in English.

---

## 0. Corrections — do not assume these

If you were told any of the following, it is **wrong** for this project:

| Wrong assumption | Reality |
|---|---|
| SQL Guard uses `sqlglot` | **No sqlglot anywhere.** `backend/agents/sql_guard.py` is regex-based. `sqlglot` is not in `requirements.txt`. |
| Layout is `app/api`, `app/agent`, `app/nl2sql`, `app/guard`, `app/db`, `app/obs`, `ui/` | Actual layout is `backend/{agents,api,core,knowledge,models,retrieval,services}`, `config/`, `frontend/`, `scripts/`, `tests/` |
| Path is `D:\powerbi_chat\v1` | Path varies per machine. Office laptop has the real SQL Server. |
| LLM is only Ollama | There is an **`LLM_PROVIDER` switch**: `ollama` or `gemini` |
| Data is only SQL Server + Excel | There is a **`DATA_SOURCE` switch**: `sqlserver` or `local` (`.csv` **or** `.xlsx`) |
| The LLM only generates SQL | It also writes the insight narrative and optional pandas calc snippets — but **never** re-types a number (see §6) |

---

## 1. What it does

NLP question → SQL → fetch → **insight AND chart (both mandatory)**, over the insurance
table `dbo.May_2` (124 columns). A LangGraph pipeline of 7 agents.

Two independent switches, each one line in `.env`:

| Switch | Values | Effect |
|---|---|---|
| `DATA_SOURCE` | `sqlserver` \| `local` | `local` = `.csv`/`.xlsx` via DuckDB, no ODBC needed |
| `LLM_PROVIDER` | `ollama` \| `gemini` | `ollama` = fully offline; `gemini` sends question + computed figures to Google |

Exactly one backend and one provider is ever constructed — the unselected one is never
imported.

---

## 2. Repo layout (actual)

```
powerbi_chatbot/
├── .env  .env.example  .gitignore  requirements.txt  README.md  CLAUDE.md
├── setup.bat  run_backend.bat  run_frontend.bat
├── backend/
│   ├── __init__.py                 deliberately import-free (see §7 pyarrow)
│   ├── main.py                     FastAPI app + startup dependency checks
│   ├── agents/
│   │   ├── graph.py                LangGraph wiring, MAX_SQL_ATTEMPTS=3, _safe() wrapper
│   │   ├── state.py                AgentState TypedDict
│   │   ├── query_understanding.py  agent 1: normalize/rewrite/filters/route/decompose
│   │   ├── schema_retrieval.py     agent 2: lexical + ChromaDB hybrid column retrieval
│   │   ├── sql_generation.py       agent 3: T-SQL generation + retry with rising temp
│   │   ├── sql_execution.py        agent 4: runs the validated SQL
│   │   ├── calculation_agent.py    agent 5: optional pandas snippet (sandboxed)
│   │   ├── chart_agent.py          agent 6: rule-based chart type + Plotly figure
│   │   ├── insight_agent.py        agent 7: 3-tier number-safe insight
│   │   ├── sql_guard.py            SELECT-only / single-statement / single-table / row cap
│   │   ├── column_guard.py         repairs column identifiers against the live schema
│   │   ├── answer_builder.py       deterministic answers + hallucinated-number rejection
│   │   ├── calc_sandbox.py         AST-validated exec, restricted builtins, timeout
│   │   └── df_utils.py             numeric/date/categorical detection + pick_measure
│   ├── api/                        auth.py, chat.py, health.py
│   ├── core/
│   │   ├── datasource.py           DataSource ABC + SqlServer/LocalFile + normalisation
│   │   ├── db.py                   thin shim over get_datasource()
│   │   ├── llm_provider.py         LLMProvider ABC + Ollama/Gemini + factory
│   │   ├── llm_client.py           thin shim over get_provider() — what agents import
│   │   ├── sql_dialect.py          T-SQL → DuckDB translation (local mode only)
│   │   ├── column_registry.py      resolves any phrase → physical column
│   │   ├── identifiers.py          normalisation + role token sets
│   │   └── cache.py, security.py, logging_config.py
│   ├── knowledge/column_aliases.json    editable column shortcut dictionary
│   ├── models/schemas.py           Pydantic request/response
│   ├── retrieval/                  chroma_store.py, embeddings.py
│   └── services/                   logging_service.py, user_service.py
├── config/settings.py              all config, validated at startup
├── data/                           example_queries.json, schema_metadata.json,
│                                   users.csv, chat_logs.csv, chroma_store/, cache/
├── frontend/streamlit_app.py       the single chat UI
├── scripts/                        setup_chromadb.py, build_column_aliases.py,
│                                   init_data_files.py, test_db_connection.py,
│                                   diagnose_sqlserver.py
└── tests/                          12 pytest files, none need DB/Ollama/ChromaDB
```

Stack: Streamlit → FastAPI → LangGraph → Ollama (`qwen2.5-coder:7b` SQL+insight,
`:3b` router, `nomic-embed-text` embeddings) or Gemini · ChromaDB · pyodbc/DuckDB ·
Plotly · Redis (optional, degrades to in-memory) · Python 3.11 pinned.

---

## 3. Pipeline

```
/chat → query_understanding → schema_retrieval → sql_generation ⇄ sql_execution
      → [calculation] → chart_agent → insight_agent → response
```

- `sql_generation` and `sql_execution` can loop back to `sql_generation` with the error
  attached, capped at `MAX_SQL_ATTEMPTS = 3`. Each retry raises temperature
  (`0.0 → 0.15 → 0.3`) and uses a different seed, because at temp 0 the model returned
  byte-identical SQL three times and burned every retry.
- Every node is wrapped by `_safe()` — an unhandled exception becomes a graceful error
  state, never a crashed request.
- Agents always emit **Microsoft T-SQL**, whichever backend is active. In local mode
  `sql_dialect.py` translates to DuckDB just before execution
  (`TOP n`→`LIMIT n`, `GETDATE()`→`CURRENT_DATE`, `ISNULL`→`COALESCE`, `[x]`→`"x"`,
  `dbo.t`→`t`, plus `FORMAT`/`DATEADD`/`DATEDIFF`/`DATEPART`). String literals are never
  rewritten. An untranslatable construct (`PIVOT`, `TOP n PERCENT`, `CONVERT`, `IIF`, …)
  raises an error **naming that construct** rather than returning different numbers.

**Chart type is rule-based, not LLM-guessed:** date+numeric→line · low-cardinality
category+numeric→bar · contribution route with ≤8 categories→donut · two numerics→scatter ·
explicit request in the question always wins.

---

## 4. THE SQL SERVER BUG (fixed in code, live-verification pending)

**Symptom:** identical question worked on `DATA_SOURCE=local` but on `sqlserver` produced
**no chart** and an **insight with no real figures** — even though rows were fetched fine.

**Cause:** pyodbc returns `decimal.Decimal` for SQL Server `DECIMAL/NUMERIC/MONEY` and
`datetime.date` for `date` columns. pandas cannot map those to a numpy dtype, so the
column lands as `object`. Everything downstream keys off dtype:

```
numeric_columns()  -> []             # Decimal is not is_numeric_dtype
pick_measure()     -> None
chart_agent        -> returns None   # NO CHART
compute_stats()    -> {"row_count": N} only   # model had nothing → WRONG INSIGHT
```

DuckDB returns real numpy dtypes — that is the only reason `local` looked healthy.

Confirmed by the user's own datatype audit of `dbo.May_2` vs the Excel copy:
- **47 columns** are `decimal(38,2)` / `decimal(18,6)` / `decimal(38,6)` / `decimal(18,2)` —
  every measure: `GROSS_PREMIUM`, `NET_PREMIUM`, `TOTAL_SUM_INSURED`, `COMMISSION_AMOUNT`,
  `Sgst_Net_Amount`, `Total_Gst`, `Balance_Amount`, `Gvw`, `PML`, `SHARE_PERCENTAGE`, …
- **4 columns** are SQL `date`: `POLICY_ISSUE_DATE`, `START_DATE`, `EXPIRY_DATE`,
  `VOUCHER_DATE`
- In `may_2.xlsx` those same 47 are stored as **Text**, so the Excel path fails the same way

### The fix

`normalize_result_frame()` in `backend/core/datasource.py`, called by **both** backends
inside `run_select()`:

1. `Decimal` → `float64`
2. `datetime.date` / `datetime` → `datetime64`
3. Numeric **text** → `float64`, **only** when the column NAME marks it a quantity
   (`looks_like_measure()` in `identifiers.py`). This is what stops `POLICY_NO` =
   `"1029156133"` from becoming a float and corrupting an identifier. One non-numeric value
   anywhere in the column and nothing is converted.
4. `repair_column_names()` — SQL Server names an un-aliased `SUM(x)` as `''` (empty
   string); DuckDB names it `sum(x)`. Empty/duplicate names get deterministic ones
   (`column_2`, `total_1`), because an empty name breaks Plotly and groupby.

Supporting fixes:
- `df_utils.pick_measure()` no longer takes the first numeric column. SQL Server keeps
  `BRANCH_OFFICE_CODE`/`POLICY_NO` as real integers, so an identifier used to win. Now
  identifier-named columns score 0 and are never chosen; if *every* numeric column is an
  identifier it returns `None` rather than charting a policy number.
- `df_utils.date_like_columns()` honours a name hint only on non-numeric columns, so a
  measure aliased `premium_this_month` is not used as the time axis.
- `insight_agent` — if `stats` has no `"total"`, the LLM is **skipped entirely** and the
  deterministic template is returned. Narrating an empty statistics dict is exactly how a
  confident wrong insight was produced.
- `identifiers.py` holds the shared token sets (`IDENTIFIER_NAME_TOKENS`,
  `MEASURE_NAME_TOKENS`, `RATIO_NAME_TOKENS`) so the data-source and agent layers can
  never drift apart.

### Debug handle

Every query now logs its dtypes:
```
SQL executed rows=10 truncated=False dtypes={'BRANCH_NAME': 'object', 'total_gross_premium': 'float64'}
```
**A measure showing `object` means the fix is not in effect.** That single line answers
most "no chart / wrong insight" reports.

### Status

Unit-verified (Decimal, date, text, identifier-safety, all 47 columns pass the name gate,
11 `pick_measure` cases). **Not yet confirmed against the live database** — the user is
testing on the office laptop. Do not assume it works end-to-end until they say so.

---

## 5. Hard constraints — violating these breaks the app

### Never install `pyarrow`
On Windows it loads native DLLs that shadow the ones `onnxruntime` needs. A later
`import onnxruntime` raises `DLL load failed while importing onnxruntime_pybind11_state`,
or **non-deterministically hard-crashes the interpreter** with
`Windows fatal exception: access violation`, which no `try/except` can catch. `onnxruntime`
is pulled in by chromadb, so installing pyarrow silently disables semantic retrieval and
can kill the uvicorn worker mid-request.

Consequences to preserve:
- `frontend/streamlit_app.py` renders the preview as HTML via `_render_table()`. **Never**
  `st.dataframe` / `st.table` — both need Arrow.
- Parquet is written by **DuckDB**, never `pandas.to_parquet`.
- `backend/__init__.py` is deliberately import-free. Fixing this by import *order* was
  tried and rejected: it only holds if `backend` is imported before pandas, which nothing
  guarantees.
- `tests/test_import_order.py` fails the build if pyarrow reappears.

### Numbers are never re-typed by a model
Three tiers in `insight_agent.py`:
1. **direct** — single-row scalar result formatted straight from the DataFrame, no LLM
2. **verified narrative** — LLM prose whose every figure is checked against the values it
   was given; an unsupported figure or invented currency symbol rejects it
3. **template** — deterministic fallback

The source columns carry **no currency**, so any `$`/`INR`/`Rs` the model adds is
fabricated and stripped. Temperature: anything producing or restating a number runs at
`0.0`; only free prose uses `LLM_TEXT_TEMPERATURE` (0.2).

This exists because of a real defect: a query returning `660000` was narrated as
`$1,200,000`.

### Ambiguity is refused, never guessed
`column_registry.py` drops any shortcut claimed by two columns. `sum insured` matches both
`TOTAL_SUM_INSURED` and `USGI_SUM_INSURED`, so it resolves to neither and both candidates
are shown. Silently picking one is how a chatbot returns a confidently wrong number.

### SQL safety
`sql_guard.py` (regex, **not sqlglot**): single statement, SELECT/WITH only, no
DDL/DML/EXEC/`xp_`/`sp_`, must reference only the configured table (CTE names allowed),
`TOP N` injected if missing, and a stray trailing `TOP n` after `ORDER BY` is relocated
rather than sent to the DB. `column_guard.py` then repairs identifiers against the live
schema (`[GROSS PREMIUM]` → `[GROSS_PREMIUM]`) — a real failure that used to burn all
three retries.

---

## 6. Commands

```bash
setup.bat                                    # one-time: venv + deps + data files
run_backend.bat                              # uvicorn :8000
run_frontend.bat                             # streamlit :8501

python scripts\test_db_connection.py         # active data source + column list
python scripts\diagnose_sqlserver.py         # 7 PASS/FAIL checks on the chart/insight chain
python scripts\diagnose_sqlserver.py --question "High-performing branch region-wise."
python scripts\build_column_aliases.py       # rebuild column shortcut dictionary
python scripts\setup_chromadb.py             # rebuild semantic index (per LLM provider)

pytest                                       # needs no DB / Ollama / ChromaDB
pytest tests\test_sqlserver_result_shape.py  # regression suite for the bug in §4
```

`GET /health/detailed` reports data source, LLM provider, configured vs available models,
ChromaDB, column registry and cache — each independently.

**After switching `LLM_PROVIDER`, re-run `setup_chromadb.py` once** — collections are
namespaced per provider (`schema_metadata__ollama` / `__gemini`) because the two embedding
spaces are not interchangeable.

---

## 7. Degradation, not failure

Redis down → in-memory cache. ChromaDB/native deps unavailable → lexical column matching
via the registry. LLM down → deterministic template insight. `chat_logs.csv` locked by
Excel → row spilled to `chat_logs.pending.csv` (merge with
`python scripts\init_data_files.py --merge-pending`). Any agent node raising → graceful
error state.

LLM failures are **classified**, not lumped together: unreachable host / model not pulled
(404) / timeout / bad API key each report differently. Lumping them together previously
sent users to check a server that was running fine.

---

## 8. How to report an error to Claude

Paste this file, then paste:

1. **`python scripts\diagnose_sqlserver.py > diag.txt 2>&1`** — full output
2. **Last full traceback** from `logs\errors.log` (`Traceback (most recent call last):`
   through the final line — never a partial one)
3. From the backend log, these two lines:
   - `sql_generation success attempt=... sql=SELECT ...`
   - `SQL executed rows=... dtypes={...}`   ← usually decisive
4. From the UI: the insight text, the ⚠️ Warnings section, and the SQL + row count under
   🔍 "View SQL & data"

---

## 9. Working arrangement

The user develops on a personal laptop (no SQL Server, no `.venv`, most dependencies
missing — but `pandas` is importable) and runs the real app on an office laptop that has
the database. Code moves across **by hand**.

Therefore:
- Give **exact copy-paste blocks with file name and location** ("find this → replace with
  this"), never "update the function".
- State plainly what was actually executed vs only reasoned about. Do not claim
  end-to-end verification when only unit logic ran.
- Prefer one complete fix over several speculative ones — each round trip costs the user a
  laptop switch.
