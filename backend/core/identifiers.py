"""Canonical identifier normalization - the single source of truth for matching a phrase
typed by a user against a physical column name in the database.

Every comparison in the app (column resolution, alias lookup, SQL repair, schema
retrieval) goes through `compact()` so that spelling-format differences can never cause a
mismatch:

    "Sub_Inward_Number"  -> "subinwardnumber"
    "sub inward number"  -> "subinwardnumber"
    "Sub Inward  Number" -> "subinwardnumber"
    "SUB-INWARD-NUMBER"  -> "subinwardnumber"

`tokens()` is the looser, word-level view used for partial/fuzzy matching:

    "sub inward no"      -> ("sub", "inward", "no")

Nothing here ever changes the PHYSICAL identifier that reaches SQL - normalization is only
ever used as a lookup key. The physical name is always taken from the live schema.
"""
from __future__ import annotations

import difflib
import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Interchangeable word forms seen in business questions vs. physical column names.
# Used to generate deterministic alias variants (see backend/core/column_registry.py).
# Keep every entry lowercase and alphanumeric.
WORD_VARIANTS: dict[str, tuple[str, ...]] = {
    "number": ("no", "num", "nbr"),
    "no": ("number", "num"),
    "num": ("number", "no"),
    "amount": ("amt",),
    "amt": ("amount",),
    "premium": ("prem",),
    "prem": ("premium",),
    "percentage": ("percent", "pct", "per"),
    "percent": ("percentage", "pct"),
    "date": ("dt",),
    "dt": ("date",),
    "code": ("cd",),
    "cd": ("code",),
    "name": ("nm",),
    "insured": ("ins",),
    "policy": ("pol",),
    "reference": ("ref",),
    "certificate": ("cert",),
    "endorsement": ("endt", "endo"),
    "customer": ("cust",),
    "intermediary": ("imd",),
    "commission": ("comm",),
    "registration": ("regn", "reg"),
    "vehicle": ("veh",),
    "manufacturing": ("manufacture", "mfg"),
    "business": ("biz",),
    "department": ("dept",),
    "quantity": ("qty",),
    "average": ("avg",),
    "previous": ("prev",),
    "secondary": ("second",),
    "tertiary": ("third",),
    "primary": ("first",),
    "company": ("co",),
    "description": ("desc",),
    "category": ("cat",),
    "location": ("loc",),
    "account": ("acct", "ac"),
    "identifier": ("id",),
    "manager": ("mgr",),
    "employee": ("emp",),
    "year": ("yr",),
    "yr": ("year",),
    "capacity": ("cap",),
    "invoice": ("inv",),
    "voucher": ("vch",),
    "collected": ("collection",),
}


# Name tokens that classify a column by ROLE, shared by the data-source layer (which
# decides whether a text column may be parsed as a number) and the agents (which decide
# what to aggregate). They live here so the two can never drift apart.
#
# An IDENTIFIER must never be summed or silently parsed: POLICY_NO holds 1029156133, which
# is a reference, not a quantity.
IDENTIFIER_NAME_TOKENS: frozenset[str] = frozenset({
    "no", "number", "num", "nbr", "code", "cd", "id", "identifier", "key",
    "gstin", "pin", "slip", "voucher", "invoice", "reference", "ref",
})

# A MEASURE is a quantity. Includes the domain's own abbreviations, because this schema
# stores real measures under names like GVW and PML that carry no generic hint.
MEASURE_NAME_TOKENS: frozenset[str] = frozenset({
    "total", "sum", "avg", "average", "mean", "count", "amount", "amt",
    "premium", "prem", "insured", "value", "balance", "commission", "comm",
    "tax", "duty", "gst", "sgst", "cgst", "igst", "cess", "gvw", "pml",
    "loading", "discount", "collected", "collection",
})

# Ratios are numbers, but a poor headline measure next to an absolute total.
RATIO_NAME_TOKENS: frozenset[str] = frozenset({
    "pct", "percent", "percentage", "share", "ratio", "rate", "per",
})


def looks_like_identifier(name: object) -> bool:
    return bool(set(tokens(name)) & IDENTIFIER_NAME_TOKENS)


def looks_like_measure(name: object) -> bool:
    """True when the NAME alone marks a column as a quantity.

    Used as the gate for parsing a text column into numbers. It is deliberately a
    positive test rather than "not an identifier": YEAR_OF_MANUFACTURING is neither, and
    turning it into a float would let it be charted as if it were a value.
    """
    name_tokens = set(tokens(name))
    # A ratio word settles it before the identifier check runs: Num_Cess_Percentage is a
    # decimal(18,6) measure whose "Num" is a type prefix, not "number of".
    if name_tokens & RATIO_NAME_TOKENS:
        return True
    if name_tokens & IDENTIFIER_NAME_TOKENS:
        return False
    return bool(name_tokens & MEASURE_NAME_TOKENS)


def compact(value: object) -> str:
    """Lowercase and strip every non-alphanumeric character.

    This is the primary lookup key: it makes underscores, spaces, hyphens, slashes and
    letter-case irrelevant when matching a user phrase to a column.
    """
    text = str(value or "")
    # Split camelCase / PascalCase first ("USGIpos_Policy_Number" -> "USGIpos Policy Number")
    text = _CAMEL_BOUNDARY.sub(" ", text)
    return _NON_ALNUM.sub("", text.lower())


def tokens(value: object) -> tuple[str, ...]:
    """Word-level view of an identifier or phrase, lowercased, empties removed."""
    text = _CAMEL_BOUNDARY.sub(" ", str(value or ""))
    return tuple(t.lower() for t in _TOKEN_SPLIT.split(text) if t)


def readable(value: object) -> str:
    """Turn a physical column name into human-facing prose ("USGI_SUM_INSURED" ->
    "USGI sum insured"). Used for answer text so users never see raw underscores."""
    parts = tokens(value)
    if not parts:
        return str(value or "")
    out = []
    for i, part in enumerate(parts):
        # Keep short all-caps acronyms uppercase (USGI, GST, NCB, TP, OD, RTO...)
        original = part.upper()
        if len(part) <= 4 and part.isalpha() and original in _ACRONYMS:
            out.append(original)
        elif i == 0:
            out.append(part.capitalize())
        else:
            out.append(part)
    return " ".join(out)


# Short tokens that must stay uppercase in user-facing prose. Ordinary English words that
# merely happen to be short ("sum", "no") are deliberately NOT here - listing them made
# readable("USGI_SUM_INSURED") render as "USGI SUM insured".
_ACRONYMS = {
    "USGI", "GST", "SGST", "CGST", "IGST", "NCB", "TP", "OD", "RTO", "PML", "GVW",
    "IMD", "BA", "ID", "TXT", "MI", "CDC", "POS",
}


def readable_inline(value: object) -> str:
    """`readable()` for use mid-sentence: the first word is lower-cased unless it is an
    acronym, so "Highest Branch name" reads as "Highest branch name"."""
    text = readable(value)
    if not text:
        return text
    first = text.split(" ", 1)[0]
    if first.upper() in _ACRONYMS:
        return text
    return text[0].lower() + text[1:]


def similarity(left: str, right: str) -> float:
    """Character-level similarity of two already-compacted keys, 0.0 - 1.0.

    Used only as a last-resort tie-breaker so a typo like "sb inward no" can still resolve
    to "sub_inward_number"; never used when an exact or alias match exists.
    """
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def variant_keys(name: str, max_variants: int = 64) -> set[str]:
    """Deterministically expand a physical column name into compacted alias keys.

    "Sub_Inward_Number" produces {"subinwardnumber", "subinwardno", "subinwardnum",
    "subinwardnbr"} by substituting each token with its known interchangeable forms.
    The expansion is bounded so a long column name can never blow up combinatorially.
    """
    parts = tokens(name)
    if not parts:
        return set()

    combos: list[list[str]] = [[]]
    for part in parts:
        options = [part, *WORD_VARIANTS.get(part, ())]
        next_combos: list[list[str]] = []
        for prefix in combos:
            for option in options:
                next_combos.append([*prefix, option])
                if len(next_combos) >= max_variants:
                    break
            if len(next_combos) >= max_variants:
                break
        combos = next_combos

    keys = {compact("".join(combo)) for combo in combos}
    # Trailing-token drop ("policy_issue_date" -> "policyissue") is too lossy to be safe,
    # but the leading acronym drop is common in speech ("USGI sum insured" -> "sum insured").
    if len(parts) > 2 and parts[0].upper() in _ACRONYMS:
        keys.add(compact("".join(parts[1:])))
    keys.discard("")
    return keys
