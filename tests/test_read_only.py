"""The app never writes: find_write_operation() and the data sources' second check.

No SQL Server is needed. pyodbc is replaced by a fake module, so these prove the refusal
happens before any connection and that a read is always rolled back - they do NOT prove
anything about a live server's permissions (scripts/check_readonly_access.py does that).
"""
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.core import datasource as datasource_module
from backend.core.datasource import (
    DatabaseError,
    LocalFileDataSource,
    SqlServerDataSource,
    split_table,
)
from backend.core.read_only import describe_write_operation, find_write_operation

ROOT = Path(__file__).resolve().parent.parent
TABLE = datasource_module.settings.DB_TABLE


def _live_columns() -> list[str]:
    data = json.loads((ROOT / "backend" / "knowledge" / "column_aliases.json").read_text("utf-8"))
    return list(data["columns"])


COLUMNS = _live_columns()


# --------------------------------------------------------------------------------------
# find_write_operation - what must be refused
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql, expected",
    [
        # Passed the old guard: SELECT ... INTO creates a table.
        (f"SELECT [BRANCH_NAME] INTO dbo.stolen FROM {TABLE}", "INTO"),
        (f"SELECT TOP 5 [BRANCH_NAME] INTO #t FROM {TABLE}", "INTO"),
        # T-SQL stacks statements without a semicolon.
        (f"SELECT [BRANCH_NAME] FROM {TABLE} DELETE FROM {TABLE}", "DELETE"),
        (f"SELECT 1 AS x FROM {TABLE} UPDATE {TABLE} SET [GROSS_PREMIUM] = 0", "UPDATE"),
        (f"SELECT 1 AS x FROM {TABLE} INSERT INTO {TABLE} DEFAULT VALUES", "INSERT"),
        # `1DELETE` lexes as `1` then `DELETE` - a digit is not a word boundary.
        (f"SELECT 1DELETE FROM {TABLE}", "DELETE"),
        (f"SELECT 1 AS x FROM {TABLE} TRUNCATE TABLE {TABLE}", "TRUNCATE"),
        (f"SELECT 1 AS x MERGE INTO {TABLE} AS t USING {TABLE} AS s ON 1 = 0 "
         "WHEN NOT MATCHED THEN INSERT DEFAULT VALUES", "MERGE"),
        (f"SELECT 1 AS x FROM {TABLE} DROP TABLE {TABLE}", "DROP"),
        (f"SELECT 1 AS x ALTER TABLE {TABLE} ADD c INT", "ALTER"),
        ("SELECT 1 AS x CREATE TABLE dbo.t (c INT)", "CREATE"),
        ("SELECT 1 AS x DISABLE TRIGGER ALL ON dbo.May_2", "TRIGGER"),
        ("SELECT 1 AS x GRANT SELECT ON dbo.May_2 TO public", "GRANT"),
        ("SELECT 1 AS x REVOKE SELECT ON dbo.May_2 FROM public", "REVOKE"),
        ("SELECT 1 AS x DENY SELECT ON dbo.May_2 TO public", "DENY"),
        ("SELECT 1 AS x EXEC ('DELETE FROM dbo.May_2')", "EXEC"),
        ("SELECT 1 AS x EXECUTE AS LOGIN = 'sa'", "EXECUTE"),
        ("SELECT 1 AS x FROM dbo.May_2 WHERE 1 = 0 sp_executesql N'x'", "SP_EXECUTESQL"),
        ("SELECT 1 AS x master..xp_cmdshell 'dir'", "XP_CMDSHELL"),
        ("SELECT 1 AS x BACKUP DATABASE d TO DISK = 'x'", "BACKUP"),
        ("SELECT 1 AS x RESTORE DATABASE d FROM DISK = 'x'", "RESTORE"),
        ("SELECT 1 AS x SHUTDOWN", "SHUTDOWN"),
        ("SELECT 1 AS x DBCC SHRINKDATABASE(0)", "DBCC"),
        ("SELECT 1 AS x KILL 53", "KILL"),
        ("SELECT 1 AS x RECONFIGURE", "RECONFIGURE"),
        ("SELECT 1 AS x CHECKPOINT", "CHECKPOINT"),
        (f"SELECT [BRANCH_NAME] FROM {TABLE} WAITFOR DELAY '00:10:00'", "WAITFOR"),
        ("SELECT * FROM OPENROWSET(BULK 'C:/x.csv', SINGLE_CLOB) AS f", "OPENROWSET"),
        ("SELECT * FROM OPENQUERY(srv, 'SELECT 1')", "OPENQUERY"),
        ("SELECT * FROM OPENDATASOURCE('SQLNCLI', 'x').db.dbo.t", "OPENDATASOURCE"),
        ("SELECT * FROM OPENXML(@h, '/r')", "OPENXML"),
        ("SELECT 1 AS x UPDATETEXT t.c @p 0 NULL 'x'", "UPDATETEXT"),
        ("SELECT 1 AS x WRITETEXT t.c @p 'x'", "WRITETEXT"),
        ("SELECT 1 AS x SETUSER 'dbo'", "SETUSER"),
        ("SELECT 1 AS x RECEIVE TOP (1) * FROM q", "RECEIVE"),
        ("SELECT 1 AS x BULK INSERT dbo.May_2 FROM 'C:/x.csv'", "BULK INSERT"),
        ("SELECT 1 AS x BULK\n  INSERT dbo.May_2 FROM 'C:/x.csv'", "BULK INSERT"),
        ("BULK INSERT dbo.May_2 FROM 'C:/x.csv'", "BULK INSERT"),
        # Locking hints that would block the job loading the table.
        (f"SELECT [BRANCH_NAME] FROM {TABLE} WITH (UPDLOCK)", "UPDLOCK"),
        (f"SELECT [BRANCH_NAME] FROM {TABLE} WITH (XLOCK)", "XLOCK"),
        (f"SELECT [BRANCH_NAME] FROM {TABLE} WITH (TABLOCKX)", "TABLOCKX"),
        (f"SELECT [BRANCH_NAME] FROM {TABLE} WITH (TABLOCK)", "TABLOCK"),
        (f"SELECT [BRANCH_NAME] FROM {TABLE} WITH (HOLDLOCK)", "HOLDLOCK"),
        ("SELECT NEXT VALUE FOR dbo.seq AS n", "NEXT VALUE FOR"),
        ("SELECT NEXT /* x */ VALUE\nFOR dbo.seq AS n", "NEXT VALUE FOR"),
        # ODBC call escape: the driver turns it into a procedure call.
        ("SELECT 1 AS x {call dbo.usp_purge}", "CALL"),
        ("{ ? = call dbo.usp_purge }", "CALL"),
        # A leading non-SELECT - a bare name there runs a procedure in T-SQL.
        (f"UPDATE {TABLE} SET [GROSS_PREMIUM] = 0", "UPDATE"),
        ("usp_purge_all", "USP_PURGE_ALL"),
        ("  -- note\n[dbo].[usp_purge_all]", "[DBO].[USP_PURGE_ALL]"),
        ("DECLARE @x INT", "DECLARE"),
        ("SET ROWCOUNT 0", "SET"),
        # DuckDB statements (local mode) at the start or after `;`.
        ("COPY May_2 TO 'C:/out.csv'", "COPY"),
        (f"SELECT [BRANCH_NAME] FROM {TABLE}; COPY May_2 TO 'C:/out.csv'", "COPY"),
        ("SELECT 1 AS x; ATTACH 'other.db' AS o", "ATTACH"),
        ("SELECT 1 AS x; DETACH o", "DETACH"),
        ("SELECT 1 AS x; PRAGMA threads = 1", "PRAGMA"),
        ("SELECT 1 AS x; INSTALL httpfs", "INSTALL"),
        ("SELECT 1 AS x; LOAD httpfs", "LOAD"),
        ("SELECT 1 AS x; EXPORT DATABASE 'C:/out'", "EXPORT"),
        ("SELECT 1 AS x; IMPORT DATABASE 'C:/in'", "IMPORT"),
        ("SELECT 1 AS x; CALL pragma_version()", "CALL"),
        ("SELECT 1 AS x; VACUUM", "VACUUM"),
        ("SELECT 1 AS x; FORCE CHECKPOINT", "CHECKPOINT"),
        # A statement start hidden behind a nested comment.
        ("SELECT 1 AS x; /* a /* b */ c */ COPY May_2 TO 'x'", "COPY"),
        ("SELECT 1 AS x;\n-- c\r\nINSTALL httpfs", "INSTALL"),
    ],
)
def test_writes_are_found(sql, expected):
    assert find_write_operation(sql) == expected


@pytest.mark.parametrize("keyword", ["delete", "Delete", "dElEtE"])
def test_matching_ignores_case(keyword):
    assert find_write_operation(f"SELECT 1 AS x {keyword} FROM dbo.May_2") == "DELETE"


def test_the_first_construct_in_the_text_is_reported():
    assert find_write_operation("SELECT 1 AS x INTO #t DELETE FROM dbo.May_2") == "INTO"


def test_a_keyword_inside_a_comment_is_refused_on_the_safe_side():
    """Skipping comments needs a tokenizer that agrees with SQL Server on every edge
    case; a disagreement would hide a write. A refused comment is the cheap failure."""
    assert find_write_operation(f"SELECT [BRANCH_NAME] FROM {TABLE} -- then DROP it") == "DROP"


# --------------------------------------------------------------------------------------
# find_write_operation - what must NOT be refused
# --------------------------------------------------------------------------------------

def test_there_are_124_columns_to_check():
    assert len(COLUMNS) == 124


@pytest.mark.parametrize("column", COLUMNS)
def test_no_real_column_name_is_mistaken_for_a_write(column):
    for sql in (
        f"SELECT TOP 10 [{column}], COUNT(*) AS n FROM {TABLE} GROUP BY [{column}]",
        f"SELECT TOP 10 {column} FROM {TABLE} WHERE {column} IS NOT NULL",
    ):
        assert find_write_operation(sql) is None, sql


def test_every_column_at_once_is_a_read():
    select_list = ", ".join(f"[{c}]" for c in COLUMNS)
    assert find_write_operation(f"SELECT TOP 5 {select_list} FROM {TABLE}") is None


@pytest.mark.parametrize(
    "identifier",
    [
        "UPDATED_ON", "CREATED_BY", "[INTO_FLAG]", "INTO_FLAG", "LOADING_ON_PREMIUM",
        "Business_Type_Fresh_Renewal", "DELETED_FLAG", "INSERTED_AT", "EXECUTIVE_NAME",
        "MERGED_POLICY", "ALTERNATE_MOBILE", "DROPPED_CASES", "GRANTED_ON", "TRUNCATED_NAME",
        "COPY_COUNT", "CALL_COUNT", "LOAD_FACTOR", "EXPORT_FLAG", "KILLED_FLAG", "BACKUP_BRANCH",
        "RESTORED_ON", "user_sp_code", "total_into_count", "CREATEDDATE", "UPDATE_TS2",
    ],
)
def test_identifiers_that_merely_contain_a_keyword_are_reads(identifier):
    sql = f"SELECT TOP 10 {identifier}, SUM([GROSS_PREMIUM]) AS total FROM {TABLE} GROUP BY {identifier}"
    assert find_write_operation(sql) is None


@pytest.mark.parametrize(
    "sql",
    [
        # Straight from data/chat_logs.csv (generated_sql_query).
        "SELECT TOP 10 [BRANCH_NAME], [STATE], SUM([GROSS_PREMIUM]) AS total_gross_premium "
        "FROM dbo.May_2 GROUP BY [BRANCH_NAME], [STATE] ORDER BY total_gross_premium DESC",
        "SELECT TOP 200      [Vertical_Channel_Map],      CAST(DATEADD(day, -(DATEPART(weekday, "
        "[POLICY_ISSUE_DATE]) - 1), [POLICY_ISSUE_DATE]) AS date) AS week_start,      "
        "SUM([GROSS_PREMIUM]) AS total_gross_premium FROM      dbo.May_2 GROUP BY      "
        "[Vertical_Channel_Map],      week_start ORDER BY      week_start,      "
        "[Vertical_Channel_Map]",
        "SELECT TOP 5      [STATE],      SUM([GROSS_PREMIUM]) AS total_gross_premium,     "
        "SUM([GROSS_PREMIUM]) * 100.0 / SUM(SUM([GROSS_PREMIUM])) OVER () AS pct_share FROM      "
        "dbo.May_2 GROUP BY      [STATE] ORDER BY      total_gross_premium DESC",
        "SELECT TOP 5 [INTERMEDIARY], SUM([GROSS_PREMIUM]) AS total_premium FROM dbo.May_2 "
        "GROUP BY [INTERMEDIARY] ORDER BY total_premium DESC",
        # The combined view the log stores for a two-section answer.
        "SELECT TOP 100 [STATE], SUM([GROSS_PREMIUM]) AS total_gross_premium FROM dbo.May_2 "
        "GROUP BY [STATE] ORDER BY total_gross_premium DESC  SELECT TOP 100 [STATE], "
        "[INTERMEDIARY], SUM([GROSS_PREMIUM]) AS total_premium FROM dbo.May_2 GROUP BY "
        "[STATE], [INTERMEDIARY] ORDER BY total_premium DESC",
        # Shapes the prompt and date_windows produce.
        "SELECT MAX([POLICY_ISSUE_DATE]) AS max_date FROM dbo.May_2",
        "SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM dbo.May_2 "
        "WHERE [POLICY_ISSUE_DATE] >= '2026-05-08' AND [POLICY_ISSUE_DATE] < '2026-05-15' "
        "GROUP BY [BRANCH_NAME] ORDER BY total DESC",
        "SELECT TOP 10 YEAR([POLICY_ISSUE_DATE]) AS yr, SUM([NET_PREMIUM]) AS total "
        "FROM dbo.May_2 WHERE MONTH([POLICY_ISSUE_DATE]) = 9 GROUP BY YEAR([POLICY_ISSUE_DATE]) "
        "ORDER BY yr",
        "SELECT TOP 24 FORMAT([POLICY_ISSUE_DATE], 'yyyy-MM') AS month, "
        "SUM([GROSS_PREMIUM]) AS total FROM dbo.May_2 "
        "GROUP BY FORMAT([POLICY_ISSUE_DATE], 'yyyy-MM') ORDER BY month",
        "SELECT TOP 1 [USGI_SUM_INSURED] FROM dbo.May_2 WHERE [POLICY_NO] = '1029156133'",
        "WITH ranked AS (SELECT [STATE], [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total, "
        "ROW_NUMBER() OVER (PARTITION BY [STATE] ORDER BY SUM([GROSS_PREMIUM]) DESC) AS rn "
        "FROM dbo.May_2 GROUP BY [STATE], [BRANCH_NAME]) "
        "SELECT TOP 50 [STATE], [BRANCH_NAME], total FROM ranked WHERE rn <= 3",
        "SELECT TOP 10 REPLACE([BRANCH_NAME], '-', ' ') AS branch, "
        "CONVERT(varchar(7), [POLICY_ISSUE_DATE], 120) AS ym, "
        "COALESCE([STATE], 'not available') AS st, ISNULL([NCB], 0) AS ncb, "
        "IIF([NET_PREMIUM] > 0, 'positive', 'zero') AS sign_flag, "
        "CASE WHEN [Business_Type_Fresh_Renewal] = 'Renewal' THEN 1 ELSE 0 END AS is_renewal, "
        "DATEDIFF(day, [START_DATE], [EXPIRY_DATE]) AS tenure, "
        "AVG([COMMISSION_PER]) OVER (PARTITION BY [STATE]) AS avg_comm, "
        "MIN([GROSS_PREMIUM]) AS lo, MAX([GROSS_PREMIUM]) AS hi "
        "FROM dbo.May_2 WITH (NOLOCK) GROUP BY [BRANCH_NAME], [POLICY_ISSUE_DATE], [STATE], "
        "[NCB], [NET_PREMIUM], [Business_Type_Fresh_Renewal], [START_DATE], [EXPIRY_DATE], "
        "[COMMISSION_PER]",
        "SELECT [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM dbo.May_2 GROUP BY "
        "[BRANCH_NAME] ORDER BY total DESC OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
        "SELECT TOP 10 [STATE], STRING_AGG([BRANCH_NAME], ', ') WITHIN GROUP "
        "(ORDER BY [BRANCH_NAME]) AS branches FROM dbo.May_2 GROUP BY [STATE]",
        "(SELECT TOP 1 [STATE] FROM dbo.May_2)",
        "-- top branches\nSELECT TOP 5 [BRANCH_NAME] FROM dbo.May_2",
        "SELECT TOP 5 [BRANCH_NAME] FROM dbo.May_2;",
    ],
)
def test_realistic_generated_queries_are_reads(sql):
    assert find_write_operation(sql) is None


@pytest.mark.parametrize(
    "value", ["CALL CENTER", "MARINE EXPORT", "IMPORT CARGO", "BULK CARGO", "LOAD", "COPY"]
)
def test_a_data_value_that_is_a_duckdb_keyword_can_still_be_filtered_on(value):
    """'CALL CENTER' is a real User_Name value; these words are statements only in DuckDB,
    where they can only start a statement."""
    sql = f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE} WHERE [User_Name] = '{value}'"
    assert find_write_operation(sql) is None


@pytest.mark.parametrize("sql", ["", "   ", None, ";", "  ;  "])
def test_empty_text_is_not_a_write(sql):
    assert find_write_operation(sql) is None


def test_the_reason_names_the_construct_and_why():
    assert describe_write_operation("INTO") == "INTO (SELECT ... INTO creates a table)"
    assert describe_write_operation("xp_cmdshell").startswith("XP_CMDSHELL (")
    assert "stored procedure" in describe_write_operation("SP_EXECUTESQL")
    assert "SELECT or WITH" in describe_write_operation("USP_PURGE_ALL")


# --------------------------------------------------------------------------------------
# Local (DuckDB) data source
# --------------------------------------------------------------------------------------

@pytest.fixture
def local_source(tmp_path, monkeypatch):
    """A real DuckDB source over a tiny CSV, with its Parquet cache kept in tmp_path."""
    csv_path = tmp_path / "tiny.csv"
    csv_path.write_text(
        "BRANCH_NAME,GROSS_PREMIUM\nMUMBAI,100.5\nDELHI,200.25\nPUNE,50\n", encoding="utf-8"
    )
    monkeypatch.setattr(datasource_module.settings, "DATA_SOURCE", "local")
    monkeypatch.setattr(datasource_module.settings, "LOCAL_DATA_PATH", str(csv_path))
    monkeypatch.setattr(
        LocalFileDataSource, "_cache_path", lambda self, source: tmp_path / "tiny.parquet"
    )
    return LocalFileDataSource()


def test_local_source_refuses_a_write_before_loading_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(datasource_module.settings, "DATA_SOURCE", "local")
    monkeypatch.setattr(
        datasource_module.settings, "LOCAL_DATA_PATH", str(tmp_path / "missing.csv")
    )
    source = LocalFileDataSource()
    with pytest.raises(DatabaseError, match="could change data: DELETE"):
        source.run_select(f"DELETE FROM {TABLE}")
    assert source._connection is None


@pytest.mark.parametrize(
    "sql, keyword",
    [
        # DuckDB really runs every `;`-separated statement - this DELETE would empty it.
        ("SELECT COUNT(*) AS n FROM {t}; DELETE FROM {t}", "DELETE"),
        ("SELECT 1 AS n; COPY {bare} TO '{out}'", "COPY"),
        ("SELECT [BRANCH_NAME] INTO copied FROM {t}", "INTO"),
    ],
)
def test_local_source_refuses_writes_and_the_data_is_untouched(local_source, tmp_path, sql, keyword):
    _, bare = split_table(TABLE)
    out = (tmp_path / "leak.csv").as_posix()
    with pytest.raises(DatabaseError, match=f"could change data: {keyword}"):
        local_source.run_select(sql.format(t=TABLE, bare=bare, out=out))
    assert not (tmp_path / "leak.csv").exists()
    result = local_source.run_select(f"SELECT COUNT(*) AS n FROM {TABLE}")
    assert int(result.dataframe.iloc[0, 0]) == 3


def test_local_source_still_answers_a_read(local_source):
    result = local_source.run_select(
        f"SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM {TABLE} "
        "GROUP BY [BRANCH_NAME] ORDER BY total DESC"
    )
    assert list(result.dataframe["BRANCH_NAME"]) == ["DELHI", "MUMBAI", "PUNE"]


def test_local_source_holds_no_write_permissions(local_source):
    assert local_source.write_permissions() == []


# --------------------------------------------------------------------------------------
# SQL Server data source, against a fake pyodbc
# --------------------------------------------------------------------------------------

class _FakeError(Exception):
    pass


class _FakeOperationalError(_FakeError):
    pass


class _FakeProgrammingError(_FakeError):
    pass


class _FakeInterfaceError(_FakeError):
    pass


class _FakeCursor:
    def __init__(self, server):
        self.server = server
        self.description = None

    def execute(self, sql, *params):
        self.server.log.append("execute")
        self.server.executed.append((sql, params))
        if self.server.execute_error is not None:
            raise self.server.execute_error
        self.description = [(name,) for name in self.server.columns]

    def fetchmany(self, size):
        self.server.log.append("fetchmany")
        return list(self.server.rows[:size])

    def fetchone(self):
        self.server.log.append("fetchone")
        return self.server.rows[0] if self.server.rows else None

    def close(self):
        self.server.log.append("cursor.close")


class _FakeConnection:
    def __init__(self, server):
        self.server = server
        self.timeout = 0

    def cursor(self):
        return _FakeCursor(self.server)

    def rollback(self):
        self.server.log.append("rollback")
        if self.server.rollback_error is not None:
            raise self.server.rollback_error

    def commit(self):  # never expected - a test fails if it is called
        self.server.log.append("COMMIT")

    def close(self):
        self.server.log.append("close")


class _FakeServer:
    def __init__(self):
        self.log: list[str] = []
        self.executed: list[tuple] = []
        self.connect_kwargs: list[dict] = []
        self.columns = ["BRANCH_NAME", "total"]
        self.rows = [("MUMBAI", Decimal("69205972.68")), ("DELHI", Decimal("41003311.10"))]
        self.execute_error = None
        self.rollback_error = None
        self.refuse_connections = False

    def connect(self, connection_string, **kwargs):
        if self.refuse_connections:
            raise AssertionError("run_select connected to the database for a refused write")
        self.connect_kwargs.append(kwargs)
        self.log.append("connect")
        return _FakeConnection(self)


@pytest.fixture
def fake_server(monkeypatch):
    server = _FakeServer()
    module = SimpleNamespace(
        connect=server.connect,
        OperationalError=_FakeOperationalError,
        ProgrammingError=_FakeProgrammingError,
        InterfaceError=_FakeInterfaceError,
    )
    monkeypatch.setitem(sys.modules, "pyodbc", module)
    return server


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS probe INTO #usgi_readonly_probe",
        f"SELECT 1 AS probe FROM {TABLE} DELETE FROM {TABLE}",
        f"UPDATE {TABLE} SET [GROSS_PREMIUM] = 0",
        "EXEC sp_who",
    ],
)
def test_sqlserver_refuses_a_write_without_connecting(fake_server, sql):
    fake_server.refuse_connections = True
    with pytest.raises(DatabaseError, match="could change data"):
        SqlServerDataSource().run_select(sql)
    assert fake_server.executed == []


def test_sqlserver_connects_with_autocommit_off(fake_server):
    SqlServerDataSource().run_select(f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE}")
    assert fake_server.connect_kwargs[0]["autocommit"] is False


def test_sqlserver_rolls_back_after_every_read(fake_server):
    result = SqlServerDataSource().run_select(
        f"SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM {TABLE} "
        "GROUP BY [BRANCH_NAME]"
    )
    assert fake_server.log == [
        "connect", "execute", "fetchmany", "cursor.close", "rollback", "close",
    ]
    # The dtype fix still applies on the way out.
    assert pd.api.types.is_float_dtype(result.dataframe["total"])
    assert result.row_count == 2


def test_a_failed_rollback_never_hides_the_real_error(fake_server):
    fake_server.execute_error = _FakeProgrammingError("42000", "Incorrect syntax near 'FROM'.")
    fake_server.rollback_error = RuntimeError("rollback broke")
    with pytest.raises(DatabaseError, match="The generated SQL was invalid") as info:
        SqlServerDataSource().run_select(f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE}")
    assert "rollback broke" not in str(info.value)
    assert fake_server.log[-3:] == ["cursor.close", "rollback", "close"]


def test_a_failed_rollback_after_a_good_read_still_returns_the_rows(fake_server):
    fake_server.rollback_error = RuntimeError("rollback broke")
    result = SqlServerDataSource().run_select(f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE}")
    assert result.row_count == 2
    assert fake_server.log[-1] == "close"


@pytest.mark.parametrize(
    "row, expected",
    [
        ((0, 0, 0, 0, 0), []),
        ((1, 0, 0, 0, 0), ["INSERT"]),
        ((1, 1, 1, 1, 1), ["INSERT", "UPDATE", "DELETE", "ALTER", "CREATE TABLE"]),
        ((0, 0, 0, 1, 0), ["ALTER"]),
        # NULL = SQL Server could not resolve the table for this login - unknown, not "no".
        ((None, None, None, None, 0), None),
    ],
)
def test_sqlserver_write_permissions(fake_server, row, expected):
    fake_server.rows = [row]
    assert SqlServerDataSource().write_permissions() == expected
    schema, table = split_table(TABLE)
    sql, params = fake_server.executed[0]
    assert "HAS_PERMS_BY_NAME" in sql
    assert params == (schema, table)
    assert "rollback" in fake_server.log


def test_sqlserver_write_permissions_never_raise(fake_server):
    def unreachable(*args, **kwargs):
        raise _FakeInterfaceError("IM002", "Data source name not found")

    sys.modules["pyodbc"].connect = unreachable
    assert SqlServerDataSource().write_permissions() is None
