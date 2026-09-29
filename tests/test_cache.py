from backend.core.cache import cache_key, normalize_for_cache


def test_normalize_is_case_and_whitespace_insensitive():
    a = normalize_for_cache("  High-Performing Branch  Region-wise.  ")
    b = normalize_for_cache("high performing branch region wise")
    assert a == b


def test_cache_key_deterministic_for_equivalent_questions():
    k1 = cache_key("High-performing branch region-wise.")
    k2 = cache_key("high performing branch region wise")
    assert k1 == k2


def test_cache_key_differs_for_different_questions():
    k1 = cache_key("High-performing branch region-wise.")
    k2 = cache_key("Vertical-wise weekly business trend.")
    assert k1 != k2
