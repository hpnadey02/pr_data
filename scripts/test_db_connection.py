"""Standalone connectivity check for the ACTIVE data source, independent of the
backend/agents stack, so data issues can be diagnosed without Ollama/ChromaDB running.

Works in both modes and prints which one is active:

    DATA_SOURCE=sqlserver  -> connects over ODBC and reads INFORMATION_SCHEMA
    DATA_SOURCE=local      -> loads the .xlsx into DuckDB and describes the sheet

Usage:
    python scripts/test_db_connection.py
    python scripts/test_db_connection.py --all-columns
    python scripts/test_db_connection.py --sql "SELECT TOP 5 [BRANCH_NAME] FROM dbo.May_2"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ConfigurationError, get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-columns", action="store_true", help="print every column")
    parser.add_argument("--sql", help="run one SELECT and print the result")
    args = parser.parse_args()

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"CONFIGURATION ERROR:\n{exc}")
        return 2

    from backend.core.datasource import get_datasource  # noqa: E402 - needs valid settings

    print("=" * 72)
    print(f"  DATA_SOURCE : {settings.DATA_SOURCE}")
    if settings.is_local_source:
        print(f"  File        : {settings.LOCAL_DATA_PATH}")
        if settings.is_local_csv:
            print("  Format      : CSV (read by DuckDB directly)")
        else:
            print(f"  Sheet       : {settings.LOCAL_SHEET_NAME or '(first sheet)'}")
            print("  Format      : Excel (read by pandas/openpyxl)")
        print("  Driver      : DuckDB (no ODBC driver required)")
    else:
        print(f"  Server      : {settings.DB_SERVER}/{settings.DB_DATABASE}")
        print(f"  User        : {settings.DB_USERNAME}")
        print(f"  Driver      : {settings.DB_DRIVER}")
    print(f"  Table       : {settings.DB_TABLE}")
    print(f"  LLM_PROVIDER: {settings.LLM_PROVIDER}  ({settings.describe_llm()})")
    print("=" * 72)

    try:
        source = get_datasource()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to initialise the data source: {exc}")
        return 1

    print("\nChecking connection ...")
    ok, message = source.check_connection()
    if not ok:
        print(f"FAILED: {message}")
        if not settings.is_local_source:
            print("\nHint: to work offline against the local workbook, set DATA_SOURCE=local in .env.")
        return 1
    print(f"OK: {message}")

    print(f"\nIntrospecting columns for {settings.DB_TABLE} ...")
    try:
        columns = source.get_table_columns()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to read columns: {exc}")
        return 1

    print(f"OK: found {len(columns)} columns.")
    shown = columns if args.all_columns else columns[:20]
    for column in shown:
        print(f"  - {column['column_name']} ({column['data_type']})")
    if len(columns) > len(shown):
        print(f"  ... and {len(columns) - len(shown)} more (use --all-columns).")

    if args.sql:
        print(f"\nRunning: {args.sql}")
        try:
            result = source.run_select(args.sql)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED: {exc}")
            return 1
        print(f"OK: {result.row_count} row(s)")
        print(result.dataframe.to_string(max_rows=20))

    return 0


if __name__ == "__main__":
    sys.exit(main())
