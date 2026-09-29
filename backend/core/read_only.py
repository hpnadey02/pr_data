"""Single answer to "could this SQL change anything?" - the app only ever reads.

Used twice: by backend/agents/sql_guard.py on generated SQL, and by both data sources'
run_select() for callers that never pass through the guard (date_windows, the scripts'
--sql). The database login must be read-only as well (scripts/sqlserver_readonly.sql);
this is one layer of three, not the only one.

The whole RAW text is scanned, not a parse of it:

  * T-SQL stacks statements WITHOUT semicolons. `SELECT ... FROM dbo.May_2 DELETE FROM
    dbo.May_2` is two statements, so a write can begin anywhere in the text.
  * Skipping string literals and comments needs a tokenizer that agrees with SQL Server on
    every edge case (nested /* */, '' escapes, ]] inside brackets). One disagreement and a
    write hides inside what we took for a literal. A raw scan cannot be fooled that way;
    the price is that a forbidden word inside a comment or a string literal is refused
    too - the safe side.

Words that are real data values - 'CALL CENTER' is in User_Name - are only statements in
DuckDB, where a statement can start nowhere but at the beginning or after a `;`. Those are
matched only there, so filtering on such a value still works.
"""
from __future__ import annotations

import re

# ASCII boundaries, deliberately lopsided. A digit on the LEFT must not protect a keyword:
# T-SQL lexes `SELECT 1DELETE FROM t` as `SELECT 1` then `DELETE FROM t`. Anything
# identifier-like on the right does, because UPDATED_ON is a single identifier.
_LEFT = r"(?<![A-Za-z_])"
_RIGHT = r"(?![A-Za-z0-9_])"
# Whitespace or comments between the words of a phrase. Nested comments are not followed;
# the only phrase that could hide behind one (NEXT VALUE FOR) also needs UPDATE on a
# sequence, which the read-only login does not hold.
_GAP = r"(?:\s|/\*.*?\*/|--[^\r\n]*)+"

_REASONS: dict[str, str] = {
    "INSERT": "adds rows",
    "UPDATE": "changes rows",
    "DELETE": "removes rows",
    "MERGE": "inserts, updates or deletes rows",
    "TRUNCATE": "empties a table",
    "INTO": "SELECT ... INTO creates a table",
    "UPDATETEXT": "changes text data in place",
    "WRITETEXT": "overwrites text data",
    "BULK INSERT": "loads rows from a file",
    "DROP": "deletes a database object",
    "ALTER": "changes a database object",
    "CREATE": "creates a database object",
    "TRIGGER": "ENABLE/DISABLE TRIGGER changes what happens on a write",
    "GRANT": "changes permissions",
    "REVOKE": "changes permissions",
    "DENY": "changes permissions",
    "SETUSER": "switches the database user",
    "EXEC": "runs a stored procedure or dynamic SQL, whose effects cannot be checked",
    "EXECUTE": "runs a stored procedure or dynamic SQL, whose effects cannot be checked",
    "BACKUP": "writes a backup",
    "RESTORE": "overwrites a database",
    "SHUTDOWN": "stops the server",
    "DBCC": "runs a database console command",
    "KILL": "ends another session",
    "RECONFIGURE": "applies server configuration changes",
    "CHECKPOINT": "forces a write to disk",
    "WAITFOR": "holds the connection open, a denial-of-service tool",
    "RECEIVE": "removes messages from a Service Broker queue",
    "OPENROWSET": "reaches files or servers outside the table",
    "OPENQUERY": "runs a query on a linked server",
    "OPENDATASOURCE": "reaches servers outside the table",
    "OPENXML": "depends on sp_xml_preparedocument",
    "UPDLOCK": "a locking hint that blocks writers, such as the job that loads the table",
    "XLOCK": "a locking hint that blocks writers, such as the job that loads the table",
    "TABLOCK": "a locking hint that blocks writers, such as the job that loads the table",
    "TABLOCKX": "a locking hint that blocks writers, such as the job that loads the table",
    "HOLDLOCK": "a locking hint that blocks writers, such as the job that loads the table",
    "NEXT VALUE FOR": "advances a sequence",
    # DuckDB statements (local mode). Matched only where a statement starts.
    "COPY": "writes a file",
    "ATTACH": "opens another database file for writing",
    "DETACH": "detaches a database",
    "PRAGMA": "changes engine settings",
    "SET": "changes session settings",
    "RESET": "changes session settings",
    "INSTALL": "downloads an extension",
    "LOAD": "loads an extension",
    "FORCE": "forces an install or checkpoint",
    "EXPORT": "writes the database out to files",
    "IMPORT": "loads a database from files",
    "CALL": "runs a procedure",
    "VACUUM": "rewrites storage",
    "USE": "switches database",
}

_ANYWHERE = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "INTO", "UPDATETEXT", "WRITETEXT",
    "DROP", "ALTER", "CREATE", "TRIGGER", "GRANT", "REVOKE", "DENY", "SETUSER",
    "EXEC", "EXECUTE", "BACKUP", "RESTORE", "SHUTDOWN", "DBCC", "KILL", "RECONFIGURE",
    "CHECKPOINT", "WAITFOR", "RECEIVE", "OPENROWSET", "OPENQUERY", "OPENDATASOURCE",
    "OPENXML", "UPDLOCK", "XLOCK", "TABLOCK", "TABLOCKX", "HOLDLOCK",
)
_ANYWHERE_RE = re.compile(
    _LEFT
    + r"((?:xp|sp)_[A-Za-z0-9_]*|"
    + "|".join(sorted(_ANYWHERE, key=len, reverse=True))
    + r")"
    + _RIGHT,
    re.IGNORECASE,
)
# BULK alone is a cargo term; it only writes as BULK INSERT or OPENROWSET(BULK ...), and
# OPENROWSET is caught on its own.
_PHRASES = (
    ("BULK INSERT", re.compile(_LEFT + r"BULK" + _GAP + r"INSERT" + _RIGHT, re.I | re.S)),
    (
        "NEXT VALUE FOR",
        re.compile(_LEFT + r"NEXT" + _GAP + r"VALUE" + _GAP + r"FOR" + _RIGHT, re.I | re.S),
    ),
    # The ODBC driver turns {call proc} into a procedure call before SQL Server sees it.
    ("CALL", re.compile(r"\{\s*(?:\?\s*=\s*)?CALL" + _RIGHT, re.I)),
)

_READ_STARTS = frozenset({"SELECT", "WITH"})
_WORD_START = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def find_write_operation(sql: str) -> str | None:
    """The first construct in `sql` that could change data or the server, upper-case.

    None means the text is a plain read. The result names the construct - "INTO",
    "DELETE", "SP_EXECUTESQL", "COPY" - so the caller can say exactly what was refused.
    """
    text = str(sql or "")
    if not text.strip():
        return None

    hits: list[tuple[int, str]] = []
    match = _ANYWHERE_RE.search(text)
    if match:
        hits.append((match.start(), match.group(1).upper()))
    for name, pattern in _PHRASES:
        match = pattern.search(text)
        if match:
            hits.append((match.start(), name))
    if hits:
        return min(hits)[1]

    for start in _statement_starts(text):
        leader = _leading_token(text, start)
        if leader is not None and leader not in _READ_STARTS:
            return leader
    return None


def describe_write_operation(keyword: str) -> str:
    """'INTO' -> 'INTO (SELECT ... INTO creates a table)', for error messages."""
    keyword = str(keyword or "").upper()
    reason = _REASONS.get(keyword)
    if reason is None and keyword.startswith("XP_"):
        reason = "runs an extended stored procedure, which can reach the operating system"
    elif reason is None and keyword.startswith("SP_"):
        reason = "runs a system stored procedure, whose effects cannot be checked"
    elif reason is None:
        reason = (
            "a statement must start with SELECT or WITH; anything else may be a command, "
            "or a stored procedure run by name"
        )
    return f"{keyword} ({reason})"


def _statement_starts(text: str):
    """Where a statement can begin: the start, and after every `;`.

    A `;` inside a literal yields a false start - at worst a refusal, never a miss.
    """
    yield 0
    for index, char in enumerate(text):
        if char == ";":
            yield index + 1


def _leading_token(text: str, position: int) -> str | None:
    """The first token of the statement at `position`, or None if it is empty."""
    position = _skip_trivia(text, position)
    if position >= len(text) or text[position] == ";":
        return None
    word = _WORD_START.match(text, position)
    if word:
        return word.group(0).upper()
    # `[dbo].[usp_purge]` or `{call ...}` - not a word, and not a read either.
    return text[position:].split(None, 1)[0][:40].upper()


def _skip_trivia(text: str, position: int) -> int:
    """Skip whitespace, comments and opening parentheses."""
    length = len(text)
    while position < length:
        char = text[position]
        if char.isspace() or char == "(":
            position += 1
        elif text.startswith("--", position):
            ends = [i for i in (text.find("\n", position), text.find("\r", position)) if i >= 0]
            position = min(ends) if ends else length
        elif text.startswith("/*", position):
            position = _block_comment_end(text, position)
        else:
            break
    return position


def _block_comment_end(text: str, position: int) -> int:
    """Both SQL Server and DuckDB nest /* */ comments, so this must too."""
    depth = 0
    length = len(text)
    while position < length:
        if text.startswith("/*", position):
            depth += 1
            position += 2
        elif text.startswith("*/", position):
            depth -= 1
            position += 2
            if depth == 0:
                return position
        else:
            position += 1
    return length
