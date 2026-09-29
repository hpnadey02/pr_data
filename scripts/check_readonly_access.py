"""Is the chatbot's database access really read-only? PASS/FAIL for the .env login.

Run it on whichever machine has the database, after the DBA has run
scripts/sqlserver_readonly.sql:

    python scripts\\check_readonly_access.py

[1] App-side guards - both modes. The generated-SQL guard and the data source must
    each refuse a DELETE and a SELECT ... INTO.
[2] Database - DATA_SOURCE=sqlserver only. Connects AS the configured login and asks
    SQL Server what that login may do: sysadmin / db_owner / db_datawriter /
    db_ddladmin membership, SELECT present, no INSERT/UPDATE/DELETE/ALTER, and the
    Row-Level Security policy present, enabled and blocking THIS login.

Nothing is ever written. The data-source probes are statements that would be harmless
even if a broken guard let them through (a temp table; a DELETE on a table that does
not exist), and the database checks are SELECTs that are rolled back.

Exit code 0 = every check passed.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ConfigurationError, get_settings  # noqa: E402

PASS = "  [PASS]"
FAIL = "  [FAIL]"
INFO = "  [INFO]"

REQUIRED_BLOCKS = ("AFTER INSERT", "AFTER UPDATE", "BEFORE UPDATE", "BEFORE DELETE")
SCRIPT = "scripts\\sqlserver_readonly.sql"


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, title: str, detail: str = "", remedy: str = "") -> bool:
        print(f"{PASS if ok else FAIL} {title}")
        if detail:
            print(f"         {detail}")
        if not ok:
            self.failures.append(title)
            if remedy:
                print(f"         -> {remedy}")
        return ok


# --------------------------------------------------------------------------------------
# [1] App-side guards
# --------------------------------------------------------------------------------------

def check_app_guards(report: Report, settings) -> None:
    from backend.agents.sql_guard import SQLValidationError, validate_sql

    table = settings.DB_TABLE
    for label, sql in (
        ("a stacked DELETE", f"SELECT TOP 5 [BRANCH_NAME] FROM {table} DELETE FROM {table}"),
        ("SELECT ... INTO", f"SELECT TOP 5 [BRANCH_NAME] INTO dbo.copied FROM {table}"),
    ):
        try:
            validate_sql(sql)
            report.check(False, f"SQL guard refuses {label}", "it was ACCEPTED",
                         "backend/agents/sql_guard.py is not using backend/core/read_only.py")
        except SQLValidationError as exc:
            report.check("disallowed keyword" in str(exc), f"SQL guard refuses {label}", str(exc))

    try:
        from backend.core.datasource import get_datasource

        source = get_datasource()
    except Exception as exc:  # noqa: BLE001
        report.check(False, "Data source available for the refusal probes", str(exc))
        return

    # Harmless even if they did reach SQL Server: #usgi_readonly_probe does not exist, and
    # a temp table vanishes with its connection.
    for label, sql in (
        ("a stacked DELETE", "SELECT 1 AS probe DELETE FROM #usgi_readonly_probe"),
        ("SELECT ... INTO", "SELECT 1 AS probe INTO #usgi_readonly_probe"),
    ):
        title = f"Data source refuses {label} before sending it"
        remedy = "backend/core/datasource.py run_select() is missing refuse_writes()"
        try:
            source.run_select(sql)
            report.check(False, title, "the statement RAN", remedy)
        except Exception as exc:  # noqa: BLE001
            refused = "could change data" in str(exc)
            report.check(
                refused, title, str(exc),
                "" if refused else f"it reached the database instead - {remedy}",
            )


# --------------------------------------------------------------------------------------
# [2] Database
# --------------------------------------------------------------------------------------

def _query(conn, sql: str, *params):
    cursor = conn.cursor()
    try:
        cursor.execute(sql, *params)
        return cursor.fetchall()
    finally:
        for step in (cursor.close, conn.rollback):
            try:
                step()
            except Exception:  # noqa: BLE001 - must not hide the query's own error
                pass


def _strip_outer_parens(text: str) -> str:
    """'([s].[f]((1)))' -> '[s].[f]((1))' - the form SQL Server stores a predicate in."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for index, char in enumerate(text):
            depth += char == "("
            depth -= char == ")"
            if depth == 0:
                break
        if index != len(text) - 1:
            break
        text = text[1:-1].strip()
    return text


_FUNCTION_CALL = re.compile(r"^(?:\[[^\]]+\]|\w+)\.(?:\[[^\]]+\]|\w+)\s*\(.*\)$", re.S)


def check_database(report: Report, settings) -> None:
    from backend.core.datasource import split_table

    schema, table = split_table(settings.DB_TABLE)
    try:
        import pyodbc

        conn = pyodbc.connect(
            settings.odbc_connection_string, timeout=settings.DB_CONNECT_TIMEOUT, autocommit=False
        )
    except Exception as exc:  # noqa: BLE001
        report.check(False, "Connect as the configured login", str(exc),
                     "Run scripts\\test_db_connection.py")
        return

    try:
        who = _query(
            conn,
            "SELECT ORIGINAL_LOGIN(), USER_NAME(), DB_NAME(), IS_SRVROLEMEMBER('sysadmin'), "
            "IS_ROLEMEMBER('db_owner'), IS_ROLEMEMBER('db_datawriter'), "
            "IS_ROLEMEMBER('db_ddladmin'), CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128))",
        )[0]
        login, user, database, sysadmin, owner, writer, ddladmin, version = who
        print(f"{INFO} Login {login!r} is database user {user!r} in {database!r} "
              f"(SQL Server {version})")

        report.check(sysadmin != 1, "Login is not sysadmin",
                     "sysadmin skips every permission check" if sysadmin == 1 else "",
                     "Give the chatbot a login that is not sysadmin")
        report.check(user != "dbo", "Login does not connect as dbo",
                     "the database owner skips permission checks" if user == "dbo" else "",
                     "Change the database owner, or use a different login")
        for role, member in (("db_owner", owner), ("db_datawriter", writer),
                             ("db_ddladmin", ddladmin)):
            report.check(member != 1, f"Not a member of {role}", "",
                         f"ALTER ROLE [{role}] DROP MEMBER [{user}];")

        perms = _query(
            conn,
            "SELECT HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'SELECT'), "
            "HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'INSERT'), "
            "HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'UPDATE'), "
            "HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'DELETE'), "
            "HAS_PERMS_BY_NAME(t.name, 'OBJECT', 'ALTER'), "
            "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE') "
            "FROM (SELECT QUOTENAME(?) + '.' + QUOTENAME(?) AS name) AS t",
            schema, table,
        )[0]
        can_select, *writes = perms
        report.check(can_select == 1, f"SELECT on {settings.DB_TABLE}",
                     f"HAS_PERMS_BY_NAME = {can_select}",
                     "The chatbot cannot read - Part 1 of the SQL script grants SELECT")
        names = ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE TABLE")
        held = [n for n, v in zip(names, writes) if v == 1]
        unknown = [n for n, v in zip(names, writes) if v is None]
        report.check(not held and not unknown, "No write permissions",
                     f"holds: {held or 'none'}" + (f"; could not check: {unknown}" if unknown else ""),
                     f"Run Part 1 of {SCRIPT}")

        check_security_policy(report, conn, schema, table)
    except Exception as exc:  # noqa: BLE001
        report.check(False, "Database checks completed", str(exc))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def check_security_policy(report: Report, conn, schema: str, table: str) -> None:
    from backend.core.read_only import find_write_operation

    try:
        rows = _query(
            conn,
            "SELECT OBJECT_SCHEMA_NAME(p.object_id) + '.' + p.name, p.is_enabled, "
            "pr.predicate_type_desc, pr.operation_desc, pr.predicate_definition "
            "FROM sys.security_policies AS p "
            "JOIN sys.security_predicates AS pr ON pr.object_id = p.object_id "
            "WHERE pr.target_object_id = OBJECT_ID(QUOTENAME(?) + '.' + QUOTENAME(?))",
            schema, table,
        )
    except Exception as exc:  # noqa: BLE001
        report.check(False, "Row-Level Security policy readable", str(exc),
                     "Row-Level Security needs SQL Server 2016 or later")
        return

    blocks = [r for r in rows if r[1] and str(r[2]).upper() == "BLOCK"]
    report.check(
        bool(rows), "Security policy on the table is visible",
        f"{len(rows)} predicate(s): " + ", ".join(sorted({str(r[0]) for r in rows})) if rows else "",
        f"Run Part 2 of {SCRIPT}. It also grants this login VIEW DEFINITION on the policy's "
        "schema - without that the policy is invisible here even when it exists.",
    )
    if not rows:
        return

    covered = {str(r[3]).upper() for r in blocks}
    missing = [op for op in REQUIRED_BLOCKS if op not in covered]
    report.check(not missing, "Enabled BLOCK predicates for every write",
                 f"missing: {missing}" if missing else ", ".join(REQUIRED_BLOCKS),
                 f"Re-run Part 2 of {SCRIPT}, or ALTER SECURITY POLICY ... WITH (STATE = ON)")

    # A policy for the wrong login name would look perfect above and block nobody, so
    # ask the predicate itself - a SELECT from the function, answered for THIS login.
    for definition in sorted({str(r[4]) for r in blocks}):
        call = _strip_outer_parens(definition)
        title = f"The predicate blocks this login: {call}"
        if not _FUNCTION_CALL.match(call) or find_write_operation(f"SELECT 1 FROM {call}"):
            report.check(False, title, "unexpected predicate shape - not evaluated",
                         "Check the policy by hand in SSMS")
            continue
        try:
            allowed = _query(conn, f"SELECT COUNT(*) FROM {call} AS probe")[0][0]
        except Exception as exc:  # noqa: BLE001
            report.check(False, title, f"could not evaluate: {exc}",
                         f"Part 2 of {SCRIPT} grants SELECT on the predicate function")
            continue
        report.check(
            allowed == 0, title,
            "writes from this login are refused" if allowed == 0
            else "the predicate ALLOWS this login to write",
            f"@login_name in {SCRIPT} must equal DB_USERNAME in .env; fix it and re-run Part 2",
        )


# --------------------------------------------------------------------------------------

def main() -> int:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"CONFIGURATION ERROR:\n{exc}")
        return 2

    report = Report()
    print("=" * 78)
    print(f"  DATA_SOURCE : {settings.DATA_SOURCE}")
    print(f"  Target      : {settings.describe_source()}")
    if not settings.is_local_source:
        print(f"  Login       : {settings.DB_USERNAME}")
    print("=" * 78)

    print("\n[1] App-side guards")
    check_app_guards(report, settings)

    print("\n[2] Database permissions and Row-Level Security")
    if settings.is_local_source:
        print(f"{INFO} Skipped: DATA_SOURCE=local has no database login. Run this on the "
              "machine where DATA_SOURCE=sqlserver.")
    else:
        check_database(report, settings)

    print("\n" + "=" * 78)
    if report.failures:
        print(f"  {len(report.failures)} CHECK(S) FAILED:")
        for failure in report.failures:
            print(f"    - {failure}")
        print("=" * 78)
        return 1
    if settings.is_local_source:
        print("  APP-SIDE CHECKS PASSED. The database checks need DATA_SOURCE=sqlserver.")
    else:
        print("  ALL CHECKS PASSED - the chatbot can read but not change the data.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
