"""The business glossary: the words a user types must reach the right physical column.

`scripts/build_column_aliases.py` holds one GLOSSARY block per column - every way the
business says it, plus what it means. Those aliases are the only thing standing between
"show me coverage amount by branch" and a query against the wrong premium column, so the
properties that make them trustworthy are pinned down here:

  * every alias resolves, and resolves to the column that claims it
  * an alias NEVER overrides a different column's real name
  * a phrase two columns both claim is refused, unless PREFERRED records a decision
  * every column carries a meaning, because that is what the SQL model reads

No database, LLM or ChromaDB is involved: the registry is built from a fixed column list.
"""
import pytest

from backend.core.column_registry import ColumnInfo, ColumnRegistry
from scripts.build_column_aliases import GLOSSARY, PREFERRED, build


# --------------------------------------------------------------------------------------
# A registry built from the glossary itself, with no data source behind it
# --------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def payload() -> dict:
    """What the build script would write for a table holding exactly the glossary columns."""
    live = [{"column_name": name, "data_type": "varchar"} for name in GLOSSARY]
    built, problems = build(live, rebuild=True)
    assert problems == [], f"the glossary does not build cleanly: {problems}"
    return built


@pytest.fixture(scope="module")
def registry(payload) -> ColumnRegistry:
    infos = [
        ColumnInfo(
            name=name,
            data_type=entry["data_type"],
            category=entry["category"],
            description=entry["description"],
            aliases=entry["aliases"],
            generated=entry["generated"],
        )
        for name, entry in payload["columns"].items()
    ]
    return ColumnRegistry(infos, preferred=payload["_preferred"])


# --------------------------------------------------------------------------------------
# Every alias lands on its own column
# --------------------------------------------------------------------------------------

def test_every_alias_resolves_to_the_column_that_claims_it(registry):
    wrong = []
    for column, entry in GLOSSARY.items():
        for alias in entry["aliases"]:
            got = registry.resolve(alias, allow_fuzzy=False)
            if got != column:
                wrong.append(f"{alias!r} -> {got} (expected {column})")
    assert not wrong, "aliases resolving to the wrong column:\n" + "\n".join(wrong)


def test_no_alias_shadows_another_columns_real_name(registry):
    """A column's own name always wins: 'gross premium' can only mean GROSS_PREMIUM."""
    for column, entry in GLOSSARY.items():
        for alias in entry["aliases"]:
            other = registry.by_name.get(alias)
            assert other is None or other.name == column, (
                f"{column} claims {alias!r}, which is another column's real name"
            )


def test_every_column_carries_a_meaning(payload):
    """The meaning is what the SQL model reads to tell two similar columns apart."""
    missing = [n for n, e in payload["columns"].items() if not e["description"].strip()]
    assert not missing, f"columns with no meaning: {missing}"


def test_a_column_added_to_the_glossary_is_reachable_by_its_aliases(registry):
    """The point of the whole file: business words, not physical names, in the chat box."""
    assert registry.resolve("coverage amount") == "USGI_SUM_INSURED"
    assert registry.resolve("proposal number") == "REFERENCE_NUMBER"
    assert registry.resolve("rc number") == "MTOR_Registration_No"
    assert registry.resolve("no claim bonus") == "NCB"
    assert registry.resolve("relationship manager name") == "BA_NAME"


# --------------------------------------------------------------------------------------
# The USGI-share columns, which is where a wrong pick costs real money
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("gross premium", "GROSS_PREMIUM"),
        ("business", "GROSS_PREMIUM"),
        ("net premium", "NET_PREMIUM"),
        ("usgi gross prem", "USGI_GROSS_PREMIUM"),
        ("usgi premium", "USGI_GROSS_PREMIUM"),
        ("usgi net prem", "USGI_NET_PREMIUM"),
        ("sum insured", "USGI_SUM_INSURED"),
        ("si", "USGI_SUM_INSURED"),
        ("tsi", "TOTAL_SUM_INSURED"),
    ],
)
def test_premium_and_sum_insured_phrases_are_bound_explicitly(registry, phrase, expected):
    assert registry.resolve(phrase, allow_fuzzy=False) == expected


def test_policy_number_phrasing_separates_the_three_policy_columns(registry):
    assert registry.resolve("policy no", allow_fuzzy=False) == "POLICY_NO"
    assert registry.resolve("actual policy number", allow_fuzzy=False) == "POLICY_NO_CHAR"
    assert registry.resolve("pos policy number", allow_fuzzy=False) == "USGIpos_Policy_Number"


# --------------------------------------------------------------------------------------
# Ambiguity is still refused - the preference only binds a tie someone decided
# --------------------------------------------------------------------------------------

def test_a_contested_phrase_with_no_recorded_preference_is_refused():
    columns = [
        ColumnInfo(name="ALPHA_AMOUNT", aliases=["settlement amount"]),
        ColumnInfo(name="BETA_AMOUNT", aliases=["settlement amount"]),
    ]
    reg = ColumnRegistry(columns)
    assert reg.resolve("settlement amount", allow_fuzzy=False) is None
    assert "settlementamount" in reg.collisions


def test_a_recorded_preference_binds_the_tie():
    columns = [
        ColumnInfo(name="ALPHA_AMOUNT", aliases=["settlement amount"]),
        ColumnInfo(name="BETA_AMOUNT", aliases=["settlement amount"]),
    ]
    reg = ColumnRegistry(columns, preferred={"settlement amount": "BETA_AMOUNT"})
    assert reg.resolve("settlement amount", allow_fuzzy=False) == "BETA_AMOUNT"
    assert reg.collisions == {}
    assert reg.resolved_ties["settlementamount"] == "BETA_AMOUNT"


def test_a_preference_naming_a_column_that_does_not_claim_the_phrase_is_ignored():
    """A typo in PREFERRED must not silently redirect a phrase to an unrelated column."""
    columns = [
        ColumnInfo(name="ALPHA_AMOUNT", aliases=["settlement amount"]),
        ColumnInfo(name="BETA_AMOUNT", aliases=["settlement amount"]),
        ColumnInfo(name="GAMMA_AMOUNT"),
    ]
    reg = ColumnRegistry(columns, preferred={"settlement amount": "GAMMA_AMOUNT"})
    assert reg.resolve("settlement amount", allow_fuzzy=False) is None
    assert "settlementamount" in reg.collisions


def test_a_preference_can_never_override_a_real_column_name():
    columns = [
        ColumnInfo(name="GROSS_PREMIUM"),
        ColumnInfo(name="USGI_GROSS_PREMIUM"),
    ]
    reg = ColumnRegistry(columns, preferred={"gross premium": "USGI_GROSS_PREMIUM"})
    assert reg.resolve("gross premium", allow_fuzzy=False) == "GROSS_PREMIUM"


# --------------------------------------------------------------------------------------
# The build script's own guarantees, so a future edit fails loudly instead of silently
# --------------------------------------------------------------------------------------

def test_a_glossary_block_for_a_column_that_does_not_exist_is_reported():
    live = [{"column_name": "BRANCH_NAME", "data_type": "varchar"}]
    _, problems = build(live, rebuild=True)
    assert any("not a column of" in p for p in problems)


def test_two_columns_claiming_one_alias_is_reported(monkeypatch):
    import scripts.build_column_aliases as builder

    monkeypatch.setattr(builder, "GLOSSARY", {
        "ALPHA_AMOUNT": {"aliases": ["settlement amount"], "meaning": "a."},
        "BETA_AMOUNT": {"aliases": ["settlement amount"], "meaning": "b."},
    })
    monkeypatch.setattr(builder, "PREFERRED", {})
    live = [
        {"column_name": "ALPHA_AMOUNT", "data_type": "float"},
        {"column_name": "BETA_AMOUNT", "data_type": "float"},
    ]
    _, problems = builder.build(live, rebuild=True)
    assert any("is claimed by" in p for p in problems)


def test_an_alias_shadowing_a_real_column_is_reported(monkeypatch):
    import scripts.build_column_aliases as builder

    monkeypatch.setattr(builder, "GLOSSARY", {
        "USGI_GROSS_PREMIUM": {"aliases": ["gross premium"], "meaning": "usgi share."},
        "GROSS_PREMIUM": {"aliases": [], "meaning": "the total."},
    })
    monkeypatch.setattr(builder, "PREFERRED", {})
    live = [
        {"column_name": "USGI_GROSS_PREMIUM", "data_type": "float"},
        {"column_name": "GROSS_PREMIUM", "data_type": "float"},
    ]
    _, problems = builder.build(live, rebuild=True)
    assert any("shadows the real column" in p for p in problems)


def test_a_preference_pointing_at_a_missing_column_is_reported(monkeypatch):
    import scripts.build_column_aliases as builder

    monkeypatch.setattr(builder, "GLOSSARY", {"BRANCH_NAME": {"aliases": [], "meaning": "b."}})
    monkeypatch.setattr(builder, "PREFERRED", {"branch": "NO_SUCH_COLUMN"})
    live = [{"column_name": "BRANCH_NAME", "data_type": "varchar"}]
    _, problems = builder.build(live, rebuild=True)
    assert any("not a live column" in p for p in problems)


def test_an_ordinary_run_keeps_aliases_hand_edited_into_the_json(monkeypatch, tmp_path):
    """Adding a shortcut straight to the JSON must survive the next ordinary rebuild."""
    import scripts.build_column_aliases as builder

    monkeypatch.setattr(builder, "GLOSSARY", {"BRANCH_NAME": {"aliases": ["branch"], "meaning": "b."}})
    monkeypatch.setattr(builder, "_load_existing", lambda: {
        "BRANCH_NAME": {"aliases": ["my own shortcut"], "description": "stale."}
    })
    live = [{"column_name": "BRANCH_NAME", "data_type": "varchar"}]

    merged, _ = builder.build(live, rebuild=False)
    assert merged["columns"]["BRANCH_NAME"]["aliases"] == ["branch", "my own shortcut"]

    rebuilt, _ = builder.build(live, rebuild=True)
    assert rebuilt["columns"]["BRANCH_NAME"]["aliases"] == ["branch"]


def test_the_glossary_meaning_replaces_a_stale_description(monkeypatch):
    """The glossary is the source of truth, so an auto-generated description gives way."""
    import scripts.build_column_aliases as builder

    monkeypatch.setattr(builder, "GLOSSARY", {
        "BRANCH_NAME": {"aliases": [], "meaning": "Branch office name."}
    })
    monkeypatch.setattr(builder, "_load_existing", lambda: {
        "BRANCH_NAME": {"aliases": [], "description": "Branch name."}
    })
    live = [{"column_name": "BRANCH_NAME", "data_type": "varchar"}]
    built, _ = builder.build(live, rebuild=False)
    assert built["columns"]["BRANCH_NAME"]["description"] == "Branch office name."


def test_every_preference_names_a_column_in_the_glossary():
    """A preference for a column nobody describes is a leftover, not a decision."""
    for phrase, winner in PREFERRED.items():
        assert winner in GLOSSARY, f"PREFERRED[{phrase!r}] points at unknown {winner}"


# --------------------------------------------------------------------------------------
# The same file has to serve both data sources
# --------------------------------------------------------------------------------------

def test_the_glossary_is_independent_of_the_data_source():
    """Aliases and meanings are keyed by column NAME, so local and SQL Server share them."""
    as_csv = [{"column_name": n, "data_type": "float"} for n in GLOSSARY]
    as_sqlserver = [{"column_name": n, "data_type": "decimal"} for n in GLOSSARY]

    local, _ = build(as_csv, rebuild=True)
    remote, _ = build(as_sqlserver, rebuild=True)

    for name in GLOSSARY:
        assert local["columns"][name]["aliases"] == remote["columns"][name]["aliases"]
        assert local["columns"][name]["description"] == remote["columns"][name]["description"]


def test_rebuild_re_derives_the_category_from_the_live_type(monkeypatch):
    """A category frozen from the CSV must not survive onto SQL Server.

    `Live_Count` is varchar in the local CSV (a dimension) but an int column in the real
    table (a measure). Category decides what may be SUMmed and what becomes a date axis, so
    carrying the CSV's answer across would chart the wrong column.
    """
    import scripts.build_column_aliases as builder

    monkeypatch.setattr(builder, "GLOSSARY", {"Live_Count": {"aliases": [], "meaning": "x."}})
    monkeypatch.setattr(builder, "_load_existing", lambda: {
        "Live_Count": {"category": "dimension", "aliases": [], "description": "x."}
    })
    live = [{"column_name": "Live_Count", "data_type": "int"}]

    rebuilt, _ = builder.build(live, rebuild=True)
    assert rebuilt["columns"]["Live_Count"]["category"] == "measure"

    # An ordinary run still honours a category hand-set in the JSON.
    merged, _ = builder.build(live, rebuild=False)
    assert merged["columns"]["Live_Count"]["category"] == "dimension"


def test_a_live_column_with_no_glossary_block_is_listed(monkeypatch):
    """Silence here would mean a real column nobody can ask about in business words."""
    import scripts.build_column_aliases as builder

    monkeypatch.setattr(builder, "GLOSSARY", {"BRANCH_NAME": {"aliases": [], "meaning": "b."}})
    monkeypatch.setattr(builder, "PREFERRED", {})
    monkeypatch.setattr(builder, "_load_existing", lambda: {})
    live = [
        {"column_name": "BRANCH_NAME", "data_type": "varchar"},
        {"column_name": "A_NEW_SQLSERVER_COLUMN", "data_type": "decimal"},
    ]
    built, problems = builder.build(live, rebuild=True)

    assert problems == [], "an undescribed column is reported, not a build failure"
    assert built["_not_in_glossary"] == ["A_NEW_SQLSERVER_COLUMN"]
