"""
Tests for bitscrape.canonicalize.
"""

from __future__ import annotations

import pytest

from bitscrape.canonicalize import (
    ContentFingerprint,
    RedirectLoopError,
    canonicalize_url,
    compute_fingerprint,
    is_near_duplicate,
    resolve_redirect_chain,
)


# ---------------------------------------------------------------------------
# canonicalize_url
# ---------------------------------------------------------------------------


def test_lowercases_scheme_and_host():
    assert canonicalize_url("HTTPS://Example.COM/Page") == "https://example.com/Page"


def test_strips_default_https_port():
    assert canonicalize_url("https://example.com:443/page") == "https://example.com/page"


def test_strips_default_http_port():
    assert canonicalize_url("http://example.com:80/page") == "http://example.com/page"


def test_keeps_nonstandard_port():
    assert canonicalize_url("http://example.com:8080/page") == "http://example.com:8080/page"


def test_strips_fragment():
    assert canonicalize_url("https://example.com/page#section") == "https://example.com/page"


def test_keeps_fragment_when_disabled():
    result = canonicalize_url("https://example.com/page#section", strip_fragment=False)
    assert result == "https://example.com/page#section"


def test_strips_trailing_slash():
    assert canonicalize_url("https://example.com/page/") == "https://example.com/page"


def test_root_path_keeps_single_slash():
    assert canonicalize_url("https://example.com/") == "https://example.com/"
    assert canonicalize_url("https://example.com") == "https://example.com/"


def test_strips_utm_tracking_params():
    url = "https://example.com/page?utm_source=twitter&utm_campaign=x&id=42"
    assert canonicalize_url(url) == "https://example.com/page?id=42"


def test_strips_known_tracking_params():
    url = "https://example.com/page?fbclid=abc&gclid=xyz&id=1"
    assert canonicalize_url(url) == "https://example.com/page?id=1"


def test_sorts_query_params():
    a = canonicalize_url("https://example.com/page?b=2&a=1")
    b = canonicalize_url("https://example.com/page?a=1&b=2")
    assert a == b


def test_two_equivalent_urls_canonicalize_identically():
    a = "HTTPS://Example.com:443/page/?utm_source=x&b=2&a=1#top"
    b = "https://example.com/page?a=1&b=2"
    assert canonicalize_url(a) == canonicalize_url(b)


def test_preserves_meaningful_query_params():
    # Pagination params etc. must NOT be stripped -- only known tracking ones.
    assert canonicalize_url("https://example.com/list?page=2") == "https://example.com/list?page=2"


def test_userinfo_in_netloc_preserved():
    result = canonicalize_url("https://User:Pass@Example.com/page")
    assert result == "https://User:Pass@example.com/page"


def test_extra_tracking_params_can_be_supplied():
    result = canonicalize_url(
        "https://example.com/page?custom_tracker=1&id=2",
        extra_tracking_params=frozenset({"custom_tracker"}),
    )
    assert result == "https://example.com/page?id=2"


# ---------------------------------------------------------------------------
# resolve_redirect_chain
# ---------------------------------------------------------------------------


def test_resolves_simple_chain():
    hops = ["https://example.com/old", "https://example.com/new"]
    assert resolve_redirect_chain(hops) == "https://example.com/new"


def test_single_hop_chain():
    assert resolve_redirect_chain(["https://example.com/page"]) == "https://example.com/page"


def test_canonicalizes_final_destination():
    hops = ["https://example.com/old", "https://Example.com:443/new/?utm_source=x"]
    assert resolve_redirect_chain(hops) == "https://example.com/new"


def test_detects_redirect_loop():
    hops = ["https://example.com/a", "https://example.com/b", "https://example.com/a"]
    with pytest.raises(RedirectLoopError):
        resolve_redirect_chain(hops)


def test_detects_loop_even_with_different_tracking_params():
    """A loop disguised by different query strings should still be caught,
    since canonicalization strips tracking params before comparing."""
    hops = [
        "https://example.com/a?utm_source=x",
        "https://example.com/b",
        "https://example.com/a?utm_source=y",
    ]
    with pytest.raises(RedirectLoopError):
        resolve_redirect_chain(hops)


def test_empty_chain_raises_value_error():
    with pytest.raises(ValueError):
        resolve_redirect_chain([])


def test_exceeds_max_hops_raises_value_error():
    hops = [f"https://example.com/page{i}" for i in range(25)]
    with pytest.raises(ValueError):
        resolve_redirect_chain(hops, max_hops=20)


# ---------------------------------------------------------------------------
# SimHash near-duplicate detection
# ---------------------------------------------------------------------------


def test_identical_text_has_zero_hamming_distance():
    fp1 = compute_fingerprint("The quick brown fox jumps over the lazy dog")
    fp2 = compute_fingerprint("The quick brown fox jumps over the lazy dog")
    assert fp1.hamming_distance(fp2) == 0


def test_completely_different_text_has_large_hamming_distance():
    fp1 = compute_fingerprint(
        "The quick brown fox jumps over the lazy dog in the forest at dawn"
    )
    fp2 = compute_fingerprint(
        "Quantum computing relies on superposition and entanglement of qubits"
    )
    assert fp1.hamming_distance(fp2) > 10


def test_near_duplicate_with_minor_edit_detected():
    original = (
        "Breaking news: the city council approved the new budget today after "
        "a long debate session that lasted several hours in total. The vote "
        "was seven to two in favor, with council members citing the need for "
        "infrastructure investment as the primary driver behind the decision. "
        "Residents in attendance expressed a mix of relief and skepticism "
        "about how quickly the funded projects would actually begin."
    )
    # Minor edit: appended a timestamp/footer, like a real near-duplicate page.
    # On a longer, more representative document, a short footer shifts only
    # a small fraction of the shingle set, keeping the fingerprints close.
    edited = original + " Published at 10:42 AM."
    assert is_near_duplicate(original, edited, max_hamming_distance=8)


def test_substantively_different_text_not_near_duplicate():
    text_a = (
        "The stock market rallied today as tech shares surged on strong "
        "earnings reports from major companies across several sectors."
    )
    text_b = (
        "A new species of frog was discovered in the Amazon rainforest by "
        "researchers studying biodiversity in remote tropical regions."
    )
    assert not is_near_duplicate(text_a, text_b)


def test_fingerprint_hex_representation():
    fp = ContentFingerprint(bits=0xDEADBEEF)
    assert fp.hex == "00000000deadbeef"


def test_empty_text_produces_zero_fingerprint():
    fp = compute_fingerprint("")
    assert fp.bits == 0


def test_short_text_below_shingle_size_still_works():
    fp1 = compute_fingerprint("hello world")
    fp2 = compute_fingerprint("hello world")
    assert fp1.hamming_distance(fp2) == 0
