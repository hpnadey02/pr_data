# CLAUDE.md

Project context for Claude Code. Loaded automatically at the start of every session, on
any machine this folder is copied to.

**Reply to this user in Hinglish (Roman-script Hindi + English), not pure English.**

---

## What this is

USGI Business Insight Chatbot — an NLP-to-SQL-to-insight-and-chart assistant over the
insurance table `dbo.May_2` (124 columns). Every business question must return **both** an
NLP insight **and** a chart. A LangGraph pipeline of 7 agents does the work.

Read `README.md` for the full design. This file only carries what a fresh session cannot
infer from the code, plus the decisions that must not be accidentally undone.

## Four switches, each one line in `.env`

| Switch | Values | Notes |
|---|---|---|
| `DATA_SOURCE` | `sqlserver` \| `local` | `local` reads a `.csv`/`.xlsx` via DuckDB. Exactly one backend is ever constructed. |
| `LLM_PROVIDER` | `ollama` \| `gemini` | `ollama` is fully offline. `gemini` sends the question **and computed figures** to Google. |
| `CHART_TYPE` | `auto` \| `bar` \| `pie` \| `donut` \| list e.g. `bar,pie,donut` \| `all` | A list draws one chart of each type. Part of the cache key. See "Charts" below. |
| `AUTH_MODE` | `csv` \| `sso` | `sso` needs `SSO_SHARED_SECRET` (≥ 16 chars). Restart backend AND frontend. See "Login" below. |

After switching `LLM_PROVIDER`, re-run `python scripts/setup_chromadb.py` once — ChromaDB
collections are namespaced per provider because the two embedding spaces differ.

## Current status (2026-09)

- `DATA_SOURCE=local` + `LLM_PROVIDER=ollama`: **working** — insight and chart both render.
- `DATA_SOURCE=sqlserver`: the dtype bug below is **fixed in code but NOT yet verified
  against a live database**. The user is testing on an office laptop and will bring back
  real errors. Treat live SQL Server behaviour as unconfirmed until they report it.
- 2026-09-25: sidebar examples, mascot in the header, centred lane, no Log out, RBAC, weekly
  chat-log files + retention, read-only layers — built and tested on the personal laptop
  (unit + in-process API + headless-Edge screenshots). **Not yet on the office laptop**;
  `scripts/sqlserver_readonly.sql` has never run against a real server.
- 2026-09-28: `AUTH_MODE=sso` built. Verified on the personal laptop with the real backend +
  real Streamlit under `--server.baseUrlPath=unisonic` behind a stand-in tornado proxy that
  injected `X-Auth-Request-Email` (headless Edge: signed in as developer; direct :8511 showed
  the no-identity error; `/auth/login` 403, `/auth/sso` without secret 401). **Never run
  behind real nginx + oauth2-proxy + the company IdP.** `deploy/sso/*` is untested config.

### Working arrangement

The user develops on a personal laptop (no SQL Server) and runs the real thing on an
office laptop that has the database. They copy
code across by hand. So:

- **Give exact copy-paste blocks with file + location** ("find this, replace with that"),
  not vague instructions.
- **The office copy (`D:\PROJECT\UAT\power_bi_chat\v2_2`) is NOT in sync with this folder.**
  2026-09: a whole-file copy of `health.py` crashed its backend at startup because the
  office `llm_client.py` has no `check_llm`. Only a brand-new file may be copied whole;
  for an existing file, give the changed blocks, never "replace the whole file".
- The personal laptop now has a `.venv` with dependencies (as of 2026-09):
  `.venv\Scripts\python -m pytest` runs the whole suite. Its one known failure is
  `test_pyarrow_is_not_installed` — streamlit pulls pyarrow in on `pip install`.
- Never claim something is verified end-to-end when only the unit logic was exercised.

---

## THE SQL SERVER BUG — do not regress this

**Symptom:** identical question worked on `local` but on `sqlserver` produced **no chart**
and an **insight with no real figures**, even though rows were fetched fine.

**Cause:** pyodbc returns `decimal.Decimal` for SQL Server `DECIMAL/NUMERIC/MONEY` and
`datetime.date` for `date` columns. pandas cannot map those to a numpy dtype, so the
column lands as `object`. Everything downstream keys off dtype:

```
numeric_columns()  -> []            # Decimal is not is_numeric_dtype
pick_measure()     -> None
chart_agent        -> returns None  # no chart
compute_stats()    -> {"row_count": N} only  # model had nothing to summarise -> wrong insight
```

DuckDB returns real numpy dtypes, which is why `local` looked healthy.

The user's own datatype audit confirmed the scale: **47 columns are `decimal(38,2)` /
`decimal(18,6)`** (every measure — `GROSS_PREMIUM`, `NET_PREMIUM`, `COMMISSION_AMOUNT`, …)
and **4 are SQL `date`** (`POLICY_ISSUE_DATE`, `START_DATE`, `EXPIRY_DATE`, `VOUCHER_DATE`).
In `may_2.xlsx` those same 47 columns are stored as **Text**, so the Excel path fails the
same way.

**Fix — `normalize_result_frame()` in `backend/core/datasource.py`**, called by *both*
backends inside `run_select()`:

1. `Decimal` → `float64`
2. `datetime.date` / `datetime` → `datetime64`
3. Numeric **text** → `float64`, but only when the column NAME marks it a quantity
   (`looks_like_measure()` in `identifiers.py`). This is what stops `POLICY_NO` =
   `"1029156133"` becoming a float and corrupting an identifier.
4. `repair_column_names()` — SQL Server names an un-aliased `SUM(x)` as `''` (empty),
   which breaks Plotly and groupby. Empty/duplicate names get deterministic ones.

Supporting fixes:

- `backend/agents/df_utils.py` — `pick_measure()` no longer takes the first numeric
  column. SQL Server keeps `BRANCH_OFFICE_CODE` / `POLICY_NO` as real integers, so an
  identifier used to win. Identifier-named columns now score 0 and are never chosen; if
  *every* numeric column is an identifier it returns `None` rather than charting a policy
  number. `date_like_columns()` only honours a name hint on non-numeric columns, so a
  measure aliased `premium_this_month` is not used as the time axis.
- `backend/agents/insight_agent.py` — if `stats` has no `total`, the LLM is skipped
  entirely and the deterministic template is returned. Narrating an empty statistics dict
  is exactly how a confident wrong insight was produced.
- `backend/core/identifiers.py` — `IDENTIFIER_NAME_TOKENS` / `MEASURE_NAME_TOKENS` /
  `RATIO_NAME_TOKENS` + `looks_like_measure()`. Shared by the data-source and agent layers
  so the two can never drift apart.

**Debug handle:** every query now logs its dtypes.
`SQL executed rows=10 dtypes={'BRANCH_NAME': 'object', 'total_gross_premium': 'float64'}`
A measure showing `object` means the fix is not in effect.

---

## Scale rules — the table is ~27M rows x 124 columns (4 years)

The app never handles the raw table: SQL Server aggregates and only the RESULT
(<= `DB_MAX_ROWS`) reaches pandas. That only holds because of these, so do not relax them:

- **`SELECT *` is rejected** by `sql_guard.py`, not merely discouraged in the prompt. All
  124 columns x millions of rows would exhaust the worker. The prompt asks; the guard
  enforces.
- **Raw-row queries are capped at `MAX_RAW_ROWS` (100)**; aggregates may use
  `DB_MAX_ROWS`, because an aggregate row already summarises many source rows. An
  oversized `TOP` is lowered, not rejected — the user still gets an answer.
- **Indexes are required at this scale.** `scripts/sqlserver_indexes.sql` creates a
  clustered **columnstore** index (the big win for this workload) plus nonclustered
  indexes for the lookup route and `POLICY_ISSUE_DATE`. Without them every question is an
  ~11 GB scan and `DB_QUERY_TIMEOUT` expires first.
- Raise `DB_QUERY_TIMEOUT` to roughly 4x the slowest measured query once indexed.

## Date rules — `backend/core/date_windows.py`

Dates are resolved to **literal values in Python before the model sees the question**, then
stated to it as fact. A 7B model writing `MAX()`/month arithmetic gets it wrong, and a
wrong range is a confidently wrong answer with no error to catch.

- Explicit date column named by the user wins; otherwise `POLICY_ISSUE_DATE`.
- **"1st/2nd/3rd/4th week" = that week of the LATEST MONTH PRESENT IN THE DATA**
  (`MAX(date column)`), never the current calendar month.
  1st = 1–7 · 2nd = 8–14 · 3rd = 15–21 · 4th = 22–end of month.
- **"Yearly trend" = the CURRENT CALENDAR MONTH compared across available years** —
  `WHERE MONTH(col) = <n> GROUP BY YEAR(col)`. It does *not* mean grouping the whole
  dataset by year. These two rules are different and must not be merged.
- Ranges are half-open (`>= start AND < end`), never `BETWEEN`, so a column with a time
  component cannot silently drop its last day.

## Charts — bar, pie, donut only

`ALLOWED_CHART_TYPES` lives in `config/settings.py` (re-exported by `chart_agent.py`). Line and
scatter were removed deliberately: every question here compares magnitudes across
categories or periods, which a bar chart reads correctly in every case. A period axis is an
ordered bar chart. An explicit "line chart" request is honoured **as a bar** rather than
refused.

Precedence: type named in the question > `CHART_TYPE` in `.env` > `auto` (contribution/share
with <= 8 slices → donut, else bar). A pie/donut becomes a bar **with a warning** on a time
axis or negative values (plotly silently drops negative slices); > 8 slices fold into
"Others (n)" so the total is unchanged. A NULL category is labelled "not available" —
plotly drops null-labelled bars, and the NULL group is often the largest.

### Chart payload is version-neutral — do not regress

Backend and Streamlit are separate processes and need not load the same plotly. A figure
written by plotly 5.x (`fig.to_json()`) carries `layout.template` naming `heatmapgl`, which
plotly 6 rejects: *"Invalid property … layout.template.Data: 'heatmapgl'"* — insight and SQL
fine, chart lost. Seen on the office laptop, 2026-09. `backend/core/figure_codec.py` is the
only writer/reader: it drops the template and decodes plotly-6 base64 arrays to lists, and
the reader uses `skip_invalid=True`. **Never** send `fig.to_json()` or use `pio.from_json` in
the frontend. `/health/detailed` → `charts.plotly_version` shows the backend's version.

## Frontend skin — `frontend/theme.py`

The UNISONIC look (from `required_frontend/test.html`, dark variant) is CSS injected into
Streamlit, not a separate web app — so login, sections, charts, SQL expanders and
`_render_table()` all keep working. It needs `.streamlit/config.toml` (`base = "dark"`);
without it Streamlit's own widgets stay light. The selectors key off Streamlit 1.41's DOM
(`data-testid`, `st-key-<key>` classes, `:has()`), so re-check the look after upgrading
streamlit. User turns are an escaped HTML bubble (`render_user_bubble`), not
`st.chat_message`.

Layout, by the user's decisions (2026-09-24):

- **Sidebar = example questions only** (`EXAMPLE_QUESTIONS` in `theme.py`), shown after
  login. A click asks the question. Still **no Debug toggle**.
- **No Log out button.** A session ends by idling out; `security.get_session` then purges
  it and back-fills its chat-log `logout_time` with the last-activity time.
- **Mascot in the header band** (2026-09-25): `render_header(user_name, role)` draws one
  HTML grid — empty cell | title | name + role + 70px mascot — so the title stays centred
  and nothing overlaps; the band stays 92px tall. The chat panel takes the full width.
  Login screen: header without account/mascot, large mascot beside the form. On a phone
  (≤ 640px) the account drops under the title and the header mascot is hidden. Plain
  static image — no ring, no animation. Since the account is plain HTML now (no Streamlit
  button), there is no keyed chip container any more — do not reintroduce one with a
  shrink-to-fit width: Streamlit hands every child its parent's measured width, and such a
  box once grew itself to 33 million px.
- **Conversation in a centred lane** (`uni-lane`, max 1080px). Being nested one level down
  switches off Streamlit's stick-to-bottom on purpose: it dragged a long answer's question
  and insight out of view. `render_scroll_to_latest` (a zero-size `components.html` script,
  one-shot per new answer) puts the newest question at the top of the window instead. It
  fails silently if a Streamlit upgrade changes the DOM — re-check it with the selectors.
- Sidebar buttons and the chat input only queue `pending_question` (callbacks); the run
  that answers draws both **disabled**, so a click mid-request cannot orphan a question.

## RBAC — `developer` vs `user` (`backend/core/rbac.py`)

`data/users.csv` has a 4th column `role`. Missing column / blank / unknown → `user` (the
office file had no column: everyone is a user until it is added).

- **Enforced server-side**, not only in the UI: `/chat` returns `redact_for_role(...)` on
  every path. A user gets `sql`, `data_preview`, `columns_used`, `warnings` and each
  section's `sql`/`data_preview` emptied, and a failed part's error replaced by a reference
  (the raw DB message quotes the SQL). The frontend additionally hides the Warnings and
  "View SQL & data" expanders unless role is developer.
- The **cache stores the unredacted** response and the **audit log keeps the SQL** for every
  role; redaction is the last step before `return`. Do not reorder.
- The role is **re-read from users.csv on every request** (`security.session_role`), so a
  demotion applies to the next question, not the next login.
- Caveat (`AUTH_MODE=csv` only): login is an email allow-list with no password, so anyone who
  knows a developer's email can claim the role. Documented to the user; not a hard boundary.
  `AUTH_MODE=sso` closes this (see "Login").

## Login — `AUTH_MODE=csv|sso` (`backend/core/sso.py`, `backend/api/auth.py`)

`csv` = the email form against users.csv. `sso` = no form: nginx `auth_request` → oauth2-proxy
→ identity headers → Streamlit reads `st.context.headers` (the websocket request) → forwards
ONLY `sso.IDENTITY_HEADERS` to `POST /auth/sso` with header `X-SSO-Secret` → the backend
extracts the email. Deploy files: `deploy/sso/nginx_sso.conf`, `oauth2-proxy.cfg.example`
(the user's other app used the same pattern under `/entdbot/`; this one uses `/unisonic/`).

Do not regress:

- **Only the four headers nginx_sso.conf `proxy_set_header`s are read** (X-Auth-Request-Email /
  -User / -Preferred-Username, Authorization). nginx passes every other browser header through
  untouched, so reading e.g. `X-Forwarded-Email` would let anyone type an identity. Adding a
  header to sso.py means adding its `proxy_set_header` line to the nginx conf too.
- **Each mode switches the other login OFF**: `/auth/login` 403 in sso (the email form would
  bypass SSO), `/auth/sso` 403 in csv.
- **`/auth/sso` needs `SSO_SHARED_SECRET`** (`hmac.compare_digest`) — otherwise anyone reaching
  :8000 could POST a developer's email. Settings refuse `AUTH_MODE=sso` without a ≥16-char one.
- The ID token's signature and `exp` are NOT checked, deliberately: oauth2-proxy already
  verified it, and it forwards the original token for its whole cookie lifetime, so an `exp`
  check would lock users out an hour after sign-in.
- Role still comes only from users.csv (re-read per request). An unlisted SSO user is a `user`
  with `user_id` = email; `SSO_REQUIRE_USERS_CSV=true` refuses them instead. A listed user keeps
  the users.csv id/name so chat-log rows match across modes.
- `st.context.headers` raises `RuntimeError` with no Streamlit server (AppTest, bare python);
  `_proxy_identity_headers()` turns that into "no identity". Frontend tests patch `st.context`.
- Security depends on deployment: 8501 and 8000 must not be reachable except through nginx.
- Entra ID details (`ENTRA_TENANT_ID/CLIENT_ID/CLIENT_SECRET/REDIRECT_URI`, `SSO_COOKIE_SECRET`,
  `SSO_EMAIL_DOMAINS`) live ONLY in `.env`. The app never calls Entra; oauth2-proxy does.
  `scripts/build_oauth2_proxy_config.py` is the one place they become proxy config
  (`deploy/sso/oauth2-proxy.cfg`, git-ignored). Never hand-edit that file or put the values
  anywhere else. `insecure_oidc_allow_unverified_email = true` is required: Entra sends no
  `email_verified`, and oauth2-proxy would refuse every sign-in without it.

## Read-only data access — three layers

"Nobody, developer or user, may change table data" (the user's "RLS" request):

1. `backend/core/read_only.py` — `find_write_operation()` is the single write rule. Used by
   `sql_guard` and again inside both `run_select()`s (and after DuckDB translation). Scans
   the raw text because T-SQL stacks statements without `;`. Catches `SELECT … INTO`, which
   the old keyword list let through. DuckDB-only words (COPY, LOAD, CALL…) count only at a
   statement start, because `'CALL CENTER'` is real data.
2. `SqlServerDataSource` connects `autocommit=False` and **rolls back after every read**.
3. `scripts/sqlserver_readonly.sql` (DBA, report-only by default): a SELECT-only role with
   DENY on writes, plus an RLS **BLOCK-predicate** security policy keyed on
   `ORIGINAL_LOGIN()` — blocks the chatbot login's INSERT/UPDATE/DELETE, leaves reads and
   the ETL login alone. Verify from the office laptop with
   `python scripts\check_readonly_access.py`. **Not yet run against a live server.**

Startup logs a WARNING if the login can still write; `/health/detailed` →
`data_source.write_permissions`.

## Chat audit log — weekly + monthly files (`backend/services/chat_log_store.py`)

`data/chat_logs/YYYY-MM/chat_logs_YYYY-MM_week1..4.csv` + `…_month.csv`; each row goes to
its week AND month file, by LOCAL date (weeks 1–7 / 8–14 / 15–21 / 22–end, same as
`date_windows`). The month is in the file name because Excel will not open two files with
the same name. Retention keeps `CHAT_LOG_KEEP_MONTHS` (2 = current + previous) and deletes
older `YYYY-MM` folders — only their chat-log files, never anything else. The old
`data/chat_logs.csv` is left alone; `init_data_files.py --split-legacy` migrates it once.

## One section per sub-question

A compound question runs the SQL → chart → insight pipeline **once per part**
(`section_start` → … → `section_end` loop in `graph.py`). Each part gets its own query,
chart and insight; `ChatResponse.sections` carries them and the frontend renders them as
separate blocks.

- A part that fails is recorded in its section and the loop **continues** — one bad part
  must never discard the parts that worked. `finalize` reports `ok` or `partial`.
- `section_start` resets per-section state; both `section_end` and `section_failed`
  advance `section_index`, which is what guarantees termination.
- Retrieval runs **once** for the whole question, not per section.
- `MAX_SECTIONS = 3` bounds latency: each section is a full LLM round trip (30–150s on a
  CPU-only host) and must fit inside `REQUEST_TIMEOUT_SECONDS`.
- Flat `insight`/`charts`/`sql` stay populated as a combined view for older clients.

## The business glossary — `GLOSSARY` in `scripts/build_column_aliases.py`

One block per column, holding every way the business says it plus what it means. It is the
**source of truth**; `backend/knowledge/column_aliases.json` is the generated artifact.

```python
"USGI_SUM_INSURED": {
    "aliases": ["sum insured", "si", "insured amount", "coverage amount"],
    "meaning": "Amount for which the risk is insured under the policy - USGI's share.",
},
```

- `aliases` → the registry resolves the user's words to the physical column, so "coverage
  amount" becomes `[USGI_SUM_INSURED]` in the SQL.
- `meaning` → printed beside the column in the SQL prompt (`also called: … | meaning`) and
  embedded in ChromaDB. Two columns can be described almost identically; what separates
  them is which words the business uses for each.

**Adding a column later:** add one block, then `python scripts\build_column_aliases.py
--rebuild` and `python scripts\setup_chromadb.py`. Nothing else.

**Both data sources share this file.** `aliases` and `meaning` are keyed by column name, so
they carry across unchanged. `category` does not: it is derived from the live type, and
`Live_Count` is `varchar` (dimension) in the local CSV but `int` (measure) in the real
table. Category decides what may be SUMmed and what becomes a date axis, so `--rebuild`
re-derives it from the LIVE schema — which is why the JSON must be regenerated on whichever
machine is pointed at SQL Server, never hand-copied from the laptop that built it against
the CSV. The run also lists any live column with no glossary block.

`--rebuild` takes the glossary verbatim and is how an alias is **removed**; a plain run
unions it with the JSON so a shortcut typed straight into the file survives. The meaning
always comes from the glossary — most descriptions in the JSON were auto-generated from the
column name and must give way.

The build fails, rather than writing, when: a block names a column that is not in the live
table (a typo would otherwise be a silent no-op), two columns claim one alias with no
`PREFERRED` entry, or an alias shadows a different column's real name. That last rule is why
"gross premium" resolves to `GROSS_PREMIUM` and never `USGI_GROSS_PREMIUM`, whatever the
business glossary says — a column's own name always wins.

## Ranking defaults

A ranking question with no number defaults to **`DEFAULT_TOP_N = 5`**, applied *after*
`classify_route` — routing reads `top_n` to tell "the single highest branch"
(`top1_then_trend`) from "the top branches" (`ranking_then_trend`), so defaulting earlier
would collapse that distinction.

## Hard constraints — violating these breaks the app

### Never install `pyarrow`

On Windows it loads native DLLs that shadow the ones `onnxruntime` needs. A later
`import onnxruntime` then raises `DLL load failed while importing
onnxruntime_pybind11_state`, or **non-deterministically hard-crashes the interpreter**
with `Windows fatal exception: access violation`, which no `try/except` can catch.
`onnxruntime` is pulled in by chromadb, so installing pyarrow silently disables semantic
retrieval and can kill the uvicorn worker mid-request.

Consequences that must be preserved:
- `frontend/streamlit_app.py` renders the data preview as HTML via `_render_table()`.
  **Never** use `st.dataframe` / `st.table` — both need Arrow.
- Parquet is written by **DuckDB**, never `pandas.to_parquet`.
- `tests/test_import_order.py` fails the build if pyarrow reappears.

Fixing this by import *order* was tried and rejected: it only holds if `backend` is
imported before pandas, which nothing guarantees. `backend/__init__.py` is deliberately
import-free.

### Numbers are never re-typed by a model

Three tiers in `insight_agent.py`: **direct** (single-row result formatted straight from
the DataFrame, no LLM), **verified narrative** (LLM prose whose every figure is checked
against the values it was given), **template** (deterministic fallback). Any unsupported
figure or invented currency symbol rejects the narrative. The source columns carry no
currency, so any `$`/`INR` the model adds is fabricated.

Temperature follows the same rule: anything producing or restating a number runs at
`0.0`; only free prose uses `LLM_TEXT_TEMPERATURE`.

### Ambiguity is refused, never guessed

`column_registry.py` drops any shortcut claimed by two columns. Silently picking one is how
a chatbot returns a confidently wrong number.

The single exception is `preferred_shortcuts` in the alias file, fed by `PREFERRED` in
`scripts/build_column_aliases.py`: a contested phrase the business has actually decided on.
That is a recorded decision, not a guess, and every entry carries its reason. A preference
for an uncontested phrase, or one naming a column that does not claim the phrase, is
ignored — so a typo there cannot redirect a question. A preference can never override a
column's own real name.

### Generated SQL is always T-SQL

Both backends receive Microsoft T-SQL. In local mode `sql_dialect.py` translates it to
DuckDB immediately before execution. An untranslatable construct raises an error **naming
that construct** rather than returning different numbers.

---

## Commands

```bash
setup.bat                                    # one-time: venv + deps + data files
run_backend.bat                              # uvicorn on :8000
run_frontend.bat                             # streamlit on :8501

python scripts\test_db_connection.py         # which data source is active + columns
python scripts\diagnose_sqlserver.py         # 7 PASS/FAIL checks on the chart/insight chain
python scripts\diagnose_sqlserver.py --question "High-performing branch region-wise."
python scripts\build_column_aliases.py --rebuild   # apply GLOSSARY edits (aliases + meanings)
python scripts\build_column_aliases.py --check     # validate only; names every clash
python scripts\build_column_aliases.py --show "coverage amount"   # what a phrase resolves to
python scripts\setup_chromadb.py             # rebuild the semantic index (per provider)
python scripts\check_readonly_access.py      # PASS/FAIL: is the DB login really read-only?
python scripts\init_data_files.py --split-legacy   # one-off: old chat_logs.csv -> weekly files
python scripts\init_data_files.py --merge-pending  # fold Excel-lock spill files back in

pytest                                       # no DB / Ollama / ChromaDB needed
pytest tests\test_sqlserver_result_shape.py  # the regression suite for the bug above
```

`GET /health/detailed` reports data source, LLM provider, configured vs available models,
ChromaDB, column registry and cache — each independently.

## Degradation, not failure

Redis down → in-memory cache. ChromaDB/native deps unavailable → lexical column matching
via the registry. LLM down → deterministic template insight. A chat-log week/month file
locked by Excel → that row goes to its sibling `….pending.csv` and the other file is still
written (merge with `python scripts\init_data_files.py --merge-pending`). Any agent node raising → a graceful
error state, never a crashed request.

## Conventions

- Comments explain **why**, never what. Most code needs none.
- Errors must name the real cause and the fix. The LLM client distinguishes unreachable
  host / model-not-pulled / timeout / bad key — lumping them together sent users to check
  a server that was running fine.
- `.env` is git-ignored and holds real credentials. Never hard-code them.
- Python 3.11 is pinned — chromadb and pyodbc have the most reliable Windows wheels there.
