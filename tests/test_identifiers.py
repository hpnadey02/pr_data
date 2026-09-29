"""Unit tests for identifier normalization (backend/core/identifiers.py).

These lock in the rule the whole matching layer depends on: spaces, underscores, hyphens
and letter case are irrelevant when comparing a user phrase to a column name.
"""
import pytest

from backend.core.identifiers import compact, readable, similarity, tokens, variant_keys


@pytest.mark.parametrize(
    "value",
    [
        "Sub_Inward_Number",
        "sub inward number",
        "SUB INWARD NUMBER",
        "Sub-Inward-Number",
        "  sub   inward   number  ",
        "sub.inward.number",
    ],
)
def test_all_spellings_compact_to_one_key(value):
    assert compact(value) == "subinwardnumber"


def test_compact_of_mixed_case_column():
    assert compact("USGIpos_Policy_Number") == "usgipospolicynumber"


def test_compact_splits_camel_case_boundaries():
    assert compact("policyIssueDate") == compact("policy_issue_date")


def test_compact_handles_empty_and_none():
    assert compact("") == ""
    assert compact(None) == ""


def test_tokens_are_lowercased_words():
    assert tokens("Sub_Inward_Number") == ("sub", "inward", "number")
    assert tokens("GROSS PREMIUM") == ("gross", "premium")


def test_readable_keeps_acronyms_upper():
    assert readable("USGI_SUM_INSURED").startswith("USGI")
    assert "_" not in readable("USGI_SUM_INSURED")


def test_readable_of_plain_column():
    assert readable("BRANCH_NAME") == "Branch name"


def test_similarity_is_symmetric_and_bounded():
    assert similarity("abc", "abc") == 1.0
    assert 0.0 <= similarity("sbinwardno", "subinwardnumber") <= 1.0
    assert similarity("", "abc") == 0.0


def test_variant_keys_cover_number_abbreviations():
    keys = variant_keys("Sub_Inward_Number")
    assert "subinwardnumber" in keys
    assert "subinwardno" in keys
    assert "subinwardnum" in keys


def test_variant_keys_are_bounded():
    # A long name must not explode combinatorially.
    keys = variant_keys("Secondary_Sales_Manager_Code")
    assert 0 < len(keys) <= 64


def test_variant_keys_drop_leading_acronym():
    keys = variant_keys("USGI_SUM_INSURED")
    assert "suminsured" in keys
