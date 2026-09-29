# USGI Business Insight Chatbot

An NLP-to-SQL-to-insight-and-chart chatbot over `dbo.May_2`. Every business question gets
**both** an NLP insight and a chart (mandatory), produced by a LangGraph multi-agent
pipeline.

Independent switches make it portable, each one line in `.env`:

| Switch | Options | Section |
|---|---|---|
| `DATA_SOURCE` | enterprise SQL Server **or** a local `.csv` / `.xlsx` | [4.1](#41-switching-data-source-sql-server--local-file) |
| `LLM_PROVIDER` | local **Ollama** (nothing leaves the machine) **or** the **Gemini** API | [4.2](#42-switching-llm-provider-ollama--gemini) |
| `CHART_TYPE` | `auto` · `bar` · `pie` · `donut` · a list like `bar,pie,donut` · `all` | [5](#chart-type) |
| `AUTH_MODE` | `csv` (email form against `data/users.csv`) **or** `sso` (company sign-in via nginx + oauth2-proxy) | [4.3](#43-switching-login-userscsv--sso) |

With `DATA_SOURCE=local` + `LLM_PROVIDER=ollama` the whole thing runs fully offline and
no data leaves the host — that is the default and the intended on-prem configuration.

```
User (Streamlit chat) ──▶ FastAPI /chat ──▶ LangGraph agent pipeline ──▶ ┌────────────────┐
                                │                    │                   │  DATA_SOURCE   │
                                │      ┌─────────────┴───────────────┐   ├────────────────┤
                                │      │ 1. Query Understanding      │   │ sqlserver:     │
                                │      │    (normalize, conversational│  │   pyodbc/ODBC  │
                                │      │    + filter rewriting,      │   │                │
                                │      │    identifier binding,      │   │      - or -    │
                                │      │    routing, decomposition)  │   │                │
                                │      │ 2. Schema & Example         │   │ local:         │
                                │      │    Retrieval (lexical +     │   │   DuckDB over  │
                                │      │    ChromaDB cosine)         │   │   the .xlsx    │
                                │      │ 3. SQL Generation           │   │   (T-SQL is    │
                                │      │    (Qwen2.5-Coder, T-SQL)   │   │   translated)  │
                                │      │ 4. SQL Execution (guarded,  │   └────────────────┘
                                │      │    column names repaired)   │
                                │      │ 5. Calculation (sandboxed)  │  Exactly one backend
                                │      │ 6. Chart Generation (Plotly)│  is ever initialised.
                                │      │ 7. Insight Generation       │
                                │      │    (numbers taken from the  │
                                │      │     result, never re-typed) │
                                │      └─────────────────────────────┘
                                ▼
                    Cache (Redis, in-memory fallback)
                    CSV logs (data/chat_logs/YYYY-MM/ week + month files)
```

## 1. Tech stack and rationale

| Layer | Choice | Why |
|---|---|---|
| Frontend | **Streamlit** | Fast to build, native chat UI (`st.chat_message`), native Plotly rendering |
| ASGI server | **Uvicorn** | Runs the FastAPI backend |
| Backend | **FastAPI** | Async API, request timeout handling, session auth, structured error responses |
| Agent framework | **LangGraph** | Explicit, controllable multi-agent state machine with conditional retry edges |
| LLM runtime | **Ollama** (default) or **Gemini API** | Ollama is 100% local/on-prem with no external API, which fits enterprise restrictions; Gemini is available via `LLM_PROVIDER=gemini` for machines that cannot host a 7B model (section 4.2) |
| LLM | **Qwen2.5-Coder 7B** | Strong SQL/code/tool-use for its size; also used to generate short pandas/numpy/scipy snippets for exact arithmetic instead of trusting the LLM to compute numbers itself |
| Small LLM | **Qwen2.5-Coder 3B** | Cheap follow-up rewriting, so short questions don't wait on the 7B model |
| Embeddings | **nomic-embed-text (via Ollama)** | Local embedding model, no HuggingFace/internet dependency at runtime |
| Semantic retrieval | **ChromaDB** (persistent, cosine similarity) | Local vector store mapping business language ("zone", "vertical", "business") to real column names and to similar past example questions |
| Column resolution | **Column registry + shortcut dictionary** | Deterministic, case/space/underscore-insensitive matching so "sub inward no" reliably finds `Sub_Inward_Number` without asking a model (section 6.1) |
| SQL connectivity | **pyodbc** + **ODBC Driver 17 for SQL Server** | Matches the enterprise SSMS environment exactly (`DATA_SOURCE=sqlserver`) |
| Local file querying | **DuckDB** (+ **openpyxl** for `.xlsx`) | Real SQL over a local `.csv` / `.xlsx` with no server and no ODBC driver, for offline/testing use (`DATA_SOURCE=local`) |
| Data processing | **Pandas / NumPy / SciPy** | DataFrame manipulation and the calculation sandbox |
| Visualization | **Plotly (Express)** | Interactive charts, renders natively in Streamlit |
| Cache | **Redis**, auto-fallback to in-memory TTL cache | Fast repeat-question answers; app must never break if Redis isn't running |
| Auth | **data/users.csv** or **SSO** (nginx + oauth2-proxy) | `AUTH_MODE=csv`: allow-list of `user_id,user_name,user_email,role`. `AUTH_MODE=sso`: the company identity provider signs users in; `users.csv` still grants `developer` |
| Logging | **data/chat_logs/** | Full audit trail per the required schema, one CSV per week and per month, 2 months kept |
| Config | **.env** (git-ignored) | Credentials never hard-coded |

Everything above runs **entirely on the local machine** — no cloud LLM, no cloud vector
DB, no cloud cache required (Redis is optional; the app degrades gracefully without it).

## 2. Prerequisites

1. **Python 3.11** (recommended: 3.11.9+). This is pinned deliberately — `chromadb`
   (onnxruntime/hnswlib) and `pyodbc` have the most reliable prebuilt Windows wheels on
   3.11; newer Python versions risk falling back to source builds that need a C++
   compiler, which a locked-down enterprise machine usually doesn't have.
   Download: https://www.python.org/downloads/release/python-3119/
2. **Microsoft ODBC Driver 17 for SQL Server** (system-wide install, not pip-installable).
   Download: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
3. **Ollama** (local LLM runtime). Download: https://ollama.com/download
   Then pull the models used by this app:
   ```
   ollama pull qwen2.5-coder:7b
   ollama pull nomic-embed-text
   ```
   For heavier reasoning/SQL accuracy later, swap in a bigger model without code changes —
   just update `.env`:
   ```
   OLLAMA_SQL_MODEL=qwen2.5-coder:14b
   OLLAMA_INSIGHT_MODEL=qwen2.5:14b
   ```
4. **Redis** (optional). If not installed, the app automatically falls back to an
   in-memory cache (process-local, resets on restart) — no configuration needed either
   way. To enable shared/persistent caching: `docker run -d -p 6379:6379 redis:7`.
5. Network/VPN access to `172.16.8.20` (the SQL Server).

## 3. Project structure

```
v1/
├── backend/            FastAPI app, LangGraph agents, core services
│   ├── agents/          the 7 pipeline nodes + graph wiring + guardrails
│   │   ├── column_guard.py     validates/repairs column names in generated SQL
│   │   ├── answer_builder.py   deterministic answers + hallucinated-number rejection
│   │   └── sql_guard.py        SELECT-only / single-table / row-cap validation
│   ├── api/              auth, chat, health routes
│   ├── core/
│   │   ├── datasource.py       DataSource ABC + SqlServer/LocalFile + factory
│   │   ├── db.py               thin compatibility shim over get_datasource()
│   │   ├── llm_provider.py     LLMProvider ABC + Ollama/Gemini + factory
│   │   ├── llm_client.py       thin shim over get_provider() - what agents import
│   │   ├── sql_dialect.py      T-SQL -> DuckDB translation (local mode only)
│   │   ├── column_registry.py  resolves any phrase to a physical column
│   │   ├── identifiers.py      the normalization rule everything matches through
│   │   └── cache.py, security.py, logging_config.py
│   ├── knowledge/
│   │   └── column_aliases.json  the editable column shortcut dictionary
│   ├── retrieval/        ChromaDB store + provider-backed embedding function
│   ├── services/         users.csv / chat audit log (weekly + monthly files) access
│   └── models/           Pydantic request/response schemas
├── frontend/
│   ├── streamlit_app.py  the single chat interface
│   ├── theme.py          UNISONIC skin: CSS, header, mascot, chat bubbles, footer
│   └── assets/           unisonic_mascot.jpg (mascot panel), unisonic_avatar.jpg (bot avatar)
├── .streamlit/config.toml  dark base theme the skin is built on
├── config/settings.py    all configuration (reads .env, validated at startup)
├── data/                 users.csv, chat_logs/, schema_metadata.json,
│                         example_queries.json, chroma_store/, cache/ (generated)
├── scripts/              setup_chromadb.py, build_column_aliases.py,
│                         init_data_files.py, test_db_connection.py
├── tests/                pytest unit tests (no data source/Ollama required)
├── logs/                 app.log, errors.log (generated)
├── .env                  local secrets (git-ignored) — already filled in for this project
└── .env.example          template for other environments
```

## 4. Local setup (Windows)

### One-time setup
```
setup.bat
```
This creates `.venv` with Python 3.11, installs `requirements.txt`, copies `.env.example`
to `.env` if missing, and creates `data/users.csv` / `data/chat_logs/` / `logs/` if
missing. **`.env` in this repo is already pre-filled with the provided SQL Server
credentials** — review it before use.

Or manually (PowerShell):
```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\init_data_files.py
```

### Verify dependencies before first real use
```
python scripts\test_db_connection.py       REM confirms the ACTIVE data source (see section 4.1)
python scripts\build_column_aliases.py     REM builds the column shortcut dictionary
python scripts\setup_chromadb.py           REM builds the semantic schema index (needs data source + Ollama)
```
Re-run `build_column_aliases.py` and `setup_chromadb.py` any time the live table schema
changes.

## 4.1 Switching data source: SQL Server ⇄ local file

The same agent pipeline can run against either the enterprise SQL Server or a local
`.csv` / `.xlsx`. **Exactly one backend is ever initialised** — the unselected one is
never even imported, so local mode needs no ODBC driver and SQL Server mode needs no
DuckDB.

Switch by editing **one line** in `.env`, then restarting the backend:

```ini
# Enterprise SSMS over ODBC
DATA_SOURCE=sqlserver

# ...or a local file, queried with DuckDB
DATA_SOURCE=local
LOCAL_DATA_PATH=F:/data/GEN AI PROJECTS/UNIVERSAL SOMPO/dummy_insurance_data.csv
LOCAL_SHEET_NAME=              # .xlsx only; ignored for .csv. Blank = first sheet
```

Confirm which one is live:

```
python scripts\test_db_connection.py
```
It prints the active mode, the target, and the introspected column list. The backend logs
the same on startup, and `GET /health/detailed` reports it under `data_source.mode`. Every
answer in the UI is tagged with the source that produced it.

| | `DATA_SOURCE=sqlserver` | `DATA_SOURCE=local` |
|---|---|---|
| Engine | pyodbc + ODBC Driver 17 | DuckDB (in-process) |
| Needs ODBC driver | yes | **no** |
| Needs network/VPN | yes | **no** |
| Reads | `USGI_CHAT.dbo.May_2` | `LOCAL_DATA_PATH` (`.csv` or `.xlsx`) |
| Table name in SQL | `dbo.May_2` | `dbo.May_2` **and** `May_2` both resolve |
| Uses `.env` `DB_*` values | yes | ignored |

**How local mode works.** The file is read once, registered as a DuckDB table plus a
`dbo`-schema view so qualified and unqualified names both work, and cached to Parquet
under `data/cache/` keyed by the source file's modification time and size. Later starts
load from that cache in ~2 s; replace the source file and the cache invalidates itself.

Each format takes the path that suits it:

* **`.csv`** — DuckDB's own `read_csv_auto`, with `sample_size=-1` so column types are
  inferred from the whole file rather than the first 20k rows (a column that is numeric
  for thousands of rows and then holds a code would otherwise abort the load part-way).
  The CSV never passes through Python memory, which matters for a ~100 MB file on a host
  that is also holding a 7B model resident.
* **`.xlsx`** — `pandas.read_excel` + openpyxl. DuckDB's Excel support lives in an
  extension that is downloaded from the internet on first use, which fails on the
  air-gapped on-prem hosts this app targets, and its type inference over a 124-column
  sheet is weaker than pandas'.

Neither path uses `pandas.to_parquet` — that would need `pyarrow`, which must not be
installed (see Known limitations). DuckDB writes the Parquet itself.

**Dialect translation.** The agents always emit Microsoft T-SQL, whichever source is
active, so prompts and behaviour are identical. In local mode
`backend/core/sql_dialect.py` rewrites it for DuckDB immediately before execution
(`SELECT TOP n`→`LIMIT n`, `GETDATE()`→`CURRENT_DATE`, `ISNULL`→`COALESCE`,
`[ident]`→`"ident"`, `dbo.table`→`table`, plus `FORMAT`/`DATEADD`/`DATEDIFF`/`DATEPART`).
String literals are never rewritten. A construct with no faithful DuckDB equivalent (e.g.
`PIVOT`, `TOP n PERCENT`) raises an error **naming that construct** rather than silently
returning different numbers. See `tests/test_sql_dialect.py`.

Query results are cached per data source, so switching modes can never serve an answer
computed against the other backend.

## 4.2 Switching LLM provider: Ollama ⇄ Gemini

The agent pipeline is provider-agnostic. Agents never name a provider — they ask for a
**role** (`settings.sql_model`, `insight_model`, `router_model`, `embed_model`) and the
active provider resolves it. **Exactly one provider is ever initialised.**

Switch by editing **one line** in `.env`, then restarting the backend:

```ini
# Fully local - nothing leaves the machine (default)
LLM_PROVIDER=ollama

# ...or Google Gemini
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key-here          # https://aistudio.google.com/apikey
```

| | `LLM_PROVIDER=ollama` | `LLM_PROVIDER=gemini` |
|---|---|---|
| Where inference runs | this machine | Google's servers |
| Needs internet | **no** | yes |
| Needs a 7B model + ~5 GB RAM | yes | **no** |
| Data sent off-box | **none** | question + retrieved column names + computed figures |
| Typical latency (CPU-only host) | 30–150 s | 2–10 s |
| Cost | free | per Google's API pricing |
| Startup warm-up | loads weights | skipped (nothing to preload) |

**⚠️ Privacy.** Gemini mode sends the user's question, the retrieved column names, and the
precomputed statistics — which contain **real figures from your data** — to Google. Only
enable it for data cleared for third-party processing. Ollama mode keeps every byte local;
that is why it is the default.

**After switching, re-run the index once:**

```
python scripts\setup_chromadb.py
```

Ollama's `nomic-embed-text` and Gemini's `text-embedding-004` produce vectors in different
spaces, so the ChromaDB collections are namespaced per provider
(`schema_metadata__ollama`, `schema_metadata__gemini`). Mixing them would either fail on a
dimension mismatch or silently return meaningless neighbours. The question cache is keyed
by provider and model for the same reason. Indexes for both providers can coexist — switch
back and forth freely once each has been built.

Confirm which provider is live:

```
python scripts\test_db_connection.py     REM prints LLM_PROVIDER alongside the data source
```
`GET /health/detailed` reports it under `llm.provider`, along with the configured models
and the ones actually available, so a wrong model name is diagnosed before a question is
asked. The backend logs the same on startup.

**Model choice.** The Gemini defaults are `gemini-2.0-flash` (SQL/insight),
`gemini-2.0-flash-lite` (router) and `text-embedding-004`. 2.0 is the default deliberately:
2.5-series models spend part of `GEMINI_MAX_OUTPUT_TOKENS` on hidden reasoning, and a short
answer can come back empty. If you switch to a 2.5 model, also set
`GEMINI_THINKING_BUDGET=0`. All model names are `.env` values — no code change.

**Failures are classified, not lumped together**, exactly as for Ollama: a bad key reports
as a key problem, an unknown model as a missing model, a slow response as a timeout. All
of them subclass `LLMUnavailableError`, so the pipeline degrades to deterministic template
insights identically whichever provider is active.

## 4.3 Switching login: users.csv ⇄ SSO

```ini
AUTH_MODE=csv          # the email form; an email listed in data/users.csv gets in
# ...or
AUTH_MODE=sso          # company sign-in through nginx + oauth2-proxy; no form
SSO_SHARED_SECRET=...  # python -c "import secrets; print(secrets.token_urlsafe(32))"
SSO_REQUIRE_USERS_CSV=false
```

Restart **both** the backend and the frontend after changing it — each reads `.env`.

| | `AUTH_MODE=csv` | `AUTH_MODE=sso` |
|---|---|---|
| Who gets in | an email listed in `users.csv` | whoever oauth2-proxy lets in (`email_domains` / groups); with `SSO_REQUIRE_USERS_CSV=true`, only those also in `users.csv` |
| Password | none | the company identity provider's |
| Login screen | email form | none — signed in on page load; an error screen names the cause if it cannot |
| Developer role | `role` column of `users.csv` | the same — an SSO user not in `users.csv` is a `user` |
| `/auth/login` | on | **off** (403) — it would bypass the SSO |
| `/auth/sso` | off (403) | on, only with `SSO_SHARED_SECRET` |

**How SSO works.** nginx asks oauth2-proxy about every request (`auth_request`) and forwards
the answer as `X-Auth-Request-Email` (and the ID token as `Authorization`). Streamlit reads
those headers from its websocket request, forwards just those four to `POST /auth/sso`
with the shared secret, and the backend (`backend/core/sso.py`) decides who they name —
`X-Auth-Request-Email` first, then the token's `email` / `preferred_username` / `upn`
claim. The user id in the chat log is the `users.csv` id when the email is listed, else the
email itself. The role is re-read from `users.csv` on every question, as in csv mode.

**Deployment (Linux, as for the other app):**

1. oauth2-proxy — fill the Entra block of `.env` (`ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`,
   `ENTRA_CLIENT_SECRET`, `ENTRA_REDIRECT_URI`, `SSO_COOKIE_SECRET`, `SSO_EMAIL_DOMAINS`),
   then `python scripts\build_oauth2_proxy_config.py`. It validates them (GUIDs, the
   secret's Value not its ID, a redirect ending in `/oauth2/callback`, a 16/24/32-byte
   cookie key) and writes `deploy/sso/oauth2-proxy.cfg` (git-ignored — it holds the
   secret). Run oauth2-proxy with `--config` pointing at it. The redirect URI must be
   registered in the Entra app under Authentication exactly as in `.env`.
2. nginx — `deploy/sso/nginx_sso.conf` (the other app's `sso.conf` plus the identity
   headers). The app is served under `/unisonic/`.
3. Streamlit — `streamlit run frontend/streamlit_app.py --server.baseUrlPath=unisonic
   --server.address=127.0.0.1` (in docker: `0.0.0.0`, port not published).
4. Backend — bound to `127.0.0.1` (or the docker network only). Streamlit calls it server
   to server; nginx never exposes it.
5. `.env` on that host: `AUTH_MODE=sso` and a fresh `SSO_SHARED_SECRET`.

Steps 3–4 are what make it secure: nginx replaces the identity headers on every request,
but anyone who can reach port 8501 or 8000 directly skips nginx. `GET /health/detailed` →
`auth` shows the active mode (never the secret); the backend logs it on startup.

### Run the app (two terminals, both with `.venv` activated)
```
run_backend.bat
```
```
run_frontend.bat
```
Then open **http://localhost:8501** and log in with an email from `data/users.csv`
(e.g. `harshit.pandey@universalsompo.com`, seeded by default).

The chat screen has the example questions in the left sidebar (click one to ask it), the
mascot with your name and role at the right end of the header, and the conversation
centred; after each answer the
window scrolls so the question sits at the top with its insight and chart below. There is
no Log out button — a session ends when it has been idle for `SESSION_TIMEOUT_MINUTES`.

Backend health check: http://localhost:8000/health/detailed — shows SQL Server, Ollama,
ChromaDB, and cache status individually, so a broken dependency is obvious immediately.

## 5. Domain questions this app is built to answer

1. High-performing branch region-wise.
2. Vertical-wise weekly business trend.
3. Product-wise and branch-wise trend.
4. Zone-wise and vertical-wise business contribution.
5. Business-type-wise vertical performance.
6. High-performing intermediaries.
7. Show the top five branches and their weekly trend.
8. Compare zone contribution and identify high-performing intermediaries.
9. Show business contribution by zone, vertical, and product.
10. Which branch has the highest business, and how did it trend weekly?
11. Show actual versus target if the relevant columns exist.
12. Generate separate charts for branch performance and vertical contribution.

Type any of these into the chat box to try them.

<a id="chart-type"></a>**Chart types: bar, pie, donut only** — selection is rule-based, never LLM-guessed.
In precedence order:

1. A type named in the question ("… as a pie chart") — line/scatter requests become a bar.
2. `CHART_TYPE` in `.env` — `bar`, `pie` or `donut` always draws that type; a list such as
   `bar,pie,donut` (or `all`) draws one chart of each, in that order.
3. `CHART_TYPE=auto` (default) — "contribution"/"share" with ≤8 categories → donut,
   everything else → bar. A time axis is always an ordered bar chart.

A pie/donut is drawn as a bar, with a warning, when the result has a time axis or negative
values (plotly silently drops negative slices). Beyond 8 slices the smallest are summed
into one "Others (n)" slice, so the total is unchanged.

## 6. Query rewriting pipeline (`backend/agents/query_understanding.py`)

- **Normalization** — case/whitespace cleanup, abbreviation expansion (YoY, QoQ, ...)
- **Conversational rewriting** — short follow-ups ("now show it by product") are expanded
  into standalone questions using the session's last 5 Q&A pairs (LLM-assisted)
- **Schema-aware rewriting** — business terms are grounded to real column names by the
  retrieval agent, which combines certain lexical matching (via the column shortcut
  dictionary, section 6.1) with ChromaDB cosine similarity
- **Filter-aware rewriting** — extracts `top_n`, date grain (weekly/monthly/quarterly),
  an explicit chart-type request, **and identifier filters**: `POLICY_NO-1029156133` is
  parsed here and bound to the physical column `POLICY_NO` before the SQL model is asked
  anything, so the model cannot filter that value on a similarly-named column. Ordinary
  hyphenated English ("region-wise") is not mistaken for a filter.

## 6.1 Column shortcuts (`backend/knowledge/column_aliases.json`)

**All matching ignores case, spaces, underscores and hyphens.** Every user phrase and
every column name is reduced to a comparison key by stripping non-alphanumerics and
lowercasing, so `Sub_Inward_Number`, `sub inward number`, `SUB INWARD NO` and
`sub-inward-number` are all the same key. Nothing about the *physical* column name ever
changes — normalization is only used for lookup.

Resolution order, strongest first (a weaker rule never overrides a stronger one):

| # | Rule | Example |
|---|---|---|
| 1 | exact physical name | `Sub_Inward_Number` |
| 2 | compacted physical name | `sub inward number` |
| 3 | curated alias from the JSON | `sub inward no` |
| 4 | generated variant (`number`↔`no`↔`num`, `amount`↔`amt`, …) | `sub inward num` |
| 5 | fuzzy match above `COLUMN_FUZZY_THRESHOLD` | `sb inward no` (typo) |

**Ambiguity is refused, never guessed.** A shortcut claimed by two columns is dropped from
the lookup and logged. `sum insured` matches both `TOTAL_SUM_INSURED` and
`USGI_SUM_INSURED`, so it resolves to neither and the user is shown both candidates —
silently picking one is how a chatbot returns a confidently wrong number.

Regenerate or inspect the dictionary:

```
python scripts\build_column_aliases.py              REM write/refresh the file
python scripts\build_column_aliases.py --check      REM verify only; non-zero exit on a clash
python scripts\build_column_aliases.py --show Sub_Inward_Number
```

To teach the bot a new shortcut, add it to that column's `aliases` list in the JSON — hand
edits are preserved across regeneration. The `generated` list is machine-managed. The build
script fails loudly if a shortcut you add collides with another column, so two columns can
never share one. Current state: **124 columns, ~475 usable shortcuts, 4 ambiguous ones
correctly ignored** (visible at `GET /health/detailed`).
- **Query routing** — classifies into ranking / trend / contribution / comparison /
  actual_vs_target / decomposition / aggregation
- **Query decomposition** — splits compound questions ("compare X and identify Y") into
  sub-questions used to (a) widen schema retrieval and (b) drive multiple charts from one
  SQL result (see **Known limitations** below)

## 7. Caching

Cache key = SHA-256 of the normalized question (case/punctuation/whitespace-insensitive),
so "High-performing branch region-wise." and "high performing branch region wise" hit the
same cached answer. Backed by Redis when reachable; otherwise an in-memory TTL cache
(`CACHE_TTL_SECONDS`, default 6h) — the app never fails because Redis is down, it just
loses cross-process cache sharing (logged as a warning, visible at `/health/detailed`).

## 8. Auth, sessions, and logging

- **Auth**: `AUTH_MODE` picks one way in (section 4.3). `csv`: `data/users.csv`
  (`user_id,user_name,user_email,role`) is a simple allow-list — enter your email to get a
  session. No password, so the role is only as strong as the secrecy of a developer's
  email. `sso`: the company identity provider signs the user in through nginx +
  oauth2-proxy and the email form is switched off.
- **Roles (RBAC)**: `role` is `developer` or `user`; a missing column, blank or unknown value
  means `user`. A **developer** sees each answer's "⚠️ Warnings" and "🔍 View SQL & data"
  panels; a **user** sees only the insight and the chart. This is enforced by `/chat` itself
  (`backend/core/rbac.py` strips SQL, data preview, columns, warnings and raw error text for
  a user), not just hidden in the UI. The role is re-read from `users.csv` on every
  question, so a change applies immediately. The cache and the audit log always keep the
  full SQL.
- **Sessions**: in-memory + persisted to `data/sessions.json` (survives a backend
  restart). Idle sessions expire after `SESSION_TIMEOUT_MINUTES` (default 480). There is no
  Log out button: an idle-expired session is purged and its rows get `logout_time` = the
  time the user was last active.
- **Logging**: every question is appended, with exactly the required columns
  `user_id, user_name, user_question, generated_sql_query, generated_output, login_time,
  logout_time, ques_no`, to BOTH its week file and its month file:

  ```
  data/chat_logs/2026-09/chat_logs_2026-09_week1.csv   days 1-7
  data/chat_logs/2026-09/chat_logs_2026-09_week2.csv   days 8-14
  data/chat_logs/2026-09/chat_logs_2026-09_week3.csv   days 15-21
  data/chat_logs/2026-09/chat_logs_2026-09_week4.csv   days 22-end of month
  data/chat_logs/2026-09/chat_logs_2026-09_month.csv   the whole month
  ```

  Weeks follow the backend machine's local calendar. `CHAT_LOG_KEEP_MONTHS` (default 2 =
  current + previous month) sets retention: older month folders are deleted automatically
  at startup and once a day. The pre-2026-09 single file `data/chat_logs.csv` is never
  touched; move its rows in once with `python scripts\init_data_files.py --split-legacy`.
  `ques_no` increments per question within a login session (same `user_id` + `login_time` =
  same session); `logout_time` is back-filled across that session's rows when it ends.
- **Read-only data access**: nobody can change `dbo.May_2` through the chatbot. Generated
  SQL containing any write (INSERT, UPDATE, DELETE, MERGE, `SELECT … INTO`, EXEC, DDL, …)
  is refused by `sql_guard` and again by the data source; SQL Server reads run in a
  transaction that is always rolled back; and `scripts/sqlserver_readonly.sql` (for the
  DBA) makes the login SELECT-only and adds a Row-Level Security BLOCK policy on the table.
  Check the result with `python scripts\check_readonly_access.py`.

## 9. Timeouts and fail-safe behavior

| Layer | Setting | Behavior on breach |
|---|---|---|
| Overall request | `REQUEST_TIMEOUT_SECONDS` (default 600s) | Returns **"Request timed out. Please refresh the page."** |
| SQL query | `DB_QUERY_TIMEOUT` (default 30s) | Same friendly message, logged with detail |
| Ollama connect | `OLLAMA_CONNECT_TIMEOUT` (default 5s) | Fails fast — an unreachable host no longer blocks for the full read timeout |
| Ollama response | `OLLAMA_REQUEST_TIMEOUT` (default 300s) | Retried once, then a message naming the real cause |
| Any agent node | n/a | Wrapped individually — a failure in chart/insight generation still returns the data/SQL already fetched instead of a blank error |

**LLM failures are classified, not lumped together.** The client previously reported every
Ollama exception — including a read timeout on a model that was merely loading slowly — as
*"Local LLM 'qwen2.5-coder:7b' is unreachable. Ensure Ollama is running…"*, which sent
users to check a server that was running fine. Each condition now reports itself:

| Condition | Message |
|---|---|
| Nothing listening on `OLLAMA_HOST` | *Ollama is not reachable at … Start it with `ollama serve`* |
| Model not pulled (HTTP 404) | *Ollama is running, but model 'X' is not available. Pull it with `ollama pull X`* |
| Answered, but too slowly | *Ollama is running, but model 'X' did not respond within Ns — usually a cold model load* |
| Answered with an error | The daemon's own error text |

`GET /health/detailed` lists configured vs. actually-pulled models, so a missing model is
diagnosed before a question is ever asked.

**Degradation, not failure.** Redis down → in-memory cache. ChromaDB or its native
dependencies unavailable → lexical column matching (which resolves any column the user
names or abbreviates). Ollama down → deterministic template insight from the query result.
A chat-log file locked by Excel → the row is written to that file's `.pending.csv` sibling
instead of being lost (merge with `python scripts\init_data_files.py --merge-pending`).

All errors are logged with a `request_id` to `logs/app.log` / `logs/errors.log`
(rotating, 10MB × 5 backups) for fast debugging; every `/chat` response also carries
per-agent timings in `timings_ms`. ChromaDB's broken telemetry client
(which emitted an ERROR line on every collection access and buried real failures) is
silenced.

## 9.1 Numeric accuracy

Figures are never re-typed by a language model.

1. **Direct** — when the SQL result is a single row of scalars, the answer is formatted
   straight from the DataFrame. No model is in the loop, so there is nothing to
   hallucinate. This is the path for identifier lookups.
2. **Verified narrative** — for many-row results the model writes prose from a precomputed
   statistics dict, and every figure it emits is then checked against the values it was
   given. An unsupported figure, or any invented currency symbol (the source data carries
   no currency), rejects the narrative.
3. **Template** — the deterministic fallback used when the model is unavailable or its
   narrative fails verification.

Temperature follows the same rule: anything producing or restating a number runs at
`LLM_SQL_TEMPERATURE` / `LLM_NUMERIC_TEMPERATURE` (both `0.0`); only free prose uses
`LLM_TEXT_TEMPERATURE` (`0.2`). Each answer in the UI is tagged with the mode that produced
it, and `answer_mode` is returned on the API response.

## 10. SQL safety guardrails (`backend/agents/sql_guard.py`)

Since SQL is LLM-generated, every query is validated before execution: SELECT-only, no
DDL/DML/EXEC keywords, single statement, must reference only `dbo.May_2`, and a `TOP N`
row cap is injected if missing. Generated calculation code (`backend/agents/
calc_sandbox.py`) is similarly AST-validated (no imports, no dunder access) and runs with
a restricted builtins set and a hard timeout.

## 11. Known limitations (be transparent, not surprised)

- **Decomposition is single-pass**: a compound question ("compare zone contribution and
  identify high-performing intermediaries") runs ONE retrieval+SQL pass whose prompt is
  instructed to return enough grouped columns to cover every sub-question; the chart agent
  then slices that one result into multiple charts. This bounds latency/failure modes on a
  local 7B model instead of recursively re-running the whole pipeline per sub-question. If
  a sub-question can't be matched to a returned column, it's skipped with a visible warning
  rather than silently dropped.
- **Zone/Target columns**: the reference sample has no explicit `ZONE` or `TARGET` column.
  `STATE` / `USGI BRANCH STATE` are used as a zone proxy; `scripts/setup_chromadb.py`
  indexes whatever the **live** table actually contains, so if `dbo.May_2` has real
  ZONE/TARGET columns they will be picked up automatically — the app never fabricates a
  target figure if one truly doesn't exist.
- **Sessions are single-process**: fine for local/internal use; horizontal scaling would
  need a shared session store (e.g. Redis-backed) instead of the in-memory + JSON file.
- **Latency is hardware-bound**: on a CPU-only host (16 GB RAM, no discrete GPU) a
  question takes roughly 30–150 s, almost all of it 7B-model token generation. The
  defaults are sized for that box: SQL and insight share one model so only ~4.7 GB of
  weights stay resident, `OLLAMA_KEEP_ALIVE` avoids re-loading them, and `OLLAMA_NUM_CTX`
  is 4096. Running two different 7B models (~9.4 GB) on such a host is what previously
  caused mid-request timeouts. A GPU box, or `OLLAMA_SQL_MODEL=qwen2.5-coder:3b`, cuts
  this substantially.
- **`pyarrow` must not be installed**: on Windows it loads native DLLs that shadow the ones
  onnxruntime needs, so a later `import onnxruntime` either raises
  `DLL load failed while importing onnxruntime_pybind11_state` or — non-deterministically —
  hard-crashes the interpreter with `Windows fatal exception: access violation`, which no
  `try/except` can catch. onnxruntime is pulled in by chromadb, so installing pyarrow
  silently disables semantic retrieval and can kill the uvicorn worker mid-request.
  Fixing it by import *order* was tried and rejected: it only holds if `backend` is
  imported before pandas, which nothing guarantees across tests, scripts and plugins.
  The only feature that wanted pyarrow was Streamlit's `st.dataframe`, so the data preview
  renders as HTML instead (`_render_table` in `frontend/streamlit_app.py`); the trade-off
  is losing in-browser column sorting on that preview. `tests/test_import_order.py` fails
  the build if pyarrow reappears or if an Arrow-backed widget is reintroduced.
  If you ever see that DLL error: `pip uninstall pyarrow`.
- **Local mode reads a snapshot**: the workbook is loaded at startup (and cached to
  Parquet). Editing the `.xlsx` while the backend is running has no effect until restart —
  the cache invalidates itself on the file's modification time.
- **The pyodbc `asyncio.wait_for` timeout** returns control to the client at the deadline,
  but the underlying blocking DB/LLM call keeps running in its worker thread until it
  finishes naturally — acceptable for a local internal tool, not a hard kill.

## 12. Testing

```
pytest
```
131 tests, none of which require SQL Server, Ollama or ChromaDB to be running:

| File | Covers |
|---|---|
| `test_sql_dialect.py` | T-SQL → DuckDB translation, literal safety, and that every untranslatable construct raises an error naming it |
| `test_column_registry.py` | Phrase → column resolution, ambiguity refusal, and SQL identifier repair (`[GROSS PREMIUM]` → `[GROSS_PREMIUM]`) |
| `test_identifiers.py` | The normalization rule: case/space/underscore/hyphen insensitivity |
| `test_answer_builder.py` | Deterministic answers and rejection of unsupported figures / invented currency |
| `test_query_understanding.py` | Identifier binding, routing, and that hyphenated English isn't mistaken for a filter |
| `test_datasource.py` | Both data backends' shared contract, CSV/Excel path validation, and schema-type parity |
| `test_sqlserver_result_shape.py` | The pyodbc result shape: Decimal/date coercion, un-aliased aggregate names, and that a SQL Server result produces the **same** chart and statistics as the local backend |
| `test_llm_provider.py` | Both LLM providers' shared contract, role→model resolution across a switch, and the error taxonomy |
| `test_sql_guard.py`, `test_cache.py`, `test_calc_sandbox.py` | SQL guardrails, cache keys, the calculation sandbox |

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| `ODBC driver ... not found` | Install ODBC Driver 17 (see Prerequisites) |
| `SQL Server login failed` | Check `DB_USERNAME`/`DB_PASSWORD` in `.env`. The error now includes the real SQLSTATE and driver message |
| Can't reach SQL Server (VPN down) | Set `DATA_SOURCE=local` in `.env` to work offline against the local file (section 4.1) |
| `LOCAL_DATA_PATH has an unsupported extension` | Only `.csv`, `.tsv`, `.txt`, `.xlsx`, `.xlsm`, `.xls` are supported. Convert the file, or point at the right one |
| *"Ollama is not reachable at …"* | Ollama really is down — run `ollama serve`. Or set `LLM_PROVIDER=gemini` to use the API instead (section 4.2) |
| *"Gemini rejected the API key"* | Check `GEMINI_API_KEY` in `.env` — get one at https://aistudio.google.com/apikey |
| *"Gemini has no model 'X'"* | Check the `GEMINI_*_MODEL` values. `GET /health/detailed` lists what is actually available |
| *"Gemini rate limit / quota exceeded"* | Wait, or switch back to `LLM_PROVIDER=ollama` |
| Gemini returns empty answers | You are on a 2.5-series model spending the output budget on reasoning. Set `GEMINI_THINKING_BUDGET=0`, or raise `GEMINI_MAX_OUTPUT_TOKENS` |
| TLS handshake fails on Gemini | A corporate proxy is intercepting HTTPS. Use `LLM_PROVIDER=ollama` |
| Schema index empty right after switching provider | Collections are per-provider. Run `python scripts\setup_chromadb.py` once for the newly selected one (section 4.2) |
| *"model 'X' is not available"* | `ollama pull X`. `GET /health/detailed` lists configured vs. pulled models |
| *"did not respond within Ns"* | A cold model load or a very slow host. Raise `OLLAMA_REQUEST_TIMEOUT`, or point `OLLAMA_SQL_MODEL` at `qwen2.5-coder:3b` |
| Answers take 30–150 s | Expected on a CPU-only host (no GPU). Keep `OLLAMA_KEEP_ALIVE` set so weights stay resident; `qwen2.5-coder:3b` roughly halves it at some accuracy cost |
| `DLL load failed while importing onnxruntime_pybind11_state`, or `Windows fatal exception: access violation` | **Do not install `pyarrow`** — on Windows it breaks onnxruntime, which disables ChromaDB and can crash the worker. Run `pip uninstall pyarrow` |
| `ModuleNotFoundError: No module named 'pyarrow'` in Streamlit | Something reintroduced `st.dataframe`/`st.table` (both need Arrow). Use `_render_table()` in `frontend/streamlit_app.py` instead — do **not** install pyarrow to "fix" it |
| **Works on `local`, but on `sqlserver` there is no chart and the insight has no real figures** | This was the pyodbc dtype bug — `Decimal`/`date` objects left every measure as an `object` column, so `numeric_columns()` returned `[]`. Fixed by `normalize_result_frame()` in `datasource.py`. Verify with `python scripts\diagnose_sqlserver.py`; check [3] must show `float64`, not `object` |
| *"Could not render chart … Invalid property … layout.template.Data: 'heatmapgl'"* | Backend and Streamlit loaded different plotly major versions (5.x wrote it, 6.x read it). Fixed permanently by `backend/core/figure_codec.py`, which strips the version-specific template; realign anyway with `pip install plotly==5.24.1` in `.venv`. `GET /health/detailed` → `charts.plotly_version` shows the backend's version |
| Chart missing and the warning says *"Data did not fit a standard chart shape"* | `pick_measure()` found no measure. Run `python scripts\diagnose_sqlserver.py` — check [4] names the numeric columns it saw. Usually the SQL selected only identifier columns; alias the aggregate (`SUM(x) AS total_x`) |
| Insight says *"No numeric measure was found…"* | Honest, not a bug: the query returned only identifiers/text. Rephrase so the SQL aggregates something |
| Schema index empty warning | Run `python scripts\setup_chromadb.py` after the data source + Ollama are up |
| "Please refresh the page" | A request exceeded `REQUEST_TIMEOUT_SECONDS` — just retry; consider raising it for a slower machine |
| Redis warnings in logs | Harmless — app auto-falls back to in-memory cache; start Redis to remove the warning |
| `Could not write chat_logs_…csv` in the log | That week/month file is open in Excel. Rows are spilled to its `.pending.csv` sibling; close Excel and run `python scripts\init_data_files.py --merge-pending` |
| Startup WARNING "The data source login can WRITE to dbo.May_2" | The SQL login has write permissions. Ask the DBA to run `scripts/sqlserver_readonly.sql`, then check with `python scripts\check_readonly_access.py` |
| A shortcut resolves to the wrong column | `python scripts\build_column_aliases.py --show <COLUMN>` shows every shortcut for it; edit `aliases` in `backend/knowledge/column_aliases.json` and re-run the script |
| Streamlit shows "Cannot reach backend" | Make sure `run_backend.bat` is running on port 8000 |
| *"Single sign-on is on … opened without a signed-in identity"* | The page was opened directly on 8501, or nginx forwards no identity. Use the nginx address; check oauth2-proxy has `set_xauthrequest = true` and nginx has the `X-Auth-Request-Email` lines of `deploy/sso/nginx_sso.conf` |
| *"SSO_SHARED_SECRET does not match"* | Frontend and backend read different `.env` values. Make them equal and restart both |
| *"Email login is off … AUTH_MODE=sso"* on the email form | The backend was switched to SSO but the frontend was not restarted. Restart it |
| oauth2-proxy: *"email in id_token isn't verified"* | The config was not built by `scripts\build_oauth2_proxy_config.py` (it sets `insecure_oidc_allow_unverified_email`). Rebuild it |
| Entra: *"AADSTS50011: redirect URI … does not match"* | `ENTRA_REDIRECT_URI` in `.env` differs from the one registered in the Entra app (Authentication). Make them identical, rebuild the config |
| Backend will not start: *"AUTH_MODE=sso requires SSO_SHARED_SECRET"* | Generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"`, put it in `.env` |
| SSO page is blank / keeps loading under `/unisonic/` | Streamlit was started without `--server.baseUrlPath=unisonic` |

## 14. Security notes

- `.env` is git-ignored; credentials are never hard-coded in source.
- SQL is allow-list validated (single table, SELECT-only) before execution.
- Calculation code runs in an AST-validated sandbox with restricted builtins, no
  filesystem/network access.
- `AUTH_MODE=csv` is an internal allow-list intended for a trusted internal network. Beyond
  that, use `AUTH_MODE=sso` — and keep ports 8501 and 8000 unreachable except through
  nginx, because the identity headers are only trustworthy when nginx has set them.
- In SSO mode the backend accepts an identity only with `SSO_SHARED_SECRET`
  (constant-time compare), so a direct POST to `/auth/sso` cannot pose as a developer, and
  `/auth/login` is refused so the email form cannot bypass the SSO.
- **`LLM_PROVIDER=gemini` sends data off the machine**: the question, the retrieved column
  names, and the computed statistics (which contain real figures) go to Google. The
  default `LLM_PROVIDER=ollama` sends nothing. Treat flipping that switch as a data-sharing
  decision, not a performance tweak.
- The Gemini key is read from `.env` (git-ignored) and travels in the `x-goog-api-key`
  header, never in a URL, so it cannot leak into request or proxy access logs.
