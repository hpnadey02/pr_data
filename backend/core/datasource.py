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

logger = get_logger(__name__)
settings = get_settings()


class DatabaseError(Exception):
    """Raised with a user-safe message; the original exception is chained via `from`."""


def refuse_writes(sql: str) -> None:
    """Second check behind sql_guard: run_select also serves callers that never pass
    through the guard (date_windows, the scripts' --sql option)."""
    offending = find_write_operation(sql)
    if offending:
        raise DatabaseError(
            "Refused to run a statement that could change data: "
            f"{describe_write_operation(offending)}. Only read-only SELECT queries are allowed."
        )


@dataclass
class QueryResult:
    dataframe: pd.DataFrame
    row_count: int
    truncated: bool
    columns: list[str]


def split_table(qualified: str) -> tuple[str, str]:
    """'dbo.May_2' -> ('dbo', 'May_2'); 'May_2' -> ('dbo', 'May_2')."""
    parts = [p.strip().strip("[]") for p in str(qualified or "").split(".") if p.strip()]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "dbo", (parts[-1] if parts else "")


# ======================================================================================
# Result normalisation - the contract every backend must satisfy
# ======================================================================================
#
# The chart and insight agents decide what to plot and what to summarise purely from
# pandas DTYPES (see backend/agents/df_utils.py). That makes dtype fidelity part of the
# DataSource contract, not an implementation detail - and it is where the two backends
# used to diverge badly:
#
#   * pyodbc returns decimal.Decimal for SQL Server DECIMAL / NUMERIC / MONEY columns,
#     and datetime.date / datetime.datetime for date columns. pandas cannot map those to
#     a numpy dtype, so the column lands as OBJECT. is_numeric_dtype() is then False,
#     numeric_columns() returns [], pick_measure() returns None - and the pipeline
#     silently produced NO chart and an insight with no figures in it, because
#     compute_stats() had nothing to compute. DuckDB hands back real numpy dtypes, which
#     is exactly why local mode looked fine while SQL Server did not.
#
#   * SQL Server names an un-aliased aggregate '' (empty string). DuckDB names it
#     'sum(x)'. An empty column name breaks Plotly and groupby.
#
# Both are repaired here, once, for every backend - so "it works locally" now actually
# predicts "it works on SQL Server".


def repair_column_names(columns: list) -> list[str]:
    """Give every column a non-empty, unique, whitespace-trimmed name."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for position, raw in enumerate(columns, start=1):
        name = "" if raw is None else str(raw).strip()
        if not name:
            # SQL Server does this for `SELECT SUM(x) FROM ...` with no alias.
            name = f"column_{position}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def _looks_like(values, types) -> bool:
    return all(isinstance(v, types) and not isinstance(v, bool) for v in values)


def _as_numeric_text(series: pd.Series) -> pd.Series | None:
    """Parse a text column of plain numbers, or None if any value is not one.

    This is the Excel case: in may_2.xlsx all 47 measure columns are stored as TEXT even
    though SQL Server declares them decimal(38,2). Read with pandas they arrive as
    strings, so the measure would be invisible to the agents exactly as the Decimal
    problem made it invisible on SQL Server.

    Requiring EVERY non-null value to parse is what keeps this safe - one branch name in
    the column and nothing is converted.
    """
    cleaned = series.astype(str).str.strip().str.replace(",", "", regex=False)
    converted = pd.to_numeric(cleaned, errors="coerce")
    if converted.notna().sum() != series.notna().sum():
        return None
    return converted


def _normalize_column(series: pd.Series, name: str) -> pd.Series:
    """Coerce a driver-typed object column to the numpy dtype it should have been.

    Decimal and date objects are always converted - they are unambiguous. Numeric TEXT is
    converted only when the column NAME marks it as a quantity, because an all-digits
    string is just as likely to be an identifier: POLICY_NO holds 1029156133, and turning
    that into a float would corrupt it.
    """
    # pandas 2 stores text as `object`; pandas 3 uses a dedicated `str` dtype. Decimal and
    # date objects are `object` in both. Checking for either keeps this working across
    # the version boundary instead of silently doing nothing on one of them.
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return series

    values = series.dropna()
    if values.empty:
        return series

    if any(isinstance(v, Decimal) for v in values) and _looks_like(values, (Decimal, int, float)):
        return pd.to_numeric(series, errors="coerce")

    if _looks_like(values, (dt.date, dt.datetime)):
        return pd.to_datetime(series, errors="coerce")

    if looks_like_measure(name) and _looks_like(values, str):
        converted = _as_numeric_text(series)
        if converted is not None:
            logger.debug("Parsed text column '%s' as numeric", name)
            return converted

    return series


def normalize_result_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Make a fetched result safe for the chart/insight agents, whatever produced it."""
    if frame is None:
        return frame
    frame.columns = repair_column_names(list(frame.columns))
    if frame.empty:
        return frame
    for column in frame.columns:
        frame[column] = _normalize_column(frame[column], str(column))
    return frame


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


# ======================================================================================
# SQL Server
# ======================================================================================

class SqlServerDataSource(DataSource):
    """pyodbc access to the enterprise SQL Server. Behaviour is unchanged from the
    original backend/core/db.py apart from check_connection() now reporting the real
    SQLSTATE instead of always claiming the ODBC driver is missing."""

    name = "sqlserver"

    def __init__(self) -> None:
        import pyodbc  # local import: SQL Server mode only

        self._pyodbc = pyodbc

    def describe(self) -> str:
        return f"SQL Server {settings.DB_SERVER}/{settings.DB_DATABASE}, table {settings.DB_TABLE}"

    # -- connection -------------------------------------------------------------------

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

    def _connect(self):
        """Translates low-level pyodbc errors into friendly DatabaseError messages. The
        transient retry must wrap the raw connect call, not this translation layer -
        otherwise the except blocks would convert the exception before tenacity sees it."""
        pyodbc = self._pyodbc
        try:
            return self._raw_connect()
        except pyodbc.InterfaceError as exc:
            raise DatabaseError(
                f"{describe_odbc_error(exc)} If the driver is missing, install the Microsoft "
                f"ODBC Driver 17 for SQL Server; DB_DRIVER is currently '{settings.DB_DRIVER}'."
            ) from exc
        except pyodbc.OperationalError as exc:
            message = str(exc)
            if "Login failed" in message:
                raise DatabaseError(
                    f"SQL Server login failed for '{settings.DB_USERNAME}'. Check "
                    f"DB_USERNAME / DB_PASSWORD in .env. {describe_odbc_error(exc)}"
                ) from exc
            raise DatabaseError(
                f"Could not reach SQL Server at {settings.DB_SERVER}. Check network/VPN and "
                f"server status. {describe_odbc_error(exc)}"
            ) from exc

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass

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

    # -- interface --------------------------------------------------------------------

    def check_connection(self) -> tuple[bool, str]:
        try:
            with self._connection() as conn:
                conn.cursor().execute("SELECT 1")
            return True, "ok"
        except DatabaseError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - health check must never raise
            return False, describe_odbc_error(exc)

    def get_table_columns(self) -> list[dict]:
        schema, table = split_table(settings.DB_TABLE)
        sql = (
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
        )
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, schema, table)
            rows = cursor.fetchall()
        if not rows:
            raise DatabaseError(
                f"Table {settings.DB_TABLE} was not found or returned no columns. "
                "Verify DB_TABLE in .env."
            )
        return [{"column_name": r.COLUMN_NAME, "data_type": r.DATA_TYPE} for r in rows]

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

    _WRITE_PERMISSIONS = ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE TABLE")

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


def describe_odbc_error(exc: Exception) -> str:
    """Report the real SQLSTATE and driver message instead of guessing at the cause.

    pyodbc raises with args of the form ('08001', '[08001] [Microsoft]...'). The previous
    implementation labelled every failure "ODBC driver not found", which sent users
    chasing a driver install for what were actually network, login or timeout failures.
    """
    args = getattr(exc, "args", ()) or ()
    sqlstate = str(args[0]) if args and isinstance(args[0], str) and len(str(args[0])) == 5 else None
    detail = str(args[1]) if len(args) > 1 else str(exc)
    detail = " ".join(str(detail).split())
    kind = type(exc).__name__
    if sqlstate:
        return f"{kind} SQLSTATE {sqlstate}: {detail}"
    return f"{kind}: {detail}"


# ======================================================================================
# Local file: CSV / Excel (DuckDB)
# ======================================================================================

_PANDAS_TO_SQL_TYPE: tuple[tuple[str, str], ...] = (
    ("datetime64", "datetime"),
    ("timedelta", "time"),
    ("bool", "bit"),
    ("int", "int"),
    ("uint", "int"),
    ("float", "float"),
    ("decimal", "decimal"),
    ("object", "varchar"),
    ("string", "varchar"),
    ("category", "varchar"),
)


def pandas_dtype_to_sql(dtype) -> str:
    """Map a pandas dtype to the SQL-ish type names INFORMATION_SCHEMA would report, so
    downstream code (schema retrieval, ChromaDB category inference) works unchanged."""
    text = str(dtype).lower()
    for prefix, sql_type in _PANDAS_TO_SQL_TYPE:
        if text.startswith(prefix):
            return sql_type
    return "varchar"


_DUCKDB_TO_SQL_TYPE: tuple[tuple[str, str], ...] = (
    ("TIMESTAMP", "datetime"),
    ("DATE", "date"),
    ("TIME", "time"),
    ("BOOLEAN", "bit"),
    ("BIGINT", "bigint"),
    ("HUGEINT", "bigint"),
    ("INTEGER", "int"),
    ("SMALLINT", "smallint"),
    ("TINYINT", "tinyint"),
    ("UBIGINT", "bigint"),
    ("UINTEGER", "int"),
    ("DOUBLE", "float"),
    ("FLOAT", "float"),
    ("REAL", "float"),
    ("DECIMAL", "decimal"),
    ("NUMERIC", "decimal"),
    ("VARCHAR", "varchar"),
)


def duckdb_type_to_sql(duck_type: str) -> str:
    """Map a DuckDB column type to the same SQL-ish names SQL Server reports.

    Schema parity matters: the schema-retrieval agent and the ChromaDB embeddings key off
    these strings, so both backends must describe an identical column in identical terms.
    """
    text = str(duck_type or "").upper()
    for prefix, sql_type in _DUCKDB_TO_SQL_TYPE:
        if text.startswith(prefix):
            return sql_type
    return "varchar"


class LocalFileDataSource(DataSource):
    """DuckDB over a local .csv or .xlsx, exposing the same interface as SqlServerDataSource.

    The file is loaded once at first use and registered twice - as the bare table name
    (May_2) and as a view under the dbo schema (dbo.May_2) - so both qualified and
    unqualified references in generated SQL resolve.
    """

    name = "local"

    def __init__(self) -> None:
        import duckdb  # local import: local mode only

        self._duckdb = duckdb
        self._lock = threading.RLock()
        self._connection = None
        self._columns: list[dict] = []
        self._load_error: str | None = None
        self._row_count = 0
        # Only populated when Parquet caching fails and the table must stay in memory.
        self._fallback_frame: pd.DataFrame | None = None

    def describe(self) -> str:
        if settings.is_local_csv:
            return f"Local CSV {settings.LOCAL_DATA_PATH}"
        return f"Local Excel {settings.LOCAL_DATA_PATH} (sheet {settings.LOCAL_SHEET_NAME or 'first'})"

    # -- loading ----------------------------------------------------------------------

    def _cache_path(self, source: Path) -> Path:
        cache_dir = settings.resolved("./data/cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        stat = source.stat()
        stamp = f"{int(stat.st_mtime)}-{stat.st_size}"
        sheet = re.sub(r"[^A-Za-z0-9_]+", "_", settings.LOCAL_SHEET_NAME or "default")
        return cache_dir / f"{source.stem}.{sheet}.{stamp}.parquet"

    def _resolve_source(self) -> Path:
        source = Path(settings.LOCAL_DATA_PATH).expanduser()
        if not source.is_absolute():
            source = settings.resolved(str(source))
        if not source.exists():
            raise DatabaseError(
                f"LOCAL_DATA_PATH does not exist: {source}. Set it to the .csv or .xlsx "
                "you want to query, or switch DATA_SOURCE=sqlserver in .env."
            )
        return source

    def _prepare_parquet(self) -> Path | None:
        """Ensure a Parquet copy of the source table exists and return its path.

        DuckDB then reads that file directly instead of holding the whole table in Python
        memory. On this data (~124 columns) that keeps hundreds of MB free, which matters
        because a resident 7B model already dominates RAM - and memory pressure is what
        makes native extension loads fail elsewhere in the process.

        Returns None if Parquet cannot be produced; the caller then falls back to
        registering an in-memory DataFrame.
        """
        source = self._resolve_source()
        cache = self._cache_path(source)
        if cache.exists():
            logger.info("Using Parquet cache %s", cache.name)
            return cache

        if source.suffix.lower() in LOCAL_CSV_SUFFIXES:
            return self._csv_to_parquet(source, cache)
        return self._workbook_to_parquet(source, cache)

    def _csv_to_parquet(self, source: Path, cache: Path) -> Path | None:
        """Convert with DuckDB alone - the CSV never passes through Python memory.

        `sample_size=-1` types the columns off the whole file rather than the first 20k
        rows. On this data a column can be numeric for thousands of rows and then hold a
        code, and a sampled guess would abort the load part-way through.
        """
        src_literal = str(source).replace("'", "''")
        dst_literal = str(cache).replace("'", "''")
        try:
            writer = self._duckdb.connect(database=":memory:")
            try:
                writer.execute(
                    f"COPY (SELECT * FROM read_csv_auto('{src_literal}', "
                    f"header=true, sample_size=-1)) "
                    f"TO '{dst_literal}' (FORMAT PARQUET)"
                )
            finally:
                writer.close()
            self._prune_stale_caches(cache)
            logger.info("Cached %s to %s", source.name, cache.name)
            return cache
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail the load
            logger.warning(
                "DuckDB could not convert %s to Parquet (%s); reading it with pandas instead.",
                source.name, exc,
            )
            try:
                self._fallback_frame = pd.read_csv(source, low_memory=False)
            except Exception as inner:  # noqa: BLE001
                raise DatabaseError(f"Failed to read {source.name}: {inner}") from inner
            self._fallback_frame.columns = [
                str(c).strip() for c in self._fallback_frame.columns
            ]
            return None

    def _workbook_to_parquet(self, source: Path, cache: Path) -> Path | None:
        frame = self._read_workbook(source)
        try:
            # Written by DuckDB, NOT pandas.to_parquet. pandas would require pyarrow, and
            # importing pyarrow on Windows breaks onnxruntime's native load
            # ("DLL load failed while importing onnxruntime_pybind11_state"), which in turn
            # disables ChromaDB - and, once, segfaulted the worker mid-request. DuckDB has
            # its own Parquet writer, so the conflicting dependency is simply not needed.
            writer = self._duckdb.connect(database=":memory:")
            try:
                writer.register("_export_frame", frame)
                literal = str(cache).replace("'", "''")
                writer.execute(
                    f"COPY (SELECT * FROM _export_frame) TO '{literal}' (FORMAT PARQUET)"
                )
            finally:
                writer.close()
            self._prune_stale_caches(cache)
            logger.info("Cached %s rows to %s", len(frame), cache.name)
            return cache
        except Exception as exc:  # noqa: BLE001 - caching is an optimisation, not a requirement
            logger.warning("Could not write the Parquet cache (%s); using memory instead.", exc)
            self._fallback_frame = frame
            return None

    def _read_workbook(self, source: Path) -> pd.DataFrame:
        """Excel only - CSV is handled by DuckDB in _csv_to_parquet."""
        logger.info("Reading %s (first load parses the workbook; later starts use a cache)...", source)
        try:
            frame = pd.read_excel(
                source,
                sheet_name=settings.LOCAL_SHEET_NAME or 0,
                engine="openpyxl",
            )
        except ImportError as exc:
            raise DatabaseError(
                "Reading .xlsx requires openpyxl. Run: pip install openpyxl"
            ) from exc
        except ValueError as exc:
            raise DatabaseError(
                f"Could not read sheet '{settings.LOCAL_SHEET_NAME}' from {source.name}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"Failed to read {source.name}: {exc}") from exc

        if isinstance(frame, dict):  # sheet_name=None would return a dict
            frame = next(iter(frame.values()))
        frame.columns = [str(c).strip() for c in frame.columns]
        return frame

    def _prune_stale_caches(self, keep: Path) -> None:
        for old in keep.parent.glob(f"{keep.name.split('.')[0]}.*.parquet"):
            if old != keep:
                try:
                    old.unlink()
                except OSError:
                    pass

    def _ensure_loaded(self):
        with self._lock:
            if self._connection is not None:
                return self._connection
            if self._load_error is not None:
                raise DatabaseError(self._load_error)

            try:
                self._fallback_frame = None
                parquet = self._prepare_parquet()
                schema, table = split_table(settings.DB_TABLE)

                connection = self._duckdb.connect(database=":memory:")
                if parquet is not None:
                    # A view over the Parquet file: DuckDB streams and pushes predicates
                    # down instead of materialising 74k x 124 cells in memory.
                    literal = str(parquet).replace("'", "''")
                    connection.execute(
                        f"CREATE VIEW \"{table}\" AS SELECT * FROM read_parquet('{literal}')"
                    )
                else:
                    frame = self._fallback_frame
                    connection.register("_source_frame", frame)
                    connection.execute(f'CREATE TABLE "{table}" AS SELECT * FROM _source_frame')
                    connection.unregister("_source_frame")
                    self._fallback_frame = None

                # Register under the dbo prefix too, so both `May_2` and `dbo.May_2`
                # resolve and generated T-SQL needs no rewriting of the table name.
                connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                connection.execute(
                    f'CREATE VIEW "{schema}"."{table}" AS SELECT * FROM "{table}"'
                )

                described = connection.execute(f'DESCRIBE "{table}"').fetchall()
                self._columns = [
                    {"column_name": str(row[0]), "data_type": duckdb_type_to_sql(str(row[1]))}
                    for row in described
                ]
                self._row_count = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                self._connection = connection
                logger.info(
                    "Local data source ready: %s rows, %s columns, table '%s' (also '%s.%s')",
                    self._row_count, len(self._columns), table, schema, table,
                )
                return connection
            except DatabaseError as exc:
                self._load_error = str(exc)
                raise
            except Exception as exc:  # noqa: BLE001
                self._load_error = f"Could not initialise the local data source: {exc}"
                raise DatabaseError(self._load_error) from exc

    # -- interface --------------------------------------------------------------------

    def check_connection(self) -> tuple[bool, str]:
        try:
            connection = self._ensure_loaded()
            _, table = split_table(settings.DB_TABLE)
            connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchall()
            return True, f"ok ({self._row_count} rows, {len(self._columns)} columns)"
        except DatabaseError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - health check must never raise
            return False, f"Local data source unavailable: {exc}"

    def get_table_columns(self) -> list[dict]:
        self._ensure_loaded()
        if not self._columns:
            hint = (
                "Check that the file has a header row."
                if settings.is_local_csv
                else "Check LOCAL_SHEET_NAME."
            )
            raise DatabaseError(f"{settings.LOCAL_DATA_PATH} produced no columns. {hint}")
        return [dict(c) for c in self._columns]

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


# ======================================================================================
# Factory
# ======================================================================================

_INSTANCE: DataSource | None = None
_FACTORY_LOCK = threading.Lock()

_BACKENDS = {
    "sqlserver": SqlServerDataSource,
    "local": LocalFileDataSource,
}


def get_datasource() -> DataSource:
    """Return the single active data source, constructing it on first use.

    Only the backend named by DATA_SOURCE is ever instantiated.
    """
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _FACTORY_LOCK:
        if _INSTANCE is None:
            mode = settings.DATA_SOURCE
            backend = _BACKENDS.get(mode)
            if backend is None:  # settings validation should have caught this already
                raise DatabaseError(
                    f"DATA_SOURCE='{mode}' is not supported. "
                    f"Use one of: {', '.join(sorted(_BACKENDS))}."
                )
            try:
                _INSTANCE = backend()
            except ImportError as exc:
                raise DatabaseError(
                    f"DATA_SOURCE={mode} needs a package that is not installed: {exc}. "
                    "Run: pip install -r requirements.txt"
                ) from exc
            logger.info("Active data source: %s", _INSTANCE.describe())
    return _INSTANCE


def reset_datasource() -> None:
    """Drop the cached instance. Used by tests and by scripts that switch modes."""
    global _INSTANCE
    with _FACTORY_LOCK:
        _INSTANCE = None
