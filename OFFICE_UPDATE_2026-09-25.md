# Office laptop update — 2026-09-25

Ye guide personal laptop ke naye code ko office copy (`D:\PROJECT\UAT\power_bi_chat\v2_2`) mein
haath se laane ke liye hai. Office copy is folder se sync NAHI hai, isliye purani files ko
poora replace mat karna — sirf neeche diye blocks badalna. Har block ko baseline code par
apply karke check kiya gaya hai ki result exact naya code banta hai.

**Kaise padhein:** "REPLACE" = us function/block ko shuru se aakhri line tak hata ke naya
paste karo. "ADD AFTER" = bataye gaye block ki aakhri line ke baad paste karo (top-level
functions ke beech 2 khaali lines). "DELETE" = dikhaya gaya block hata do.

## Step 0 — Backup

Office folder ki ek copy bana lo (`v2_2_backup_2026-09-25`). Backend aur frontend band kar do.

## Step 1 — Nayi files: poori copy karo (in-ko pehle, kyunki baaki code inhe import karta hai)

- `backend/core/rbac.py`
- `backend/core/read_only.py`
- `backend/services/chat_log_store.py`
- `scripts/sqlserver_readonly.sql`
- `scripts/check_readonly_access.py`
- `tests/test_rbac.py`
- `tests/test_read_only.py`
- `tests/test_chat_log_partitions.py`

## Step 2 — Purani code files: blocks badlo (isi order mein)

### 2.1 `config/settings.py` — Nayi settings (CHAT_LOGS_DIR, CHAT_LOG_KEEP_MONTHS)

**1. ADD AFTER** `class Settings` ke andar, assignment `USERS_CSV = ...` — ye naya block:

```python
    # Chat audit log: one folder per calendar month holding week1..week4 + month CSVs.
    # CHAT_LOG_KEEP_MONTHS counts calendar months INCLUDING the current one; older month
    # folders are deleted automatically.
    CHAT_LOGS_DIR: str = "./data/chat_logs"
```

**2. ADD AFTER** `class Settings` ke andar, assignment `CHAT_LOGS_DIR = ...` — ye naya block:

```python
    CHAT_LOG_KEEP_MONTHS: int = 2
```

**3. REPLACE** `class Settings` ke andar, assignment `CHAT_LOGS_CSV = ...`. Naya version:

```python
    # The pre-partition single-file log. Nothing writes it any more; it stays configurable
    # so `init_data_files.py --split-legacy` can import it.
    CHAT_LOGS_CSV: str = "./data/chat_logs.csv"
```

**4. ADD AFTER** `class Settings` ke andar, poora `def _check_chart_type(...)` function — ye naya block:

```python
    @field_validator("CHAT_LOG_KEEP_MONTHS")
    @classmethod
    def _check_chat_log_keep_months(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                f"CHAT_LOG_KEEP_MONTHS must be 1 or more (it counts the current month), "
                f"got {value}. Set CHAT_LOG_KEEP_MONTHS=2 in .env to keep this month and "
                "the previous one."
            )
        return value
```

### 2.2 `backend/services/logging_service.py` — Weekly/monthly log store ko use karta hai

**1. REPLACE** file ke top par import lines ka block. Naya version:

```python
from datetime import datetime

from backend.core.logging_config import get_logger
from backend.services.chat_log_store import ChatLogStore
from config.settings import get_settings
```

**2. ADD AFTER** assignment `FIELDNAMES = ...` — ye naya block:

```python
def get_chat_log_store() -> ChatLogStore:
    return ChatLogStore(
        settings.resolved(settings.CHAT_LOGS_DIR), FIELDNAMES, settings.CHAT_LOG_KEEP_MONTHS
    )
```

**3. REPLACE** poora `def ensure_log_file(...)` function. Naya version:

```python
def ensure_log_file() -> None:
    get_chat_log_store().ensure()
```

**4. REPLACE** poora `def log_interaction(...)` function. Naya version:

```python
def log_interaction(
    user_id: str,
    user_name: str,
    user_question: str,
    generated_sql_query: str,
    generated_output: str,
    login_time: str,
    ques_no: int,
) -> None:
    row = {
        "user_id": user_id,
        "user_name": user_name,
        "user_question": user_question,
        "generated_sql_query": (generated_sql_query or "").replace("\n", " ").strip(),
        "generated_output": (generated_output or "").replace("\n", " ").strip(),
        "login_time": login_time,
        "logout_time": "",
        "ques_no": ques_no,
    }
    try:
        # A file open in Excel is handled inside: the row goes to that file's
        # .pending.csv sibling, so an audit row is never dropped.
        get_chat_log_store().append(row)
    except Exception as exc:  # noqa: BLE001 - never let logging break a response
        logger.error("Failed to write chat log row (non-fatal, response still returned): %s", exc)
```

**5. REPLACE** poora `def update_logout_time(...)` function. Naya version:

```python
def update_logout_time(user_id: str, login_time: str, logout_time: str | None = None) -> None:
    logout_ts = logout_time or datetime.utcnow().isoformat()
    try:
        get_chat_log_store().backfill_logout(user_id, login_time, logout_ts)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to update logout_time (non-fatal): %s", exc)
```

**6. DELETE** poora `def _paths(...)` function. Pehchaanne ke liye purana code:

```python
def _paths():
    csv_path = settings.resolved(settings.CHAT_LOGS_CSV)
    return csv_path, csv_path.with_suffix(csv_path.suffix + ".lock")
```

**7. DELETE** poora `def _spill(...)` function. Pehchaanne ke liye purana code:

```python
def _spill(row: dict, csv_path, reason: str) -> None:
    """Last-resort write to chat_logs.pending.csv so an audit row is never dropped.

    Merge it back with: python scripts/init_data_files.py --merge-pending
    """
    pending = csv_path.with_name(csv_path.stem + ".pending.csv")
    try:
        is_new = not pending.exists() or pending.stat().st_size == 0
        with open(pending, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
        logger.warning(
            "Could not write %s (%s). Row appended to %s instead - close the file in Excel "
            "and merge it back.", csv_path.name, reason, pending.name,
        )
    except Exception as exc:  # noqa: BLE001 - never let logging break a response
        logger.error("Chat log row lost (both primary and pending writes failed): %s", exc)
```

**8. REPLACE** file ke top wala docstring *(optional — sirf comment/docstring)*. Naya version:

```python
"""Chat audit log writer. Rows are partitioned into weekly + monthly CSV files under
CHAT_LOGS_DIR, and month folders older than CHAT_LOG_KEEP_MONTHS are deleted automatically -
see backend/services/chat_log_store.py for the layout.

Columns exactly as specified: user_id, user_name, user_question, generated_sql_query,
generated_output, login_time, logout_time, ques_no. login_time is written on every row
of a session and doubles as the session key when logout_time is back-filled at logout.
"""
```

### 2.3 `backend/services/user_service.py` — users.csv se role padhta hai

**1. REPLACE** file ke top par import lines ka block. Naya version:

```python
import csv
from typing import Optional

from backend.core.logging_config import get_logger
from backend.core.rbac import normalize_role
from config.settings import get_settings
```

**2. REPLACE** poora `def find_user_by_email(...)` function. Naya version:

```python
def find_user_by_email(email: str) -> Optional[dict]:
    path = settings.resolved(settings.USERS_CSV)
    if not path.exists():
        logger.error("users.csv not found at %s", path)
        return None
    email_norm = email.strip().lower()
    try:
        # utf-8-sig: Excel's "CSV UTF-8" prepends a BOM that would otherwise glue onto
        # the first header, so no "user_id" column is found and every user is locked out.
        # Headers match case/space-insensitively because the role column is typed by hand.
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for raw in csv.DictReader(f):
                row = {
                    k.strip().lower(): (v or "").strip()
                    for k, v in raw.items() if isinstance(k, str)  # None = surplus cells
                }
                if row.get("user_email", "").lower() == email_norm:
                    return {
                        "user_id": row["user_id"],
                        "user_name": row["user_name"],
                        "user_email": row["user_email"],
                        "role": normalize_role(row.get("role")),
                    }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed reading users.csv: %s", exc)
        return None
    return None
```

**3. REPLACE** file ke top wala docstring *(optional — sirf comment/docstring)*. Naya version:

```python
"""Allow-list authentication backed by data/users.csv (user_id, user_name, user_email, role).

The role column is optional: a file without it still works and everyone is a "user".
"""
```

### 2.4 `backend/models/schemas.py` — Login response mein role

**1. REPLACE** poori `class LoginResponse` class. Naya version:

```python
class LoginResponse(BaseModel):
    session_id: str
    user_id: str
    user_name: str
    # "developer" | "user". Only drives what the UI offers - /chat enforces it server-side.
    role: str = "user"
```

### 2.5 `backend/core/security.py` — Session role + idle session purge + logout_time

**1. REPLACE** file ke top par import lines ka block. Naya version:

```python
import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

from backend.core.logging_config import get_logger
from backend.core.rbac import ROLE_USER, normalize_role
from backend.services import logging_service
from backend.services.user_service import find_user_by_email
from config.settings import get_settings
```

**2. REPLACE** poora `def _load(...)` function. Naya version:

```python
def _load() -> None:
    path = _sessions_path()
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for sid, s in raw.items():
            s["login_time"] = datetime.fromisoformat(s["login_time"])
            s["last_activity"] = datetime.fromisoformat(s["last_activity"])
            # Sessions saved before roles existed carry none - they must not gain access.
            s["role"] = normalize_role(s.get("role"))
        _sessions.update(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load sessions file (starting fresh): %s", exc)
```

**3. REPLACE** poora `def create_session(...)` function. Naya version:

```python
def create_session(user_id: str, user_name: str, user_email: str, role: str = ROLE_USER) -> dict:
    session_id = str(uuid.uuid4())
    now = datetime.utcnow()
    with _lock:
        _sessions[session_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "user_email": user_email,
            "role": normalize_role(role),
            "login_time": now,
            "last_activity": now,
            "ques_no": 0,
            "chat_history": [],
        }
        _save()
    logger.info("Session created user_id=%s role=%s session_id=%s",
                user_id, _sessions[session_id]["role"], session_id)
    return _sessions[session_id]
```

**4. ADD AFTER** poora `def create_session(...)` function — ye naya block:

```python
def session_role(session: Optional[dict]) -> str:
    """The role /chat enforces, read from users.csv on every call.

    Not the role stored at login: a session lives as long as it keeps asking, so a
    developer demoted in users.csv would otherwise keep seeing SQL indefinitely. Anyone
    no longer found there - or a users.csv that cannot be read - is a user.
    """
    email = str((session or {}).get("user_email") or "").strip()
    user = find_user_by_email(email) if email else None
    return normalize_role(user.get("role")) if user else ROLE_USER
```

**5. ADD AFTER** poora `def session_role(...)` function — ye naya block:

```python
def _pop_expired() -> list[dict]:
    now = datetime.utcnow()
    limit = timedelta(minutes=settings.SESSION_TIMEOUT_MINUTES)
    with _lock:
        expired = [sid for sid, s in _sessions.items() if now - s["last_activity"] > limit]
        if not expired:
            return []
        popped = [_sessions.pop(sid) for sid in expired]
        _save()
    return popped
```

**6. ADD AFTER** poora `def _pop_expired(...)` function — ye naya block:

```python
def _record_expired_logouts(expired: list[dict]) -> None:
    # There is no Log out button, so an idle-out is how nearly every session ends. Its
    # logout_time is when the user was last seen, not when the purge happened to run.
    for session in expired:
        logger.info("Session expired (idle) user_id=%s session_id=%s",
                    session["user_id"], session["session_id"])
        logging_service.update_logout_time(
            session["user_id"], session["login_time"].isoformat(),
            logout_time=session["last_activity"].isoformat(),
        )
```

**7. REPLACE** poora `def get_session(...)` function. Naya version:

```python
def get_session(session_id: str) -> Optional[dict]:
    # Purged here rather than only when the expired id is asked for: a closed tab never
    # asks again, and its session would otherwise sit in sessions.json forever.
    _record_expired_logouts(_pop_expired())
    with _lock:
        return _sessions.get(session_id)
```

### 2.6 `backend/agents/sql_guard.py` — read_only.py wala write-check

**1. REPLACE** file ke top par import lines ka block. Naya version:

```python
import re

from backend.core.read_only import describe_write_operation, find_write_operation
from config.settings import get_settings
```

**2. REPLACE** poora `def validate_sql(...)` function. Naya version:

```python
def validate_sql(sql: str) -> str:
    """Returns a sanitized, safe-to-execute SQL string or raises SQLValidationError."""
    sql = _strip_fences(sql)

    if not sql:
        raise SQLValidationError("The model returned no SQL statement.")

    if ";" in sql:
        raise SQLValidationError("Multiple SQL statements are not allowed.")

    if not _SELECT_START.match(sql):
        raise SQLValidationError("Only SELECT statements are allowed.")

    offending = find_write_operation(sql)
    if offending:
        raise SQLValidationError(
            "Generated SQL contains a disallowed keyword: "
            f"{describe_write_operation(offending)}. Only read-only SELECT queries are allowed."
        )

    sql = _relocate_trailing_top(sql)

    allowed_table = _normalize_table_name(settings.DB_TABLE)
    referenced = [_normalize_table_name(t) for t in _TABLE_REF.findall(sql)]
    if not referenced:
        raise SQLValidationError("Could not determine the table referenced by the SQL.")

    # Names introduced by the statement itself (CTEs, derived tables) are legitimate.
    local_names = {
        name.lower()
        for name in re.findall(r"(?i)\b(?:WITH|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", sql)
    }
    allowed_names = {allowed_table, allowed_table.split(".")[-1], *local_names}
    for table in referenced:
        if table not in allowed_names:
            raise SQLValidationError(
                f"Generated SQL references an unexpected table '{table}'. "
                f"Only {settings.DB_TABLE} is permitted."
            )

    # `SELECT *` on a table of tens of millions of rows x 124 columns would stream the
    # whole thing into pandas and exhaust the worker's memory. The prompt forbids it, but
    # a prompt is a request - this is the enforcement.
    if _STAR_SELECT.search(sql):
        raise SQLValidationError(
            "SELECT * is not allowed on this table - it would return every one of its "
            "124 columns. Name the columns you need, or aggregate with SUM/COUNT/AVG "
            "and GROUP BY."
        )

    sql = _enforce_row_cap(sql)
    return sql
```

**3. DELETE** assignment `_FORBIDDEN = ...`. Pehchaanne ke liye purana code:

```python
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|EXEC|EXECUTE|MERGE|CREATE|GRANT|REVOKE|"
    r"BACKUP|RESTORE|SHUTDOWN|xp_\w+|sp_\w+)\b",
    re.IGNORECASE,
)
```

**4. REPLACE** file ke top wala docstring *(optional — sirf comment/docstring)*. Naya version:

```python
"""Validates and hardens LLM-generated SQL before it ever reaches the data source.

Defense in depth for a single-table, read-only app:
  1. Must be exactly one SELECT statement (no DDL/DML, no stacked statements).
  2. Must reference only the configured table (dbo.May_2) - blocks table-name
     hallucination and any attempt to pivot to a different object.
  3. Anything that could write is blocked outright regardless of position. The rule lives
     in backend/core/read_only.py, shared with the data sources' second check.
  4. A row cap (TOP N) is injected if the model omitted one.
  5. Misplaced clauses the model emits when it runs out of context - most often a stray
     `TOP 1` appended after ORDER BY - are repaired rather than sent to the database,
     where they only produce "Incorrect syntax near the keyword 'TOP'".
"""
```

### 2.7 `backend/core/datasource.py` — Read-only guard, rollback, write_permissions()

**1. REPLACE** file ke top par import lines ka block. Naya version:

```python
from __future__ import annotations

import datetime as dt
import re
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd

from backend.core.identifiers import looks_like_measure
from backend.core.logging_config import get_logger
from backend.core.read_only import describe_write_operation, find_write_operation
from config.settings import LOCAL_CSV_SUFFIXES, get_settings
```

**2. ADD AFTER** poori `class DatabaseError` class — ye naya block:

```python
def refuse_writes(sql: str) -> None:
    """Second check behind sql_guard: run_select also serves callers that never pass
    through the guard (date_windows, the scripts' --sql option)."""
    offending = find_write_operation(sql)
    if offending:
        raise DatabaseError(
            "Refused to run a statement that could change data: "
            f"{describe_write_operation(offending)}. Only read-only SELECT queries are allowed."
        )
```

**3. REPLACE** poori `class DataSource` class. Naya version:

```python
class DataSource(ABC):
    """Interface every backend must implement. Kept deliberately narrow."""

    name: str = "abstract"

    @abstractmethod
    def check_connection(self) -> tuple[bool, str]:
        """Never raises - health checks must always return a verdict."""

    @abstractmethod
    def get_table_columns(self) -> list[dict]:
        """[{'column_name': ..., 'data_type': ...}] in physical column order."""

    @abstractmethod
    def run_select(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """Execute an already-validated SELECT (see backend/agents/sql_guard.py)."""

    def describe(self) -> str:
        return self.name

    def write_permissions(self) -> list[str] | None:
        """Write permissions the connection holds on DB_TABLE; None = could not tell.

        Never raises. A local file has no login, so there is nothing to hold.
        """
        return []
```

**4. REPLACE** `class SqlServerDataSource` ke andar, poora `def _raw_connect(...)` function. Naya version:

```python
    def _raw_connect(self):
        from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

        pyodbc = self._pyodbc

        @retry(
            stop=stop_after_attempt(2),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(pyodbc.OperationalError),
            reraise=True,
        )
        def _attempt():
            # Explicit, not the driver default: with autocommit off every statement sits in
            # a transaction that _end_read() rolls back, so nothing can persist.
            return pyodbc.connect(
                settings.odbc_connection_string,
                timeout=settings.DB_CONNECT_TIMEOUT,
                autocommit=False,
            )

        return _attempt()
```

**5. ADD AFTER** `class SqlServerDataSource` ke andar, poora `def _connection(...)` function — ye naya block:

```python
    @staticmethod
    def _end_read(cursor, conn) -> None:
        """Roll back whatever the statement did. Free for a read; the backstop if a write
        ever got past both guards. The cursor goes first because a partly fetched result
        keeps the connection busy. Neither step may mask the error that led here."""
        try:
            cursor.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Closing the cursor failed: %s", exc)
        try:
            conn.rollback()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ROLLBACK after a read failed (%s); closing the connection discards the "
                "transaction instead.", exc,
            )
```

**6. REPLACE** `class SqlServerDataSource` ke andar, poora `def run_select(...)` function. Naya version:

```python
    def run_select(self, sql: str, max_rows: int | None = None) -> QueryResult:
        pyodbc = self._pyodbc
        refuse_writes(sql)
        max_rows = max_rows or settings.DB_MAX_ROWS
        try:
            with self._connection() as conn:
                conn.timeout = settings.DB_QUERY_TIMEOUT
                cursor = conn.cursor()
                try:
                    cursor.execute(sql)
                    columns = [c[0] for c in cursor.description] if cursor.description else []
                    rows = cursor.fetchmany(max_rows + 1)
                finally:
                    self._end_read(cursor, conn)
        except pyodbc.OperationalError as exc:
            if "timeout" in str(exc).lower():
                raise DatabaseError(
                    "The database query timed out. Please refresh the page."
                ) from exc
            raise DatabaseError(f"Database query failed: {exc}") from exc
        except pyodbc.ProgrammingError as exc:
            raise DatabaseError(f"The generated SQL was invalid: {exc}") from exc

        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        columns = repair_column_names(columns)
        frame = (
            pd.DataFrame.from_records([tuple(r) for r in rows], columns=columns)
            if rows
            else pd.DataFrame(columns=columns)
        )
        # Decimal/date objects from pyodbc would otherwise leave every measure as an
        # object column, which switches off charting and empties the insight statistics.
        frame = normalize_result_frame(frame)
        logger.info(
            "SQL executed rows=%s truncated=%s dtypes=%s",
            len(frame), truncated, {c: str(frame[c].dtype) for c in frame.columns},
        )
        return QueryResult(
            dataframe=frame,
            row_count=len(frame),
            truncated=truncated,
            columns=list(frame.columns),
        )
```

**7. ADD AFTER** `class SqlServerDataSource` ke andar, poora `def run_select(...)` function — ye naya block:

```python
    _WRITE_PERMISSIONS = ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE TABLE")
```

**8. ADD AFTER** `class SqlServerDataSource` ke andar, assignment `_WRITE_PERMISSIONS = ...` — ye naya block:

```python
    def write_permissions(self) -> list[str] | None:
        # HAS_PERMS_BY_NAME answers for THIS login after every GRANT, DENY and role
        # membership. It cannot see the RLS block predicate - check_readonly_access.py does.
        sql = (
            "SELECT HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'INSERT'), "
            "HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'UPDATE'), "
            "HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'DELETE'), "
            "HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'ALTER'), "
            "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE') "
            "FROM (SELECT QUOTENAME(?) + '.' + QUOTENAME(?) AS name) AS t"
        )
        schema, table = split_table(settings.DB_TABLE)
        try:
            with self._connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(sql, schema, table)
                    row = cursor.fetchone()
                finally:
                    self._end_read(cursor, conn)
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never raise
            logger.warning("Could not check write permissions on %s: %s", settings.DB_TABLE, exc)
            return None
        # NULL means SQL Server could not resolve the table for this login - not "no".
        if row is None or any(value is None for value in row):
            return None
        return [name for name, value in zip(self._WRITE_PERMISSIONS, row) if int(value) == 1]
```

**9. REPLACE** `class LocalFileDataSource` ke andar, poora `def run_select(...)` function. Naya version:

```python
    def run_select(self, sql: str, max_rows: int | None = None) -> QueryResult:
        from backend.core.sql_dialect import SQLDialectError, translate

        refuse_writes(sql)
        connection = self._ensure_loaded()
        max_rows = max_rows or settings.DB_MAX_ROWS

        try:
            duck_sql = translate(sql)
        except SQLDialectError as exc:
            raise DatabaseError(f"The generated SQL was invalid: {exc}") from exc
        # Checked again because the translation, not the T-SQL, is what DuckDB runs - and
        # DuckDB runs every `;`-separated statement it is given.
        refuse_writes(duck_sql)

        logger.debug("Translated T-SQL -> DuckDB:\n%s", duck_sql)
        try:
            with self._lock:
                cursor = connection.execute(duck_sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                rows = cursor.fetchmany(max_rows + 1)
        except Exception as exc:  # noqa: BLE001 - duckdb raises many narrow types
            raise DatabaseError(f"The generated SQL was invalid: {exc}") from exc

        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        columns = repair_column_names(columns)
        frame = (
            pd.DataFrame.from_records(rows, columns=columns)
            if rows
            else pd.DataFrame(columns=columns)
        )
        # Applied to both backends so "works locally" predicts "works on SQL Server".
        frame = normalize_result_frame(frame)
        logger.info(
            "SQL executed rows=%s truncated=%s dtypes=%s",
            len(frame), truncated, {c: str(frame[c].dtype) for c in frame.columns},
        )
        return QueryResult(
            dataframe=frame,
            row_count=len(frame),
            truncated=truncated,
            columns=list(frame.columns),
        )
```

**10. REPLACE** file ke top wala docstring *(optional — sirf comment/docstring)*. Naya version:

```python
"""Pluggable data-source layer.

The same agent pipeline runs against either enterprise SQL Server or a local file
(.csv or .xlsx), selected by DATA_SOURCE in .env. Exactly one backend is ever
constructed: the unselected one is never imported, so local mode needs no ODBC driver and
SQL Server mode needs no DuckDB/openpyxl.

    DATA_SOURCE=sqlserver  ->  SqlServerDataSource  (pyodbc, T-SQL executed as generated)
    DATA_SOURCE=local      ->  LocalFileDataSource  (DuckDB over the .csv / .xlsx)

Both expose the identical interface used by the rest of the app:

    check_connection()   -> (ok: bool, detail: str)
    get_table_columns()  -> [{"column_name": str, "data_type": str}, ...]
    run_select(sql)      -> QueryResult  (refuses anything that could write)
    write_permissions()  -> ["INSERT", ...] held on DB_TABLE, [] if none, None if unknown

How each local format is read, and why:

  * .csv  - DuckDB's own read_csv_auto. It is built into the DuckDB binary (no extension
            download, so it works air-gapped), streams the file instead of materialising
            it, and infers types well. Routing a 100 MB CSV through pandas first would
            cost hundreds of MB of RAM for no benefit.
  * .xlsx - pandas.read_excel + openpyxl. DuckDB's Excel support lives in an extension
            that is fetched from the internet on first use, which fails on the air-gapped
            on-prem hosts this app targets, and its type inference over a 124-column
            sheet is weaker than pandas'.

Either way the parsed table is cached to Parquet under data/cache/, keyed by the source
file's modification time and size, so only the first start pays the parsing cost and
replacing the file invalidates the cache automatically.
"""
```

### 2.8 `backend/api/auth.py` — Login role return karta hai

**1. REPLACE** poora `def login(...)` function. Naya version:

```python
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    user = user_service.find_user_by_email(payload.email)
    if not user:
        raise HTTPException(
            status_code=403,
            detail="Access denied. This email is not in the authorized users list - contact your admin.",
        )
    session = security.create_session(
        user["user_id"], user["user_name"], user["user_email"], role=user.get("role", "user"),
    )
    return LoginResponse(
        session_id=session["session_id"], user_id=user["user_id"], user_name=user["user_name"],
        role=security.session_role(session),
    )
```

### 2.9 `backend/api/chat.py` — User ke liye redaction

**1. REPLACE** file ke top par import lines ka block. Naya version:

```python
import asyncio
import json
import time
import uuid

from fastapi import APIRouter, HTTPException

from backend.agents.graph import get_graph
from backend.core.cache import cache_key, get_cache
from backend.core.logging_config import get_logger
from backend.core import security
from backend.core.rbac import redact_for_role
from backend.models.schemas import ChartPayload, ChatRequest, ChatResponse, SectionPayload
from backend.services import logging_service
from config.settings import get_settings
```

**2. REPLACE** poora `def chat(...)` function. Naya version:

```python
@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest):
    request_id = str(uuid.uuid4())
    session = security.get_session(payload.session_id)
    if not session:
        raise HTTPException(
            status_code=401,
            detail="Your session has expired or is invalid. Please refresh the page and log in again.",
        )
    # Redaction is the LAST step on every path below: the cache and the chat log keep the
    # full response, so a developer still gets SQL for a question a user asked first.
    role = security.session_role(session)

    ques_no = security.touch_session(payload.session_id) or 0
    cache = get_cache()
    key = cache_key(payload.question)

    cached = cache.get(key)
    if cached:
        logger.info("Cache hit request_id=%s", request_id, extra={"request_id": request_id})
        response = ChatResponse(**{**cached, "request_id": request_id, "cache_hit": True, "ques_no": ques_no})
        logging_service.log_interaction(
            session["user_id"], session["user_name"], payload.question,
            response.sql, response.insight, session["login_time"].isoformat(), ques_no,
        )
        return redact_for_role(response, role)

    initial_state = {
        "request_id": request_id,
        "user_id": session["user_id"],
        "user_name": session["user_name"],
        "raw_question": payload.question,
        "chat_history": session.get("chat_history", []),
        "sql_attempts": 0,
        "sections": [],
        "section_index": 0,
        "warnings": [],
        "timings_ms": {},
    }

    t0 = time.time()
    try:
        final_state = await asyncio.wait_for(
            asyncio.to_thread(_run_graph_sync, initial_state),
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Request timed out request_id=%s question=%s", request_id, payload.question,
                     extra={"request_id": request_id})
        response = ChatResponse(
            status="timeout", request_id=request_id,
            error_message="Request timed out. Please refresh the page.", ques_no=ques_no,
        )
        logging_service.log_interaction(
            session["user_id"], session["user_name"], payload.question,
            "", "TIMEOUT: Please refresh the page.", session["login_time"].isoformat(), ques_no,
        )
        return redact_for_role(response, role)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled pipeline error request_id=%s: %s", request_id, exc,
                      extra={"request_id": request_id})
        response = ChatResponse(
            status="error", request_id=request_id,
            error_message="Something went wrong processing your question. Please refresh the page and try again.",
            ques_no=ques_no,
        )
        logging_service.log_interaction(
            session["user_id"], session["user_name"], payload.question,
            "", response.error_message, session["login_time"].isoformat(), ques_no,
        )
        return redact_for_role(response, role)

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    security.append_history(payload.session_id, payload.question, final_state.get("rewritten_question", payload.question))

    response = _build_response(request_id, final_state, cache_hit=False, ques_no=ques_no)
    response.timings_ms["total"] = elapsed_ms

    if response.status == "ok":
        cache.set(key, response.model_dump(exclude={"request_id", "cache_hit", "ques_no"}))

    logging_service.log_interaction(
        session["user_id"], session["user_name"], payload.question,
        response.sql, response.insight or (response.error_message or ""),
        session["login_time"].isoformat(), ques_no,
    )

    logger.info("chat request_id=%s status=%s elapsed_ms=%s", request_id, response.status, elapsed_ms,
                extra={"request_id": request_id})
    return redact_for_role(response, role)
```

### 2.10 `backend/api/health.py` — health mein write_permissions

**1. ADD AFTER** poora `def health(...)` function — ye naya block:

```python
def _write_permissions():
    """[] = read-only, a list = what the login could change, None = could not tell."""
    try:
        return get_datasource().write_permissions()
    except Exception:  # noqa: BLE001 - a health check must always answer
        return None
```

**2. REPLACE** poora `def health_detailed(...)` function. Naya version:

```python
@router.get("/detailed")
def health_detailed():
    try:
        source = get_datasource()
        db_ok, db_msg = source.check_connection()
        source_detail = source.describe()
    except Exception as exc:  # noqa: BLE001 - a health check must always answer
        db_ok, db_msg, source_detail = False, str(exc), settings.describe_source()

    llm_ok, llm_msg = check_llm()
    chroma_ok, chroma_msg = collections_ready()
    cache_status = get_cache().status()

    registry = get_registry()
    registry_info = {
        "columns": len(registry.columns),
        "shortcuts": registry.shortcut_count,
        "ambiguous_shortcuts_ignored": len(registry.collisions),
    }

    overall = "ok" if all([db_ok, llm_ok, chroma_ok]) else "degraded"
    return {
        "status": overall,
        "data_source": {
            "mode": settings.DATA_SOURCE,
            "ok": db_ok,
            "detail": db_msg,
            "target": source_detail,
            # Skipped when down: it would only wait out a second connect timeout.
            "write_permissions": _write_permissions() if db_ok else None,
        },
        # Kept for backwards compatibility with anything reading the old key.
        "sql_server": {"ok": db_ok, "detail": db_msg},
        "llm": {
            "provider": settings.LLM_PROVIDER,
            "ok": llm_ok,
            "detail": llm_msg,
            "target": settings.describe_llm(),
            "models_configured": settings.configured_models,
            "models_available": list_models(),
        },
        "chromadb": {"ok": chroma_ok, "detail": chroma_msg},
        "column_registry": registry_info,
        "cache": cache_status,
        "charts": {
            "chart_type": settings.CHART_TYPE,
            # Compare with the frontend's `python -c "import plotly; print(plotly.__version__)"`
            # when a chart fails to render but the insight is fine.
            "plotly_version": plotly.__version__,
        },
    }
```

### 2.11 `backend/main.py` — Startup par write-permission WARNING

**1. REPLACE** poora `def on_startup(...)` function. Naya version:

```python
@app.on_event("startup")
def on_startup():
    """Verify every dependency and report precisely which one is unhealthy.

    Nothing here is fatal: the app still starts so /health/detailed can be used to
    diagnose the problem, and each failing subsystem degrades on its own terms.
    """
    logger.info("=" * 78)
    logger.info("Starting backend - verifying dependencies (failures are logged, not fatal)")
    logger.info("Active data source: %s", settings.describe_source())
    logger.info("Active LLM provider: %s", settings.describe_llm())
    logging_service.ensure_log_file()

    try:
        source = get_datasource()
        db_ok, db_msg = source.check_connection()
    except Exception as exc:  # noqa: BLE001 - a misconfigured backend must not stop startup
        db_ok, db_msg = False, str(exc)
    logger.info("Data source (%s): %s (%s)", settings.DATA_SOURCE,
                "OK" if db_ok else "UNAVAILABLE", db_msg)

    if db_ok:
        # The app only reads. A login that can write turns any guard bypass into data loss.
        try:
            write_perms = source.write_permissions()
        except Exception:  # noqa: BLE001 - e.g. an older datasource.py without the method
            write_perms = None
        if write_perms:
            logger.warning(
                "The data source login can WRITE to %s (%s). The chatbot never needs this - "
                "ask the DBA to run scripts/sqlserver_readonly.sql.",
                settings.DB_TABLE, ", ".join(write_perms),
            )
        elif write_perms is None:
            logger.warning(
                "Could not check whether the data source login can write to %s - run "
                "python scripts/check_readonly_access.py.", settings.DB_TABLE,
            )

    llm_ok, llm_msg = check_llm()
    logger.info(
        "LLM (%s): %s (%s)", settings.LLM_PROVIDER,
        "OK" if llm_ok else "UNAVAILABLE", llm_msg,
    )

    if db_ok:
        registry = get_registry(refresh=True)
        logger.info(
            "Column registry: %s columns, %s shortcuts%s",
            len(registry.columns), registry.shortcut_count,
            f", {len(registry.collisions)} ambiguous ignored" if registry.collisions else "",
        )
    else:
        logger.warning("Column registry not built - the data source is unreachable.")

    chroma_ok, chroma_msg = collections_ready()
    logger.info("ChromaDB: %s (%s)", "OK" if chroma_ok else "NOT READY", chroma_msg)

    if not db_ok:
        if settings.is_local_source:
            logger.warning(
                "Local data file unreadable - check LOCAL_DATA_PATH / LOCAL_SHEET_NAME in .env."
            )
        else:
            logger.warning(
                "SQL Server unreachable at startup - chat requests will fail until this is "
                "fixed. To work offline, set DATA_SOURCE=local in .env."
            )
    if not llm_ok:
        if settings.is_gemini:
            logger.warning(
                "Gemini unreachable at startup - check GEMINI_API_KEY and internet access, "
                "or set LLM_PROVIDER=ollama to run fully locally."
            )
        else:
            logger.warning(
                "Ollama unreachable at startup - run `ollama serve` and pull required models."
            )
    if not chroma_ok:
        logger.warning(
            "Run `python scripts/setup_chromadb.py` once the data source and the LLM "
            "provider are reachable."
        )

    # Warm-up only means anything for a local runtime that has weights to load; the
    # Gemini provider returns an empty dict, so this is skipped there.
    if llm_ok and not settings.is_gemini and settings.OLLAMA_WARMUP_ON_STARTUP:
        # Loading weights now means the first user question does not pay a multi-minute
        # cold start, which is what previously surfaced as a mid-pipeline timeout.
        logger.info("Warming up models (first load can take a minute)...")
        for model, outcome in warm_up().items():
            logger.info("  %s: %s", model, outcome)

    logger.info("Backend ready on http://%s:%s", settings.BACKEND_HOST, settings.BACKEND_PORT)
    logger.info("=" * 78)
```

### 2.12 `scripts/init_data_files.py` — --merge-pending / --split-legacy, users.csv template

**1. REPLACE** file ke top par import lines ka block (doosra group). Naya version:

```python
from filelock import Timeout  # noqa: E402

from backend.services.chat_log_store import LegacySplitError  # noqa: E402
from backend.services.logging_service import (  # noqa: E402
    FIELDNAMES,
    ensure_log_file,
    get_chat_log_store,
)
from config.settings import get_settings  # noqa: E402
```

**2. ADD AFTER** assignment `settings = ...` — ye naya block:

```python
PLACEHOLDER_USERS = (
    "user_id,user_name,user_email,role\n"
    "U001,Demo User,demo.user@example.com,user\n"
)
```

**3. REPLACE** poora `def merge_pending(...)` function. Naya version:

```python
def merge_pending() -> int:
    """Fold every *.pending.csv back into the chat-log file it was meant for.

    Rows land there when a log file could not be written - almost always because it was
    open in Excel. Merging is a separate, explicit step so it never races a live writer.
    """
    status = 0
    try:
        outcomes = get_chat_log_store().merge_pending()
    except Timeout:
        print(
            "FAILED: the chat-log lock is held by another process (the backend is writing). "
            "Retry in a few seconds."
        )
        return 1
    for outcome in outcomes:
        if outcome.error:
            status = 1
            print(
                f"FAILED: {outcome.target.name} - {outcome.error} (close it in Excel and "
                f"retry). Rows remain in {outcome.pending.name}."
            )
        else:
            print(
                f"Merged {outcome.rows} pending row(s) into {outcome.target.name} and "
                f"removed {outcome.pending.name}."
            )
    legacy_status = _merge_legacy_pending()
    if not outcomes and legacy_status is None:
        print("No pending chat-log rows to merge.")
    return status or (legacy_status or 0)
```

**4. ADD AFTER** poora `def merge_pending(...)` function — ye naya block:

```python
def _merge_legacy_pending() -> int | None:
    """data/chat_logs.pending.csv from before the log was partitioned. None = none there."""
    csv_path = settings.resolved(settings.CHAT_LOGS_CSV)
    pending = csv_path.with_name(csv_path.stem + ".pending.csv")
    if not pending.exists() or pending.stat().st_size == 0:
        return None

    with open(pending, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        pending.unlink()
        print(f"{pending.name} was empty; removed it.")
        return 0

    try:
        is_new = not csv_path.exists() or csv_path.stat().st_size == 0
        with open(csv_path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            if is_new:
                writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in FIELDNAMES})
    except PermissionError:
        print(
            f"FAILED: {csv_path.name} is still locked (close it in Excel and retry). "
            f"{len(rows)} row(s) remain in {pending.name}."
        )
        return 1

    pending.unlink()
    print(f"Merged {len(rows)} pending row(s) into {csv_path.name} and removed {pending.name}.")
    return 0
```

**5. ADD AFTER** poora `def _merge_legacy_pending(...)` function — ye naya block:

```python
def split_legacy() -> int:
    """Move the old single data/chat_logs.csv into the weekly/monthly partitions."""
    legacy = settings.resolved(settings.CHAT_LOGS_CSV)
    store = get_chat_log_store()
    try:
        report = store.split_legacy(legacy)
    except LegacySplitError as exc:
        print(f"FAILED: {exc}")
        return 1

    print(
        f"Split {report.routed} row(s) from {legacy.name} into {store.root} "
        f"(filed by each session's login time, this machine's time zone)."
    )
    if report.skipped_expired:
        print(
            f"Skipped {report.skipped_expired} row(s) older than {report.oldest_kept} - "
            f"outside CHAT_LOG_KEEP_MONTHS={store.keep_months}."
        )
    if report.skipped_unparseable:
        print(f"Skipped {report.skipped_unparseable} row(s) with an unreadable login_time.")
    if report.skipped_expired or report.skipped_unparseable:
        print(f"Skipped rows are still in {report.migrated_to.name}.")
    for name in report.spilled_to_pending:
        print(
            f"NOTE: a target file was locked; its rows went to {name}. Close Excel and run "
            f"--merge-pending."
        )
    print(f"Renamed {legacy.name} to {report.migrated_to.name} so it cannot be imported twice.")
    return 0
```

**6. ADD AFTER** poora `def split_legacy(...)` function — ye naya block:

```python
def _ensure_users_file(users_path: Path) -> None:
    if not users_path.exists():
        users_path.parent.mkdir(parents=True, exist_ok=True)
        users_path.write_text(PLACEHOLDER_USERS, encoding="utf-8")
        print(f"Created {users_path} with a placeholder user - edit it to add real authorized users.")
    else:
        print(f"OK: {users_path} already exists.")
```

**7. REPLACE** poora `def main(...)` function. Naya version:

```python
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merge-pending", action="store_true",
        help="append every *.pending.csv under the chat-log folder (and the old "
             "data/chat_logs.pending.csv) into its main file, then delete it",
    )
    parser.add_argument(
        "--split-legacy", action="store_true",
        help="move the old single data/chat_logs.csv into the weekly/monthly files, then "
             "rename it to chat_logs.migrated.csv",
    )
    args = parser.parse_args()

    if args.merge_pending:
        return merge_pending()
    if args.split_legacy:
        return split_legacy()

    _ensure_users_file(settings.resolved(settings.USERS_CSV))

    ensure_log_file()
    print(f"OK: {settings.resolved(settings.CHAT_LOGS_DIR)} ready "
          f"(keeping {settings.CHAT_LOG_KEEP_MONTHS} month(s)).")
    legacy = settings.resolved(settings.CHAT_LOGS_CSV)
    if legacy.exists():
        print(f"NOTE: {legacy.name} is the old single-file log and is no longer written. "
              f"Import it with: python scripts\\init_data_files.py --split-legacy")

    settings.resolved(settings.LOG_DIR).mkdir(parents=True, exist_ok=True)
    settings.resolved(settings.CHROMA_PERSIST_DIR).mkdir(parents=True, exist_ok=True)
    settings.resolved("./data/cache").mkdir(parents=True, exist_ok=True)
    print("OK: logs/, chroma_store/ and data/cache/ directories ready.")
    return 0
```

**8. REPLACE** file ke top wala docstring *(optional — sirf comment/docstring)*. Naya version:

```python
"""Idempotent setup: creates data/users.csv, this month's chat-log folder under
data/chat_logs/, logs/, and the ChromaDB persist directory if they don't already exist.
Safe to re-run at any time.

Usage:
    python scripts/init_data_files.py
    python scripts/init_data_files.py --merge-pending
    python scripts/init_data_files.py --split-legacy
"""
```

### 2.13 `frontend/theme.py` — Sidebar CSS, header mein mascot, scroll script (streamlit_app.py se PEHLE)

**1. REPLACE** file ke top par import lines ka block. Naya version:

```python
import base64
import html
from functools import lru_cache
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
```

**2. ADD AFTER** assignment `INPUT_PLACEHOLDER = ...` — ye naya block:

```python
EXAMPLE_QUESTIONS = [
    "High-performing branch region-wise.",
    "Vertical-wise business trend.",
    "Product-wise and branch-wise trend.",
    "Zone-wise and vertical-wise business contribution.",
    "Business-type-wise vertical performance.",
    "High-performing intermediaries.",
    "Show the top five branches",
    "Compare zone contribution and identify high-performing intermediaries.",
    "Show business contribution by zone, vertical, and product.",
    "Which branch has the highest business?",
    "Show actual versus target if the relevant columns exist.",
    "Generate separate charts for branch performance and vertical contribution.",
]
```

**3. REPLACE** assignment `_CSS = ...`. Naya version:

```python
_CSS = """
<style>
:root {
    --uni-bg-top: #0a0f1f;
    --uni-bg-bottom: #02040a;
    --uni-text: #e0f7ff;
    --uni-cyan: #00ffff;
    --uni-line: rgba(0, 255, 255, 0.2);
    --uni-glow: rgba(0, 255, 255, 0.15);
    --uni-glass: rgba(255, 255, 255, 0.05);
    --uni-bubble: rgba(255, 255, 255, 0.08);
    /* The design's cyan -> purple, with a lighter violet end: black text on pure
       `purple` is 2.2:1, on this violet it stays above 5:1. */
    --uni-accent: linear-gradient(90deg, #00ffff, #a855f7);
}

.stApp {
    background: radial-gradient(circle at top, var(--uni-bg-top), var(--uni-bg-bottom)) !important;
    color: var(--uni-text);
    font-family: 'Segoe UI', sans-serif;
    /* Lets the grid below sit at z-index -1 without dropping behind the page itself. */
    isolation: isolate;
}
.stApp::before {
    content: "";
    position: fixed;
    top: 0;
    left: 0;
    width: calc(100% + 120px);
    height: calc(100% + 120px);
    background-image: linear-gradient(rgba(0, 255, 255, 0.05) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(0, 255, 255, 0.05) 1px, transparent 1px);
    background-size: 60px 60px;
    /* Two whole tiles per loop, so the restart is seamless. */
    animation: uni-grid 24s linear infinite;
    pointer-events: none;
    z-index: -1;
}
@keyframes uni-grid {
    from { transform: translate(0, 0); }
    to { transform: translate(-120px, -120px); }
}

[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stHeader"] {
    background: transparent !important;
}
[data-testid="stDecoration"] { display: none; }
[data-testid="stMainBlockContainer"] {
    padding-top: 2.2rem;
    padding-bottom: 1rem;
    /* Streamlit's 5rem sides cost the chat a quarter of a laptop screen next to the sidebar. */
    padding-left: clamp(1rem, 3vw, 3rem);
    padding-right: clamp(1rem, 3vw, 3rem);
    max-width: 1400px;
}
.stApp p, .stApp li, .stApp label, .stApp button, .stApp input, .stApp textarea,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 {
    font-family: 'Segoe UI', sans-serif;
}

/* ---- header / footer ----
   Three columns: an empty one, the title, then the account and the mascot. The two outer
   columns share the leftover space equally, so the title stays centred; when the right one
   needs more than its share the title moves left instead of being overlapped. */
.uni-header {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    column-gap: 16px;
    text-align: center;
    padding: 10px 16px 10px 20px;
    background: rgba(0, 0, 0, 0.4);
    backdrop-filter: blur(10px);
    border: 1px solid var(--uni-line);
    border-radius: 16px;
}
.uni-header-right {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 12px;
}
/* The header's own height (title + subtitle), so it never makes the band taller. */
.uni-header-mascot {
    height: 70px;
    width: auto;
    border-radius: 10px;
    filter: drop-shadow(0 0 8px var(--uni-cyan));
}
.uni-header-title {
    font-size: 2rem;
    font-weight: 700;
    line-height: 1.2;
    letter-spacing: 0.04em;
    color: var(--uni-text);
    text-shadow: 0 0 10px var(--uni-cyan);
}
.uni-header-sub {
    margin-top: 4px;
    font-size: 0.9rem;
    opacity: 0.7;
}
.uni-footer {
    text-align: center;
    padding: 6px 0 10px;
    font-size: 0.85rem;
    opacity: 0.6;
}

/* Account chip, left of the mascot. Plain HTML inside the header now that there is no
   Log out button; a fixed width keeps a long name from pushing the title around. */
.st-key-uni-top { margin-bottom: 8px; }
.uni-account {
    width: 150px;
    min-width: 0;
}
.uni-account-name,
.uni-account-role {
    text-align: right;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.uni-account-name {
    font-size: 0.9rem;
    opacity: 0.85;
}
.uni-account-role {
    font-size: 0.68rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    opacity: 0.55;
}
/* A phone has no room beside the title: the account drops under it, the mascot goes. */
@media (max-width: 640px) {
    .uni-header { grid-template-columns: 1fr; row-gap: 6px; }
    .uni-header > .uni-header-side:first-child { display: none; }
    .uni-header-right { justify-content: center; }
    .uni-header-mascot { display: none; }
    .uni-account-name, .uni-account-role { text-align: center; }
}

/* ---- chat panel ----
   The glass styling goes on the border wrapper Streamlit draws for border=True, not on the
   keyed block itself: Streamlit sizes children from that block's measured width, so
   padding added to the block would push charts past its edge. */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chat),
[data-testid="stForm"] {
    background: var(--uni-glass);
    border: 1px solid var(--uni-line);
    border-radius: 20px;
    box-shadow: 0 0 25px var(--uni-glow);
}
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chatbox) {
    /* Whatever the viewport leaves after the header and the input row, so the SEND bar
       stays on screen on a 768px laptop and the window grows on anything taller. */
    height: max(360px, calc(100vh - 268px)) !important;
    scrollbar-width: thin;
    scrollbar-color: rgba(0, 255, 255, 0.35) transparent;
}
/* Narrower screens spend more height above the box: next to an open sidebar the title can
   wrap at 1000px, and on a phone the account also drops under it. */
@media (max-width: 1000px) {
    [data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chatbox) {
        height: max(360px, calc(100vh - 324px)) !important;
    }
}
@media (max-width: 640px) {
    [data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chatbox) {
        height: max(320px, calc(100vh - 385px)) !important;
    }
}

/* The conversation reads down a centred lane instead of stretching across a wide screen.
   Set on the border wrapper, which is what Streamlit measures to size charts, and with an
   explicit width: auto margins alone would make it shrink-to-fit, and a shrink-to-fit box
   feeds its own width back into itself. */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-lane) {
    width: 100%;
    max-width: 1080px;
    margin: 0 auto;
}

.st-key-uni-chatbox [data-testid="stChatMessage"] {
    background: var(--uni-bubble);
    border: 1px solid var(--uni-line);
    border-radius: 12px;
    padding: 12px 15px;
}
.st-key-uni-chatbox [data-testid="stChatMessage"] img {
    border-radius: 50%;
    box-shadow: 0 0 8px var(--uni-glow);
}
.uni-user {
    width: fit-content;
    max-width: 75%;
    margin: 4px 0 4px auto;
    padding: 12px 15px;
    border-radius: 12px;
    background: var(--uni-accent);
    color: #000;
    font-weight: 500;
    overflow-wrap: anywhere;
}

/* Plotly paints its own opaque background; let the bubble show through instead. */
.st-key-uni-chatbox .js-plotly-plot .main-svg { background: transparent !important; }

[data-testid="stExpander"] details {
    background: rgba(0, 0, 0, 0.25);
    border: 1px solid var(--uni-line);
    border-radius: 10px;
}

/* ---- input area ---- */
.st-key-uni-chat [data-testid="stChatInput"] {
    padding-top: 12px;
    border-top: 1px solid var(--uni-line);
    border-radius: 0 !important;
    background: transparent !important;
}
.st-key-uni-chat [data-testid="stChatInput"] > div {
    /* Lines up with the conversation lane above it. */
    max-width: 1080px;
    margin: 0 auto;
    background: rgba(0, 0, 0, 0.25) !important;
    border: 1px solid var(--uni-line) !important;
    border-radius: 12px !important;
}
[data-testid="stChatInputTextArea"] {
    color: var(--uni-text) !important;
    /* Room for the SEND label, which is wider than the icon it replaces. */
    padding-right: 96px !important;
}
[data-testid="stChatInputSubmitButton"] {
    align-self: center;
    width: auto !important;
    min-width: 76px;
    height: 32px !important;
    margin-right: 4px;
    padding: 0 18px !important;
    border-radius: 10px !important;
    background: var(--uni-accent) !important;
    color: #000 !important;
}
[data-testid="stChatInputSubmitButton"] svg { display: none; }
[data-testid="stChatInputSubmitButton"]::after {
    content: "SEND";
    font-weight: 700;
    letter-spacing: 0.05em;
}
[data-testid="stChatInputSubmitButton"]:disabled { opacity: 0.45; }

/* ---- buttons ---- */
[data-testid="stBaseButton-primary"],
[data-testid="stBaseButton-primaryFormSubmit"] {
    background: var(--uni-accent) !important;
    border: none !important;
    color: #000 !important;
    font-weight: 700;
}
[data-testid="stBaseButton-primary"]:hover,
[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    box-shadow: 0 0 16px var(--uni-glow);
    filter: brightness(1.08);
}

/* ---- mascot ---- */
.uni-mascot-wrap {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 320px;
}
.uni-mascot {
    width: min(220px, 85%);
    border-radius: 14px;
    filter: drop-shadow(0 0 20px var(--uni-cyan));
}

/* ---- sidebar: example questions ---- */
[data-testid="stSidebar"] {
    /* Near-opaque: on a narrow screen the sidebar opens over the chat, not beside it. */
    background: rgba(2, 6, 18, 0.9) !important;
    backdrop-filter: blur(10px);
    border-right: 1px solid var(--uni-line);
}
.st-key-uni-examples { gap: 0.5rem; }
.uni-side-title {
    font-size: 1.05rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-shadow: 0 0 10px var(--uni-glow);
}
.uni-side-hint {
    /* Streamlit pulls a markdown block up 1rem to cancel a paragraph margin these divs
       do not have, so the gap above the first button is given back here. */
    margin: 4px 0 calc(1rem + 2px);
    font-size: 0.85rem;
    opacity: 0.65;
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"] {
    justify-content: flex-start;
    min-height: 0;
    padding: 8px 12px;
    background: var(--uni-glass);
    border: 1px solid var(--uni-line);
    border-radius: 10px;
    color: var(--uni-text);
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"] p {
    white-space: normal;
    text-align: left;
    font-size: 0.88rem;
    line-height: 1.35;
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"]:hover:enabled {
    border-color: var(--uni-cyan);
    color: var(--uni-cyan);
    box-shadow: 0 0 12px var(--uni-glow);
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"]:disabled { opacity: 0.45; }

/* Holds the one-shot scroll script: out of flow so it adds no gap, and clipped rather than
   display:none, which can stop a browser loading the iframe at all. */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-scroll) {
    position: absolute;
    width: 0;
    height: 0;
    overflow: hidden;
    pointer-events: none;
}

@media (prefers-reduced-motion: reduce) {
    .stApp::before { animation: none; }
}
</style>
"""
```

**4. REPLACE** poora `def render_header(...)` function. Naya version:

```python
def render_header(user_name: str | None = None, role: str | None = None) -> None:
    """The title band. Signed in, its right end carries the account and the mascot; the
    login screen shows neither, since it has the large mascot beside the form."""
    right = ""
    if user_name is not None:
        name = html.escape(user_name)
        src = _mascot_data_uri()
        mascot = f'<img class="uni-header-mascot" src="{src}" alt="UNISONIC mascot">' if src else ""
        right = (
            f'<div class="uni-account"><div class="uni-account-name" title="{name}">👤 {name}</div>'
            f'<div class="uni-account-role">{html.escape(role or "")}</div></div>{mascot}'
        )
    st.markdown(
        '<div class="uni-header"><div class="uni-header-side"></div>'
        f'<div class="uni-header-main"><div class="uni-header-title">{HEADER_TITLE}</div>'
        f'<div class="uni-header-sub">{HEADER_SUBTITLE}</div></div>'
        f'<div class="uni-header-side uni-header-right">{right}</div></div>',
        unsafe_allow_html=True,
    )
```

**5. ADD AFTER** poora `def render_mascot(...)` function — ye naya block:

```python
# Runs in a same-origin component iframe and reaches into the page. Every step is inside
# try/catch: a Streamlit DOM change must cost only the scroll, never the chat.
_SCROLL_SCRIPT = """<script>
/* run __NONCE__ */
(function () {
  try {
    var doc = window.parent.document;
    var view = doc.defaultView;
    var stopAt = Date.now() + 1600;
    var userMoved = false;
    var scroller = null;
    var events = ["wheel", "touchstart", "pointerdown"];
    function onUser(event) { if (event.isTrusted) { userMoved = true; } }
    function findScroller() {
      var el = doc.querySelector(".st-key-uni-chatbox");
      for (; el && el !== doc.body; el = el.parentElement) {
        var overflow = view.getComputedStyle(el).overflowY;
        if (overflow === "auto" || overflow === "scroll") { return el; }
      }
      return null;
    }
    function finish() {
      try {
        if (scroller) {
          events.forEach(function (name) { scroller.removeEventListener(name, onUser); });
        }
      } catch (e) {}
    }
    function step() {
      try {
        if (userMoved) { finish(); return; }
        if (!scroller) {
          scroller = findScroller();
          if (scroller) {
            events.forEach(function (name) {
              scroller.addEventListener(name, onUser, { passive: true });
            });
          }
        }
        var bubbles = scroller ? scroller.querySelectorAll(".uni-user") : [];
        var latest = bubbles[bubbles.length - 1];
        if (latest) {
          var top = latest.getBoundingClientRect().top - scroller.getBoundingClientRect().top
                    + scroller.scrollTop - 10;
          var target = Math.max(0, Math.min(top, scroller.scrollHeight - scroller.clientHeight));
          if (Math.abs(scroller.scrollTop - target) > 2) { scroller.scrollTop = target; }
        }
      } catch (e) {}
      if (Date.now() < stopAt) { setTimeout(step, 120); } else { finish(); }
    }
    step();
  } catch (e) {}
})();
</script>"""
```

**6. ADD AFTER** assignment `_SCROLL_SCRIPT = ...` — ye naya block:

```python
def render_scroll_to_latest(nonce: int) -> None:
    """Scroll the chat window so the newest question sits at its top.

    Otherwise a long answer (insight, a 420px chart, an expander) leaves the reader looking
    at its end, with the question and the insight scrolled out of view above. It keeps
    re-applying for ~1.5 s because plotly lays charts out after the page renders, and
    gives up at once if the reader scrolls. The nonce makes each call a new document, so the
    browser runs it again; the caller draws it only on a run that added a message.
    """
    components.html(_SCROLL_SCRIPT.replace("__NONCE__", str(int(nonce))), height=0)
```

**7. REPLACE** file ke top wala docstring *(optional — sirf comment/docstring)*. Naya version:

```python
"""UNISONIC skin for the Streamlit chat: page CSS, header, mascot, user bubble, footer, and
the script that scrolls the chat window to the newest question.

Kept out of streamlit_app.py so the look can change without touching the request and
render logic. The selectors target Streamlit 1.41's DOM - `data-testid` attributes and the
`st-key-<key>` class a keyed container gets - so re-check them after upgrading streamlit.
The matching dark base theme lives in .streamlit/config.toml; without it Streamlit's own
widgets (expanders, code blocks, charts) stay light and clash with this background.
"""
```

### 2.14 `frontend/streamlit_app.py` — Sidebar, lane, role gate, Log out hataya, mascot header mein

**1. REPLACE** file ke top par import lines ka block (doosra group). Naya version:

```python
import httpx
import streamlit as st

from backend.core.figure_codec import figure_from_json
from config.settings import get_settings
from frontend.theme import (
    BOT_AVATAR,
    CHAT_BOX_HEIGHT,
    EXAMPLE_QUESTIONS,
    INPUT_PLACEHOLDER,
    WELCOME_MESSAGE,
    apply_theme,
    render_footer,
    render_header,
    render_mascot,
    render_scroll_to_latest,
    render_user_bubble,
)
```

**2. REPLACE** ye purani ek line:

```python
st.set_page_config(page_title="UNISONIC · USGI Business Insight", page_icon=BOT_AVATAR, layout="wide")
```

is naye block se (sidebar shuru se khula rahe, aur do nayi constants):

```python
st.set_page_config(
    page_title="UNISONIC · USGI Business Insight",
    page_icon=BOT_AVATAR,
    layout="wide",
    initial_sidebar_state="expanded",
)

DEVELOPER_ROLE = "developer"
CHAT_INPUT_KEY = "uni-question"
```

**3. REPLACE** poora `def _init_state(...)` function. Naya version:

```python
def _init_state():
    defaults = {
        "logged_in": False, "session_id": None, "user_id": None, "user_name": None,
        "role": "user", "messages": [], "pending_question": None,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)
```

**4. ADD AFTER** poora `def _init_state(...)` function — ye naya block:

```python
def _role_from(login_response: dict) -> str:
    # Least privilege: anything but an explicit "developer" gets the plain user view.
    role = str(login_response.get("role") or "").strip().lower()
    return DEVELOPER_ROLE if role == DEVELOPER_ROLE else "user"
```

**5. ADD AFTER** poora `def _role_from(...)` function — ye naya block:

```python
def _is_developer() -> bool:
    return st.session_state.get("role") == DEVELOPER_ROLE
```

**6. REPLACE** poora `def _login_form(...)` function. Naya version:

```python
def _login_form():
    with st.form("login_form"):
        st.markdown("### 🔐 Sign in")
        st.caption("Ask a business question in plain English - get an NLP insight and a chart, grounded in dbo.May_2.")
        email = st.text_input("Work email", placeholder="you@company.com")
        submitted = st.form_submit_button("Log in", type="primary", use_container_width=True)
    if submitted:
        if not email.strip():
            st.error("Please enter your email.")
            return
        ok, data = _api("/auth/login", {"email": email.strip()}, timeout=15)
        if ok:
            st.session_state.logged_in = True
            st.session_state.session_id = data["session_id"]
            st.session_state.user_id = data["user_id"]
            st.session_state.user_name = data["user_name"]
            st.session_state.role = _role_from(data)
            st.rerun()
        else:
            st.error(data)
```

**7. REPLACE** poora `def _render_message(...)` function. Naya version:

```python
def _render_message(msg: dict):
    role = msg["role"]
    if role == "user":
        render_user_bubble(msg["content"])
        return
    with st.chat_message(role, avatar=BOT_AVATAR):
        status = msg.get("status", "ok")
        if status in ("error", "timeout"):
            st.error(msg.get("error_message") or "Something went wrong. Please refresh the page.")
            return

        badges = []
        if msg.get("cache_hit"):
            badges.append("⚡ Served from cache")
        # Make it visible when a figure came straight from SQL versus being narrated.
        mode_label = {
            "direct": "🔒 Answer taken verbatim from the query result",
            "template": "🔒 Figures taken directly from the query result",
            "narrative": "✅ Narrative verified against the query result",
        }.get(msg.get("answer_mode", ""))
        if mode_label:
            badges.append(mode_label)
        if msg.get("data_source"):
            badges.append(f"source: {msg['data_source']}")
        if badges:
            st.caption("  •  ".join(badges))

        sections = msg.get("sections") or []
        if sections:
            # A compound question is answered one part at a time: each part has its own
            # SQL, insight and chart, so they are rendered as separate blocks rather than
            # one merged answer.
            for position, section in enumerate(sections):
                _render_section(msg, section, position, numbered=len(sections) > 1)
        else:
            # A client-side fallback for a cached response saved before sections existed.
            st.markdown(msg.get("insight") or "_No insight generated._")
            _render_charts(msg.get("charts", []), key_prefix=f"{msg['id']}-flat")

        # SQL and pipeline warnings are for developers. The backend already strips them for
        # a user; this keeps the UI from drawing an empty panel in their place.
        if _is_developer() and msg.get("warnings"):
            with st.expander("⚠️ Warnings"):
                for w in msg["warnings"]:
                    st.caption(f"- {w}")
```

**8. REPLACE** poora `def _render_section(...)` function. Naya version:

```python
def _render_section(msg: dict, section: dict, position: int, numbered: bool) -> None:
    """One answered part of the question: heading, insight, chart, then (developer) its SQL."""
    question = section.get("question") or ""
    if numbered:
        st.markdown(f"##### {position + 1}. {question}")

    if section.get("error"):
        st.warning(f"Could not answer this part: {section['error']}")
        return

    st.markdown(section.get("insight") or "_No insight generated._")
    _render_charts(section.get("charts", []), key_prefix=f"{msg['id']}-{position}")

    if not _is_developer():
        return
    label = f"🔍 View SQL & data — {question[:60]}" if numbered else "🔍 View SQL & data"
    with st.expander(label):
        st.code(section.get("sql") or "(no SQL generated)", language="sql")
        st.caption(f"Rows returned: {section.get('row_count', 0)}")
        if section.get("data_preview"):
            _render_table(section["data_preview"])
```

**9. ADD AFTER** poora `def _render_section(...)` function — ye naya block:

```python
def _queue_question(question: str) -> None:
    st.session_state.pending_question = question
```

**10. ADD AFTER** poora `def _queue_question(...)` function — ye naya block:

```python
def _queue_typed_question() -> None:
    typed = (st.session_state.get(CHAT_INPUT_KEY) or "").strip()
    if typed:
        _queue_question(typed)
```

**11. REPLACE** poora `def _ask(...)` function. Naya version:

```python
def _ask(question: str, scroll_slot):
    st.session_state.messages.append({"role": "user", "content": question})
    # Drawn in place while the pipeline runs (30-150s on a CPU-only host), so the question
    # and a working bubble are on screen at once; the rerun replaces it with the answer.
    render_user_bubble(question)
    with scroll_slot:
        render_scroll_to_latest(nonce=len(st.session_state.messages))
    with st.chat_message("assistant", avatar=BOT_AVATAR):
        with st.spinner(f"🤖 Analyzing: {question}"):
            ok, data = _api("/chat", {"session_id": st.session_state.session_id, "question": question})
            # Stored before the spinner closes: closing it is the first Streamlit call after
            # the request, and that is where a Stop pressed meanwhile ends this run.
            msg_id = len(st.session_state.messages)
            if ok:
                st.session_state.messages.append({"role": "assistant", "id": msg_id, **data})
            else:
                st.session_state.messages.append({
                    "role": "assistant", "id": msg_id, "status": "error", "error_message": data,
                })
            st.session_state.scroll_to_latest = True
```

**12. ADD AFTER** poora `def _ask(...)` function — ye naya block:

```python
def _example_sidebar(busy: bool) -> None:
    with st.sidebar, st.container(key="uni-examples"):
        st.markdown(
            '<div class="uni-side-title">💡 Example questions</div>'
            '<div class="uni-side-hint">Click one to ask it.</div>',
            unsafe_allow_html=True,
        )
        for index, question in enumerate(EXAMPLE_QUESTIONS):
            st.button(
                question, key=f"uni-example-{index}", on_click=_queue_question, args=(question,),
                disabled=busy, use_container_width=True,
            )
```

**13. REPLACE** poora `def _chat_screen(...)` function. Naya version:

```python
def _chat_screen(busy: bool):
    # The mascot lives in the header band, so the chat panel takes the full width.
    with st.container(border=True, key="uni-chat"):
        # Fixed height makes this the scrolling chat window.
        chat_box = st.container(height=CHAT_BOX_HEIGHT, border=False, key="uni-chatbox")
        st.chat_input(
            INPUT_PLACEHOLDER, key=CHAT_INPUT_KEY, on_submit=_queue_typed_question,
            disabled=busy,
        )
    scroll_slot = st.container(key="uni-scroll")

    # The lane centres the conversation. Being one level down it also means the window
    # holds no chat message directly, which is what makes Streamlit pin it to the bottom -
    # and so drag a long answer's question and insight out of view.
    # render_scroll_to_latest positions it instead, on the runs that add a message.
    with chat_box, st.container(key="uni-lane"):
        with st.chat_message("assistant", avatar=BOT_AVATAR):
            st.markdown(WELCOME_MESSAGE)
        for msg in st.session_state.messages:
            _render_message(msg)
        question = st.session_state.pop("pending_question", None)
        if question:
            _ask(question, scroll_slot)
            st.rerun()

    if st.session_state.pop("scroll_to_latest", False):
        with scroll_slot:
            render_scroll_to_latest(nonce=len(st.session_state.messages))
```

**14. REPLACE** poora `def main(...)` function. Naya version:

```python
def main():
    _init_state()
    apply_theme()
    with st.container(key="uni-top"):
        if st.session_state.logged_in:
            # Name, role and mascot at the header's right end. No Log out, by the user's
            # decision (2026-09): a session ends when it idles out.
            role = DEVELOPER_ROLE if _is_developer() else "user"
            render_header(st.session_state.user_name or "", role)
        else:
            render_header()
    if not st.session_state.logged_in:
        _login_screen()
    else:
        # A question is queued by a click or a submit and answered further down this run,
        # which blocks for the whole request. Everything that could queue another is drawn
        # disabled first: a click mid-request would interrupt the run and leave that
        # question with no answer. The rerun after the answer enables them again.
        busy = bool(st.session_state.pending_question)
        _example_sidebar(busy)
        _chat_screen(busy)
    render_footer()
```

**15. DELETE** poora `def _logout(...)` function. Pehchaanne ke liye purana code:

```python
def _logout():
    if st.session_state.session_id:
        _api("/auth/logout", {"session_id": st.session_state.session_id}, timeout=10)
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()
```

**16. DELETE** poora `def _account_bar(...)` function. Pehchaanne ke liye purana code:

```python
def _account_bar():
    """Signed-in user and log out, pinned inside the header band."""
    with st.container(key="uni-account"):
        name_col, logout_col = st.columns([1.3, 1], vertical_alignment="center")
        name = html.escape(st.session_state.user_name or "")
        name_col.markdown(f'<div class="uni-account-name">👤 {name}</div>', unsafe_allow_html=True)
        if logout_col.button("Log out", type="primary"):
            _logout()
```

## Step 3 — `data/users.csv` (haath se, office ka file replace MAT karna)

Office ke users.csv mein asli users hain. Sirf ek column jodo:

1. Header line ke end mein `,role` lagao → `user_id,user_name,user_email,role`
2. Har row ke end mein `,developer` ya `,user` lagao. Jise SQL/Warnings dekhne hain → `developer`.
3. Column na ho ya khaali ho → wo user `user` maana jayega (SQL nahi dikhegi).

Role har sawaal par dobara padha jata hai — role badalne ke liye backend restart ki zaroorat nahi.

## Step 4 — Ye files poori copy kar sakte ho (runtime par asar nahi)

- `tests/test_frontend_render.py`
- `tests/test_sql_guard.py`
- `.env.example`
- `.gitignore`
- `setup.bat`
- `CLAUDE.md`
- `README.md`

`.env` mein kuch badalna zaroori nahi — nayi settings ke defaults hain
(`CHAT_LOGS_DIR=./data/chat_logs`, `CHAT_LOG_KEEP_MONTHS=2`).

## Step 5 — Check karo

```bat
.venv\Scripts\python -m pytest
```
Sirf `test_pyarrow_is_not_installed` fail hona chahiye (purana, known). Koi aur fail ho to
koi block chhoot gaya hai — error message bhejo.

Phir `run_backend.bat` + `run_frontend.bat`. Backend log mein dekho:

- `The data source login can WRITE to dbo.May_2 (...)` WARNING aaye to Step 6 zaroori hai.
- `/health/detailed` → `data_source.write_permissions` `[]` hona chahiye.

## Step 6 — SQL Server ko read-only banana (DBA ke saath)

1. `scripts/sqlserver_readonly.sql` SSMS mein kholo, upar `@login_name` = `.env` ka `DB_USERNAME`.
2. Pehle `@apply_changes = 0` se chalao (sirf report). Messages tab padho — WARNING ho to pehle wo theek karo.
3. Phir `@apply_changes = 1` — poori script F5 (kuch highlight mat karna).
4. Office laptop par: `.venv\Scripts\python scripts\check_readonly_access.py` → sab PASS.

Ye script abhi tak kisi real SQL Server par nahi chali hai — jo bhi error aaye, exact text bhejo.

## Step 7 — Purane logs ko weekly files mein (ek baar)

```bat
.venv\Scripts\python scripts\init_data_files.py --split-legacy
```
`data/chat_logs.csv` ki rows `data/chat_logs/YYYY-MM/` ki week + month files mein chali jaati
hain; 2 mahine se purani rows skip hoti hain; purani file `chat_logs.migrated.csv` ban jaati hai.
