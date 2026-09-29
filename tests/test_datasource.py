"""Tests for the pluggable data-source layer.

No live SQL Server and no workbook are needed: these cover the parts that must hold for
BOTH backends - configuration validation, schema parity, and the factory contract.
"""
import pytest

from backend.core.datasource import (
    DataSource,
    LocalFileDataSource,
    SqlServerDataSource,
    describe_odbc_error,
    duckdb_type_to_sql,
    pandas_dtype_to_sql,
    split_table,
)
from config.settings import ALLOWED_DATA_SOURCES, Settings


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def test_only_two_modes_are_allowed():
    assert set(ALLOWED_DATA_SOURCES) == {"sqlserver", "local"}


def test_invalid_data_source_fails_fast_with_a_clear_message():
    with pytest.raises(Exception) as info:
        Settings(DATA_SOURCE="mysql", _env_file=None)
    assert "DATA_SOURCE" in str(info.value)


@pytest.mark.parametrize("value", ["LOCAL", " local ", '"local"'])
def test_data_source_value_is_normalised(value):
    settings = Settings(DATA_SOURCE=value, LOCAL_DATA_PATH="x.xlsx", _env_file=None)
    assert settings.DATA_SOURCE == "local"


def test_sqlserver_mode_requires_credentials():
    with pytest.raises(Exception) as info:
        Settings(DATA_SOURCE="sqlserver", DB_SERVER="", DB_DATABASE="", _env_file=None)
    assert "DB_SERVER" in str(info.value)


def test_local_mode_does_not_require_sqlserver_credentials():
    """Local mode must work on a machine with no ODBC driver and no DB credentials."""
    settings = Settings(
        DATA_SOURCE="local", LOCAL_DATA_PATH="D:/data/may_2.xlsx",
        DB_SERVER="", DB_USERNAME="", DB_PASSWORD="", _env_file=None,
    )
    assert settings.is_local_source
    assert "may_2.xlsx" in settings.describe_source()


def test_local_mode_requires_a_path():
    with pytest.raises(Exception) as info:
        Settings(DATA_SOURCE="local", LOCAL_DATA_PATH="", _env_file=None)
    assert "LOCAL_DATA_PATH" in str(info.value)


@pytest.mark.parametrize(
    "path", ["data.csv", "D:/x/dummy_insurance_data.csv", "book.xlsx", "book.xlsm"]
)
def test_local_mode_accepts_csv_and_excel(path):
    settings = Settings(DATA_SOURCE="local", LOCAL_DATA_PATH=path, _env_file=None)
    assert settings.is_local_source
    assert settings.is_local_csv == path.lower().endswith(".csv")


def test_local_mode_rejects_an_unsupported_extension():
    """A .parquet/.json path would load as an empty table and silently answer nothing."""
    with pytest.raises(Exception) as info:
        Settings(DATA_SOURCE="local", LOCAL_DATA_PATH="data.parquet", _env_file=None)
    assert "unsupported extension" in str(info.value)


def test_csv_source_is_described_without_a_sheet():
    settings = Settings(
        DATA_SOURCE="local", LOCAL_DATA_PATH="D:/x/dummy_insurance_data.csv", _env_file=None
    )
    described = settings.describe_source()
    assert "csv" in described
    assert "sheet" not in described.lower()


# --------------------------------------------------------------------------------------
# Interface parity
# --------------------------------------------------------------------------------------

def test_both_backends_implement_the_same_interface():
    for backend in (SqlServerDataSource, LocalFileDataSource):
        assert issubclass(backend, DataSource)
        for method in ("check_connection", "get_table_columns", "run_select"):
            assert callable(getattr(backend, method))
            assert getattr(backend, method) is not getattr(DataSource, method)


def test_datasource_is_abstract():
    with pytest.raises(TypeError):
        DataSource()  # type: ignore[abstract]


# --------------------------------------------------------------------------------------
# Schema parity - both backends must describe a column identically
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "dtype, expected",
    [
        ("object", "varchar"),
        ("float64", "float"),
        ("int64", "int"),
        ("datetime64[ns]", "datetime"),
        ("bool", "bit"),
        ("category", "varchar"),
        ("string", "varchar"),
    ],
)
def test_pandas_dtype_mapping(dtype, expected):
    assert pandas_dtype_to_sql(dtype) == expected


@pytest.mark.parametrize(
    "duck_type, expected",
    [
        ("VARCHAR", "varchar"),
        ("BIGINT", "bigint"),
        ("INTEGER", "int"),
        ("DOUBLE", "float"),
        ("TIMESTAMP", "datetime"),
        ("DATE", "date"),
        ("BOOLEAN", "bit"),
        ("DECIMAL(18,2)", "decimal"),
        ("SOMETHING_UNKNOWN", "varchar"),
    ],
)
def test_duckdb_type_mapping(duck_type, expected):
    assert duckdb_type_to_sql(duck_type) == expected


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "qualified, expected",
    [
        ("dbo.May_2", ("dbo", "May_2")),
        ("May_2", ("dbo", "May_2")),
        ("[dbo].[May_2]", ("dbo", "May_2")),
        ("USGI_CHAT.dbo.May_2", ("dbo", "May_2")),
    ],
)
def test_split_table(qualified, expected):
    assert split_table(qualified) == expected


def test_odbc_error_reports_the_real_sqlstate():
    """The old code labelled EVERY failure "ODBC driver not found", sending users to
    chase a driver install for what were network, login or timeout failures."""

    class FakeOperationalError(Exception):
        pass

    exc = FakeOperationalError(
        "08001",
        "[08001] [Microsoft][ODBC Driver 17 for SQL Server]Named Pipes Provider: "
        "Could not open a connection to SQL Server [53].",
    )
    described = describe_odbc_error(exc)
    assert "08001" in described
    assert "Named Pipes Provider" in described
    assert "not found" not in described.lower()


def test_odbc_error_without_sqlstate_still_reports_something_useful():
    described = describe_odbc_error(ValueError("something odd happened"))
    assert "something odd happened" in described
    assert "ValueError" in described
