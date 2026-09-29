"""Column resolution: turn anything a user types into the exact physical column name.

The user never types the physical name. They type "sub inward no", "sb inward no",
"policy no", "usgi sum insured". This registry resolves all of those to the one real
column, or reports honestly that it cannot.

Resolution order (first match wins - a weaker rule NEVER overrides a stronger one):

  1. exact physical name           "Sub_Inward_Number"
  2. compacted physical name       "subinwardnumber"  (spaces/underscores/case removed)
  3. curated alias                 from backend/knowledge/column_aliases.json
  4. generated alias               "sub inward no" via the word-variant table
  5. fuzzy match                   "sb inward no" -> ratio >= COLUMN_FUZZY_THRESHOLD

An alias claimed by two different columns is a collision. Collisions are dropped from the
lookup entirely and logged, because silently picking one of two columns is how a chatbot
returns confidently wrong numbers. `scripts/build_column_aliases.py` fails loudly on
curated collisions so they are fixed in the file, not at runtime.

The one exception is `preferred_shortcuts` in the alias file: a contested phrase the
BUSINESS has actually decided on ("sum insured" means USGI_SUM_INSURED, not the
co-insurance total). That is a recorded decision, not a guess, so it binds. A preference
for an uncontested phrase, or one naming a column that does not own the phrase, is ignored.

The registry is built from the LIVE schema of the active data source, so a column that
exists in the alias file but not in the table is ignored, and a column in the table with
no alias entry still resolves by its own name.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from backend.core.identifiers import compact, similarity, tokens, variant_keys
from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


@dataclass
class ColumnInfo:
    name: str                     # physical name, exactly as it exists in the table
    data_type: str = "varchar"
    category: str = "dimension"   # dimension | measure | date
    description: str = ""
    aliases: list[str] = field(default_factory=list)   # curated, human-readable
    generated: list[str] = field(default_factory=list) # machine-expanded compact keys

    @property
    def key(self) -> str:
        return compact(self.name)


_TEXT_TYPES = {"varchar", "nvarchar", "char", "nchar", "text", "ntext"}
_NUMERIC_TYPES = {
    "int", "bigint", "smallint", "tinyint", "decimal", "numeric",
    "float", "real", "money", "smallmoney",
}
_DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "time", "timestamp"}


def infer_category(name: str, data_type: str) -> str:
    lowered = str(data_type or "").lower()
    if lowered in _DATE_TYPES:
        return "date"
    name_tokens = set(tokens(name))
    if lowered in _NUMERIC_TYPES:
        # Identifier-like numeric columns are dimensions, not things to SUM.
        if name_tokens & {"no", "number", "code", "id", "num"}:
            return "dimension"
        return "measure"
    if name_tokens & {"date", "time", "timestamp"}:
        return "date"
    return "dimension"


class ColumnRegistry:
    """Immutable snapshot of the live columns plus every way to refer to them."""

    def __init__(self, columns: list[ColumnInfo], preferred: dict[str, str] | None = None):
        self.columns: list[ColumnInfo] = columns
        self.by_name: dict[str, ColumnInfo] = {c.name: c for c in columns}
        self._exact: dict[str, str] = {}      # compacted physical name -> physical name
        self._alias: dict[str, str] = {}      # compacted alias        -> physical name
        # Contested phrases the business has decided on: compacted phrase -> physical name.
        self._preferred: dict[str, str] = {
            compact(phrase): name for phrase, name in (preferred or {}).items() if compact(phrase)
        }
        self.resolved_ties: dict[str, str] = {}
        self.collisions: dict[str, list[str]] = {}
        self._build_lookup()

    # -- construction ------------------------------------------------------------------

    def _build_lookup(self) -> None:
        for column in self.columns:
            self._exact[column.key] = column.name

        claims: dict[str, set[str]] = {}
        for column in self.columns:
            keys: set[str] = set()
            for alias in column.aliases:
                keys.add(compact(alias))
            for generated in column.generated:
                keys.add(compact(generated))
            keys |= variant_keys(column.name)
            keys.discard("")
            for key in keys:
                if key in self._exact and self._exact[key] != column.name:
                    # An alias may never shadow another column's real name.
                    continue
                claims.setdefault(key, set()).add(column.name)

        for key, owners in claims.items():
            if key in self._exact:
                continue
            if len(owners) == 1:
                self._alias[key] = next(iter(owners))
                continue
            winner = self._preferred.get(key)
            if winner in owners:
                self._alias[key] = winner
                self.resolved_ties[key] = winner
            else:
                self.collisions[key] = sorted(owners)

        if self.resolved_ties:
            logger.info(
                "%s contested column shortcut(s) bound by a recorded business preference: %s",
                len(self.resolved_ties),
                "; ".join(f"'{k}' -> {v}" for k, v in self.resolved_ties.items()),
            )

        if self.collisions:
            logger.warning(
                "%s ambiguous column shortcut(s) ignored (claimed by more than one column): %s",
                len(self.collisions),
                "; ".join(
                    f"'{k}' -> {owners}" for k, owners in list(self.collisions.items())[:8]
                ),
            )

    # -- queries -----------------------------------------------------------------------

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def shortcut_count(self) -> int:
        return len(self._alias)

    def get(self, name: str) -> ColumnInfo | None:
        return self.by_name.get(name)

    def resolve(self, phrase: str, *, allow_fuzzy: bool = True) -> str | None:
        """Physical column name for a phrase, or None when it cannot be resolved safely."""
        raw = str(phrase or "").strip()
        if not raw:
            return None
        if raw in self.by_name:
            return raw

        key = compact(raw)
        if not key:
            return None
        if key in self._exact:
            return self._exact[key]
        if key in self._alias:
            return self._alias[key]
        if key in self.collisions:
            return None  # genuinely ambiguous - refuse rather than guess
        if not allow_fuzzy:
            return None

        best_name, best_score = None, 0.0
        runner_up = 0.0
        for candidate_key, name in (*self._exact.items(), *self._alias.items()):
            score = similarity(key, candidate_key)
            if score > best_score:
                best_name, runner_up, best_score = name, best_score, score
            elif score > runner_up:
                runner_up = score
        threshold = settings.COLUMN_FUZZY_THRESHOLD
        # Require a clear winner: a near-tie means the phrase is ambiguous.
        if best_name and best_score >= threshold and (best_score - runner_up) >= 0.02:
            logger.debug("Fuzzy column match '%s' -> %s (%.3f)", raw, best_name, best_score)
            return best_name
        return None

    def resolve_all(self, phrases: list[str]) -> dict[str, str]:
        """{phrase: physical_column} for every phrase that resolves."""
        out: dict[str, str] = {}
        for phrase in phrases:
            resolved = self.resolve(phrase)
            if resolved:
                out[phrase] = resolved
        return out

    def suggest(self, phrase: str, limit: int = 3) -> list[str]:
        """Closest column names for an unresolvable phrase - used in error messages."""
        key = compact(phrase)
        if not key:
            return []
        scored = sorted(
            ((similarity(key, c.key), c.name) for c in self.columns), reverse=True
        )
        return [name for score, name in scored[:limit] if score > 0.4]

    def measures(self) -> list[str]:
        return [c.name for c in self.columns if c.category == "measure"]

    def dates(self) -> list[str]:
        return [c.name for c in self.columns if c.category == "date"]

    def shortcuts_for(self, name: str) -> list[str]:
        """Every shortcut that currently resolves to this column (for docs/debugging)."""
        return sorted(k for k, target in self._alias.items() if target == name)


# ======================================================================================
# Loading
# ======================================================================================

_registry: ColumnRegistry | None = None
_lock = threading.Lock()


def load_alias_document() -> dict:
    """The whole backend/knowledge/column_aliases.json, or {} when it cannot be read."""
    path = settings.resolved(settings.COLUMN_ALIASES_FILE)
    if not path.exists():
        logger.warning(
            "Column alias file %s not found - falling back to auto-generated shortcuts only. "
            "Run: python scripts/build_column_aliases.py",
            path,
        )
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Column alias file %s is unreadable (%s) - continuing without it.", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_alias_file() -> dict:
    columns = load_alias_document().get("columns")
    return columns if isinstance(columns, dict) else {}


def build_registry(live_columns: list[dict]) -> ColumnRegistry:
    """Merge the live schema with the curated alias file into one registry."""
    document = load_alias_document()
    alias_data = document.get("columns") if isinstance(document.get("columns"), dict) else {}
    raw_preferred = document.get("preferred_shortcuts")
    preferred = raw_preferred if isinstance(raw_preferred, dict) else {}
    # Alias entries are matched to live columns by compacted key so a change in
    # underscore/space style in either file can never silently drop the metadata.
    by_key = {compact(name): entry for name, entry in alias_data.items()}

    infos: list[ColumnInfo] = []
    for column in live_columns:
        name = str(column.get("column_name") or "").strip()
        if not name:
            continue
        data_type = str(column.get("data_type") or "varchar")
        entry = by_key.get(compact(name), {}) or {}
        infos.append(
            ColumnInfo(
                name=name,
                data_type=data_type,
                category=str(entry.get("category") or infer_category(name, data_type)),
                description=str(entry.get("description") or ""),
                aliases=[str(a) for a in entry.get("aliases", []) if str(a).strip()],
                generated=[str(a) for a in entry.get("generated", []) if str(a).strip()],
            )
        )
    return ColumnRegistry(infos, preferred={str(k): str(v) for k, v in preferred.items()})


def get_registry(refresh: bool = False) -> ColumnRegistry:
    """The process-wide registry, built from the ACTIVE data source's live schema.

    Raises nothing on a schema-read failure: an empty registry degrades the pipeline to
    its previous (unvalidated) behaviour rather than taking the whole app down.
    """
    global _registry
    if _registry is not None and not refresh:
        return _registry
    with _lock:
        if _registry is None or refresh:
            from backend.core.datasource import get_datasource

            try:
                live = get_datasource().get_table_columns()
            except Exception as exc:  # noqa: BLE001 - degrade, never crash a request
                logger.error(
                    "Could not read the live schema for the column registry (%s). "
                    "Column validation is disabled until the data source is reachable.",
                    exc,
                )
                live = []
            _registry = build_registry(live)
            logger.info(
                "Column registry ready: %s columns, %s shortcuts, %s ambiguous shortcut(s) ignored.",
                len(_registry.columns), len(_registry._alias), len(_registry.collisions),
            )
    return _registry


def reset_registry() -> None:
    global _registry
    with _lock:
        _registry = None
