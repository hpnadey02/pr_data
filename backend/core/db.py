"""Backwards-compatibility shim.

All database access now goes through the pluggable data-source layer in
backend/core/datasource.py, which lets the same pipeline run against SQL Server or a
local .xlsx (DATA_SOURCE in .env). The pyodbc implementation lives inside
`SqlServerDataSource`; no direct pyodbc call exists anywhere else.

New code should import from backend.core.datasource. This module remains only so older
imports (`from backend.core.db import run_select`) keep working, and it simply forwards to
whichever data source is active.
"""
from backend.core.datasource import (  # noqa: F401 - re-exported for compatibility
    DatabaseError,
    QueryResult,
    get_datasource,
)


def check_connection() -> tuple[bool, str]:
    return get_datasource().check_connection()


def get_table_columns() -> list[dict]:
    return get_datasource().get_table_columns()


def run_select(sql: str, max_rows: int | None = None) -> QueryResult:
    return get_datasource().run_select(sql, max_rows)
