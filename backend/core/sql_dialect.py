"""T-SQL -> DuckDB translation, used ONLY by LocalFileDataSource.

The agent pipeline always generates Microsoft T-SQL (so behaviour is identical whichever
data source is active). When DATA_SOURCE=local, this module rewrites that T-SQL into the
DuckDB dialect immediately before execution.

Design rules:
  * String literals are never rewritten. The tokenizer walks the statement and only
    transforms text outside single-quoted literals, so a value like 'TOP 10 [Branch]'
    passes through untouched.
  * Anything that cannot be faithfully translated raises SQLDialectError naming the exact
    unsupported construct. Silently returning a query that means something *different*
    would hand the user wrong numbers, which is worse than an error.

Supported translations
----------------------
    SELECT TOP n / TOP (n)     ->  ... LIMIT n
    GETDATE() / SYSDATETIME()  ->  CURRENT_DATE / CURRENT_TIMESTAMP
    ISNULL(a, b)               ->  COALESCE(a, b)
    [ident]                    ->  "ident"
    dbo.table                  ->  table
    FORMAT(col, 'yyyy-MM')     ->  strftime(col, '%Y-%m')
    DATEADD(part, n, d)        ->  (d + INTERVAL (n) part)
    DATEDIFF(part, a, b)       ->  date_diff('part', a, b)
    DATEPART(part, d)          ->  T-SQL-compatible extract expression
    LEN(x)                     ->  LENGTH(x)
    CHARINDEX(needle, hay)     ->  strpos(hay, needle)
    N'text'                    ->  'text'
"""
from __future__ import annotations

import re


class SQLDialectError(Exception):
    """Raised when a T-SQL construct has no faithful DuckDB equivalent."""


# --------------------------------------------------------------------------------------
# Literal-safe tokenizer
# --------------------------------------------------------------------------------------

def _split_literals(sql: str) -> list[tuple[bool, str]]:
    """Split SQL into (is_literal, text) segments so rewrites never touch string values.

    Handles the T-SQL escaped quote ('' inside a literal) and the N'...' unicode prefix.
    """
    segments: list[tuple[bool, str]] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]
        if char == "'":
            segments.append((False, "".join(buffer)))
            buffer = []
            literal = ["'"]
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        literal.append("''")
                        index += 2
                        continue
                    literal.append("'")
                    index += 1
                    break
                literal.append(sql[index])
                index += 1
            segments.append((True, "".join(literal)))
            continue
        buffer.append(char)
        index += 1

    segments.append((False, "".join(buffer)))
    return segments


def _map_code(sql: str, transform) -> str:
    """Apply `transform` to every non-literal segment of `sql`."""
    return "".join(text if is_literal else transform(text) for is_literal, text in _split_literals(sql))


def _code_only(sql: str) -> str:
    """The statement with all string literals blanked out - safe to scan for keywords."""
    return "".join("" if is_literal else text for is_literal, text in _split_literals(sql))


def _strip_unicode_prefix(sql: str) -> str:
    """N'text' -> 'text'. DuckDB has no N-prefix; every literal is already UTF-8.

    The prefix sits at the END of a code segment (the quote opens the next segment), so
    it cannot be matched by a regex applied within a single segment.
    """
    segments = _split_literals(sql)
    out: list[str] = []
    for index, (is_literal, text) in enumerate(segments):
        if not is_literal and text.endswith(("N", "n")):
            following = segments[index + 1] if index + 1 < len(segments) else None
            preceding = text[-2] if len(text) >= 2 else " "
            if following and following[0] and not (preceding.isalnum() or preceding == "_"):
                out.append(text[:-1])
                continue
        out.append(text)
    return "".join(out)


# --------------------------------------------------------------------------------------
# Unsupported-construct detection
# --------------------------------------------------------------------------------------

_UNSUPPORTED: tuple[tuple[str, str], ...] = (
    (r"\bPIVOT\b", "PIVOT"),
    (r"\bUNPIVOT\b", "UNPIVOT"),
    (r"\b(?:CROSS|OUTER)\s+APPLY\b", "CROSS/OUTER APPLY"),
    (r"\bTOP\s*\(?\s*\d+\s*\)?\s+PERCENT\b", "TOP n PERCENT"),
    (r"\bWITH\s+TIES\b", "WITH TIES"),
    (r"\bFOR\s+XML\b", "FOR XML"),
    (r"\bFOR\s+JSON\b", "FOR JSON"),
    (r"\bAT\s+TIME\s+ZONE\b", "AT TIME ZONE"),
    (r"\bSTUFF\s*\(", "STUFF()"),
    (r"\bCONVERT\s*\(", "CONVERT()"),
    (r"\bIIF\s*\(", "IIF()"),
    (r"\bCHOOSE\s*\(", "CHOOSE()"),
    (r"\bEOMONTH\s*\(", "EOMONTH()"),
    (r"\bDATENAME\s*\(", "DATENAME()"),
    (r"#\w+", "temporary table (#temp)"),
    (r"@\w+", "T-SQL variable (@var)"),
    (r"\bOPENQUERY\b", "OPENQUERY"),
    (r"\bOPENROWSET\b", "OPENROWSET"),
)


def _reject_unsupported(sql: str) -> None:
    code = _code_only(sql)
    for pattern, label in _UNSUPPORTED:
        if re.search(pattern, code, re.IGNORECASE):
            raise SQLDialectError(
                f"The generated T-SQL uses {label}, which has no faithful DuckDB "
                f"equivalent. Rephrase the question, or switch DATA_SOURCE=sqlserver."
            )


# --------------------------------------------------------------------------------------
# Individual rewrites
# --------------------------------------------------------------------------------------

_TOP_RE = re.compile(r"(?i)\bSELECT\s+(DISTINCT\s+)?TOP\s*(?:\(\s*(\d+)\s*\)|(\d+))\s+")
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
_ISNULL_RE = re.compile(r"(?i)\bISNULL\s*\(")
_LEN_RE = re.compile(r"(?i)\bLEN\s*\(")
_GETDATE_RE = re.compile(r"(?i)\b(?:GETDATE|GETUTCDATE)\s*\(\s*\)")
_SYSDATETIME_RE = re.compile(r"(?i)\bSYSDATETIME\s*\(\s*\)")
_SCHEMA_RE = re.compile(r"(?i)(?<![\w\.\"])(?:\[?dbo\]?)\s*\.\s*")

_DATE_PARTS = {
    "year": "year", "yy": "year", "yyyy": "year",
    "quarter": "quarter", "qq": "quarter", "q": "quarter",
    "month": "month", "mm": "month", "m": "month",
    "day": "day", "dd": "day", "d": "day",
    "week": "week", "wk": "week", "ww": "week",
    "hour": "hour", "hh": "hour",
    "minute": "minute", "mi": "minute", "n": "minute",
    "second": "second", "ss": "second", "s": "second",
    "dayofyear": "dayofyear", "dy": "dayofyear",
    "weekday": "weekday", "dw": "weekday",
}

_FORMAT_TOKENS: tuple[tuple[str, str], ...] = (
    ("yyyy", "%Y"), ("yy", "%y"),
    ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"),
    ("dddd", "%A"), ("ddd", "%a"), ("dd", "%d"),
    ("HH", "%H"), ("hh", "%I"),
    ("mm", "%M"), ("ss", "%S"), ("tt", "%p"),
)


def _find_matching_paren(text: str, open_index: int) -> int:
    """Index of the ')' matching the '(' at `open_index`, literal-aware. -1 if unbalanced."""
    depth = 0
    index = open_index
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            index += 1
            while index < length:
                if text[index] == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        index += 2
                        continue
                    break
                index += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _split_arguments(text: str) -> list[str]:
    """Split a function argument list on top-level commas, literal- and paren-aware."""
    args: list[str] = []
    depth = 0
    current: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            current.append(char)
            index += 1
            while index < length:
                current.append(text[index])
                if text[index] == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        current.append(text[index + 1])
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    args.append("".join(current).strip())
    return [a for a in args if a != ""]


def _rewrite_function(sql: str, name: str, builder) -> str:
    """Rewrite every `name(...)` call using `builder(args) -> str`, innermost-last.

    Scanning right-to-left keeps nested calls (DATEADD inside DATEADD) correct because an
    inner call's offsets are unaffected by rewriting an outer one that starts later.
    """
    pattern = re.compile(rf"(?i)(?<![\w.]){re.escape(name)}\s*\(")
    while True:
        matches = list(pattern.finditer(sql))
        if not matches:
            return sql
        match = matches[-1]
        open_index = sql.index("(", match.end() - 1)
        close_index = _find_matching_paren(sql, open_index)
        if close_index == -1:
            raise SQLDialectError(f"Unbalanced parentheses in {name}(...).")
        args = _split_arguments(sql[open_index + 1:close_index])
        replacement = builder(args)
        sql = sql[:match.start()] + replacement + sql[close_index + 1:]


def _normalize_part(raw: str) -> str:
    part = raw.strip().strip("'\"").lower()
    if part not in _DATE_PARTS:
        raise SQLDialectError(f"Unsupported date part '{raw.strip()}' in a date function.")
    return _DATE_PARTS[part]


def _build_dateadd(args: list[str]) -> str:
    if len(args) != 3:
        raise SQLDialectError("DATEADD() requires exactly three arguments.")
    part = _normalize_part(args[0])
    if part == "weekday":
        part = "day"
    return f"(({args[2]}) + (({args[1]}) * INTERVAL 1 {part.upper()}))"


def _build_datediff(args: list[str]) -> str:
    if len(args) != 3:
        raise SQLDialectError("DATEDIFF() requires exactly three arguments.")
    part = _normalize_part(args[0])
    if part == "weekday":
        part = "day"
    return f"date_diff('{part}', {args[1]}, {args[2]})"


def _build_datepart(args: list[str]) -> str:
    if len(args) != 2:
        raise SQLDialectError("DATEPART() requires exactly two arguments.")
    part = _normalize_part(args[0])
    if part == "weekday":
        # T-SQL DATEPART(weekday, d) is 1=Sunday..7=Saturday (with the default DATEFIRST).
        # DuckDB dayofweek() is 0=Sunday..6=Saturday, so add one to keep parity.
        return f"(dayofweek({args[1]}) + 1)"
    if part == "dayofyear":
        return f"dayofyear({args[1]})"
    return f"EXTRACT({part.upper()} FROM {args[1]})"


def _build_format(args: list[str]) -> str:
    if len(args) < 2:
        raise SQLDialectError("FORMAT() requires a value and a format string.")
    pattern = args[1].strip()
    if not (pattern.startswith("'") and pattern.endswith("'")):
        raise SQLDialectError("FORMAT() is only supported with a literal format string.")
    body = pattern[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        for token, replacement in _FORMAT_TOKENS:
            if body.startswith(token, index):
                out.append(replacement)
                index += len(token)
                break
        else:
            out.append(body[index])
            index += 1
    return f"strftime({args[0]}, '{''.join(out)}')"


def _build_charindex(args: list[str]) -> str:
    if len(args) < 2:
        raise SQLDialectError("CHARINDEX() requires at least two arguments.")
    if len(args) > 2:
        raise SQLDialectError("CHARINDEX() with a start position is not supported.")
    return f"strpos({args[1]}, {args[0]})"


def _strip_top(sql: str) -> tuple[str, int | None]:
    """Remove `TOP n` from the outermost SELECT and return the row limit it expressed."""
    match = _TOP_RE.search(sql)
    if not match:
        return sql, None
    limit = int(match.group(2) or match.group(3))
    replacement = f"SELECT {match.group(1) or ''}"
    return sql[:match.start()] + replacement + sql[match.end():], limit


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------

def translate(sql: str) -> str:
    """Translate one T-SQL SELECT statement into DuckDB SQL.

    Raises SQLDialectError naming the construct when faithful translation is impossible.
    """
    if not str(sql or "").strip():
        raise SQLDialectError("Cannot translate an empty SQL statement.")

    _reject_unsupported(sql)

    result = sql.strip().rstrip(";")

    # N'literal' -> 'literal'
    result = _strip_unicode_prefix(result)

    # TOP n -> LIMIT n. Done before other rewrites so the SELECT prefix is still intact.
    result, limit = _strip_top(result)

    # Nullary/simple scalar swaps.
    def _scalars(code: str) -> str:
        code = _GETDATE_RE.sub("CURRENT_DATE", code)
        code = _SYSDATETIME_RE.sub("CURRENT_TIMESTAMP", code)
        code = _ISNULL_RE.sub("COALESCE(", code)
        code = _LEN_RE.sub("LENGTH(", code)
        return code

    result = _map_code(result, _scalars)

    # Function rewrites that need argument parsing.
    result = _rewrite_function(result, "DATEADD", _build_dateadd)
    result = _rewrite_function(result, "DATEDIFF", _build_datediff)
    result = _rewrite_function(result, "DATEPART", _build_datepart)
    result = _rewrite_function(result, "FORMAT", _build_format)
    result = _rewrite_function(result, "CHARINDEX", _build_charindex)

    # dbo.May_2 -> May_2, then [ident] -> "ident".
    result = _map_code(result, lambda code: _SCHEMA_RE.sub("", code))
    result = _map_code(
        result,
        lambda code: _BRACKET_RE.sub(lambda m: '"' + m.group(1).replace('"', '""') + '"', code),
    )

    if limit is not None and not re.search(r"(?i)\bLIMIT\s+\d+\s*$", result.strip()):
        result = result.rstrip().rstrip(";") + f"\nLIMIT {limit}"

    return result.strip()
