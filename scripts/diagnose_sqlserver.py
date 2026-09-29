"""End-to-end health check for the ACTIVE data source, focused on the chart/insight chain.

Answers one question: after data is fetched, will a chart and a grounded insight actually
be produced? That chain broke on SQL Server while working on local files, because pyodbc
returns Decimal/date objects that pandas leaves as OBJECT columns - so every dtype-driven
decision downstream silently gave up.

Run it on whichever machine has the database:

    python scripts\\diagnose_sqlserver.py
    python scripts\\diagnose_sqlserver.py --question "High-performing branch region-wise."
    python scripts\\diagnose_sqlserver.py --sql "SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM dbo.May_2 GROUP BY [BRANCH_NAME]"

Checks 1-7 need only the data source. `--question` additionally runs the full LangGraph
pipeline and therefore needs the LLM provider up.

Exit code 0 = every check passed.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from config.settings import ConfigurationError, get_settings  # noqa: E402

PASS = "  [PASS]"
FAIL = "  [FAIL]"


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


def _build_probe_sql(settings, registry) -> str:
    """A representative `GROUP BY` aggregate built from the LIVE schema.

    Using the real schema rather than hard-coded names means this works even if the table
    changes, and it exercises the same shape the SQL agent generates for a "branch wise
    business" question.
    """
    measure = None
    for candidate in ("GROSS_PREMIUM", "USGI_GROSS_PREMIUM", "NET_PREMIUM"):
        measure = registry.resolve(candidate, allow_fuzzy=False)
        if measure:
            break
    measure = measure or (registry.measures()[0] if registry.measures() else None)

    dimension = None
    for candidate in ("BRANCH_NAME", "STATE", "LINE_OF_BUSINESS", "PRODUCT_NAME"):
        dimension = registry.resolve(candidate, allow_fuzzy=False)
        if dimension:
            break
    if not dimension:
        dimensions = [c.name for c in registry.columns if c.category == "dimension"]
        dimension = dimensions[0] if dimensions else None

    if not measure or not dimension:
        raise SystemExit(
            "Could not find a measure and a dimension in the live schema. "
            "Run `python scripts\\build_column_aliases.py --check` first."
        )

    return (
        f"SELECT TOP 10 [{dimension}], SUM([{measure}]) AS total_{measure.lower()} "
        f"FROM {settings.DB_TABLE} GROUP BY [{dimension}] "
        f"ORDER BY SUM([{measure}]) DESC"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", help="run this SELECT instead of the generated probe")
    parser.add_argument("--question", help="also run the full agent pipeline on this question")
    args = parser.parse_args()

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"CONFIGURATION ERROR:\n{exc}")
        return 2

    from backend.agents.answer_builder import collect_allowed_numbers, unsupported_numbers  # noqa: E402
    from backend.agents.chart_agent import chart_agent_node  # noqa: E402
    from backend.agents.df_utils import numeric_columns, pick_measure  # noqa: E402
    from backend.agents.insight_agent import compute_stats  # noqa: E402
    from backend.core.column_registry import get_registry  # noqa: E402
    from backend.core.datasource import get_datasource  # noqa: E402

    report = Report()

    print("=" * 78)
    print(f"  DATA_SOURCE  : {settings.DATA_SOURCE}")
    print(f"  Target       : {settings.describe_source()}")
    print(f"  LLM_PROVIDER : {settings.LLM_PROVIDER}")
    print("=" * 78)

    # -- 1. connectivity ---------------------------------------------------------------
    print("\n[1] Data source")
    try:
        source = get_datasource()
        ok, message = source.check_connection()
    except Exception as exc:  # noqa: BLE001
        ok, message = False, str(exc)
    if not report.check(ok, "Connection", message, "Run scripts\\test_db_connection.py"):
        return 1

    registry = get_registry(refresh=True)
    report.check(
        bool(registry.columns),
        "Live schema readable",
        f"{len(registry.columns)} columns, {registry.shortcut_count} shortcuts",
        "Check DB_TABLE in .env",
    )

    # -- 2. fetch ----------------------------------------------------------------------
    sql = args.sql or _build_probe_sql(settings, registry)
    print(f"\n[2] Probe query\n         {sql}")
    try:
        result = source.run_select(sql)
    except Exception as exc:  # noqa: BLE001
        report.check(False, "Query executed", str(exc))
        return 1
    frame = result.dataframe
    report.check(result.row_count > 0, "Rows returned", f"{result.row_count} rows")
    if frame is None or frame.empty:
        print("\nNo rows - cannot check the chart/insight chain. Try a different --sql.")
        return 1

    # -- 3. dtypes: the actual root cause ----------------------------------------------
    print("\n[3] Column dtypes after normalisation")
    for column in frame.columns:
        sample = frame[column].dropna()
        example = sample.iloc[0] if not sample.empty else None
        print(f"         {column:<40} {str(frame[column].dtype):<16} e.g. {example!r}")

    leftover_decimals = [
        c for c in frame.columns
        if frame[c].dtype == object
        and not frame[c].dropna().empty
        and type(frame[c].dropna().iloc[0]).__name__ == "Decimal"
    ]
    report.check(
        not leftover_decimals,
        "No Decimal objects left in the frame",
        f"offending columns: {leftover_decimals}" if leftover_decimals else "",
        "normalize_result_frame() is not being applied in datasource.run_select",
    )

    report.check(
        bool(str(c).strip() for c in frame.columns) and all(str(c).strip() for c in frame.columns),
        "Every column has a usable name",
        f"columns: {list(frame.columns)}",
        "repair_column_names() is not being applied",
    )

    # -- 4. measure detection ----------------------------------------------------------
    print("\n[4] Chart/insight prerequisites")
    numerics = numeric_columns(frame)
    report.check(
        bool(numerics),
        "At least one numeric column detected",
        f"numeric: {numerics}",
        "The measure is still an object column - this is the SQL Server dtype bug",
    )
    measure = pick_measure(frame, numerics)
    report.check(
        measure is not None,
        "A measure was selected",
        f"measure = {measure}",
        "Every numeric column looks like an identifier; alias the aggregate in the SQL",
    )

    # -- 5. insight statistics ---------------------------------------------------------
    stats = compute_stats(frame, args.question or "diagnostic probe")
    report.check(
        "total" in stats,
        "Insight statistics contain a total",
        f"total = {stats.get('total')}, dimension = {stats.get('dimension')}",
        "compute_stats found no measure, so the model would have nothing to summarise",
    )
    if stats.get("top_entities"):
        top = stats["top_entities"][0]
        print(f"         top entity: {top['name']} = {top['value']:,}")

    # -- 6. hallucination guard ---------------------------------------------------------
    allowed = collect_allowed_numbers(stats, frame)
    if "total" in stats:
        spoken = f"The total is {stats['total']:,.2f}."
        report.check(
            unsupported_numbers(spoken, allowed) == [],
            "Real figures pass the hallucination guard",
            spoken,
            "The narrative would be rejected and replaced by a bare template",
        )

    # -- 7. the chart -------------------------------------------------------------------
    chart_state = {
        "request_id": "diagnose",
        "dataframe": frame,
        "rewritten_question": args.question or "diagnostic probe",
        "route": "ranking",
        "filters": {},
        "sub_questions": [],
        "retrieved_columns": [],
        "warnings": [],
        "timings_ms": {},
    }
    chart_result = chart_agent_node(chart_state)
    report.check(
        bool(chart_result.get("charts")),
        "A chart was generated",
        f"chart_type = {chart_result.get('chart_type')}",
        "; ".join(chart_result.get("warnings", [])) or "chart_agent returned nothing",
    )

    # -- 8. optional full pipeline ------------------------------------------------------
    if args.question:
        print(f"\n[8] Full agent pipeline: {args.question!r}")
        print("         (needs the LLM provider running - this can take a minute)")
        from backend.agents.graph import get_graph  # noqa: E402

        final = get_graph().invoke(
            {
                "request_id": "diagnose",
                "raw_question": args.question,
                "chat_history": [],
                "sql_attempts": 0,
                "warnings": [],
                "timings_ms": {},
            }
        )
        report.check(
            final.get("status") != "error",
            "Pipeline completed",
            final.get("error_message") or "",
        )
        print(f"\n         SQL:\n{final.get('sql_query', '(none)')}\n")
        report.check(bool(final.get("charts")), "Pipeline produced a chart")
        report.check(
            final.get("answer_mode") in ("direct", "narrative", "template"),
            "Pipeline produced an insight",
            f"answer_mode = {final.get('answer_mode')}",
        )
        print(f"\n         Insight:\n{final.get('insight', '(none)')}\n")
        for warning in final.get("warnings", []):
            print(f"         warning: {warning}")

    # -- verdict -------------------------------------------------------------------------
    print("\n" + "=" * 78)
    if report.failures:
        print(f"  {len(report.failures)} CHECK(S) FAILED:")
        for failure in report.failures:
            print(f"    - {failure}")
        print("=" * 78)
        return 1
    print("  ALL CHECKS PASSED - charts and grounded insights will be generated.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
