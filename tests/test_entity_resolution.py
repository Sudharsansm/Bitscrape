"""
Tests for bitscrape.entity_resolution.
"""

from __future__ import annotations

from bitscrape.entity_resolution import (
    EntityResolver,
    normalize_entity_name,
    similarity,
)


# ---------------------------------------------------------------------------
# normalize_entity_name
# ---------------------------------------------------------------------------


def test_normalizes_case_and_whitespace():
    assert normalize_entity_name("  John   SMITH  ") == "john smith"


def test_strips_punctuation():
    assert normalize_entity_name("J. Smith, Jr.") == "j smith jr"


# ---------------------------------------------------------------------------
# similarity
# ---------------------------------------------------------------------------


def test_identical_names_score_one():
    assert similarity("John Smith", "John Smith") == 1.0


def test_case_insensitive_exact_match():
    assert similarity("john smith", "JOHN SMITH") == 1.0


def test_initials_match_scores_high():
    assert similarity("J Smith", "John Smith") >= 0.9
    assert similarity("Jon Smith", "John Smith") >= 0.9


def test_different_last_names_do_not_initials_match():
    score = similarity("J Smith", "J Jones")
    assert score < 0.9


def test_substring_containment_scores_high():
    assert similarity("Acme", "Acme Corp") >= 0.85


def test_completely_different_names_score_low():
    assert similarity("John Smith", "Maria Garcia") < 0.5


def test_typo_variation_scores_moderately_high():
    score = similarity("Jonathan", "Jonathon")  # one-letter typo
    assert score > 0.8


def test_empty_string_scores_zero():
    assert similarity("", "John Smith") == 0.0
    assert similarity("John Smith", "") == 0.0


# ---------------------------------------------------------------------------
# EntityResolver
# ---------------------------------------------------------------------------


def test_first_mention_creates_new_cluster():
    resolver = EntityResolver()
    canonical = resolver.resolve("John Smith")
    assert canonical == "John Smith"
    assert resolver.cluster_count == 1


def test_similar_mention_joins_existing_cluster():
    resolver = EntityResolver(similarity_threshold=0.85)
    resolver.resolve("John Smith")
    canonical = resolver.resolve("Jon Smith")
    assert canonical == "John Smith"  # joined the first cluster
    assert resolver.cluster_count == 1


def test_dissimilar_mention_creates_new_cluster():
    resolver = EntityResolver()
    resolver.resolve("John Smith")
    canonical = resolver.resolve("Maria Garcia")
    assert canonical == "Maria Garcia"
    assert resolver.cluster_count == 2


def test_resolve_all_batch():
    resolver = EntityResolver()
    mapping = resolver.resolve_all(["John Smith", "Jon Smith", "J. Smith", "Maria Garcia"])
    assert mapping["John Smith"] == "John Smith"
    assert mapping["Jon Smith"] == "John Smith"
    assert mapping["J. Smith"] == "John Smith"
    assert mapping["Maria Garcia"] == "Maria Garcia"
    assert resolver.cluster_count == 2


def test_cluster_tracks_all_mentions():
    resolver = EntityResolver()
    resolver.resolve("John Smith")
    resolver.resolve("Jon Smith")
    resolver.resolve("J. Smith")

    cluster = resolver.cluster_for("Jon Smith")
    assert cluster is not None
    assert cluster.canonical_name == "John Smith"
    assert set(cluster.mentions) == {"John Smith", "Jon Smith", "J. Smith"}


def test_cluster_for_unknown_mention_returns_none():
    resolver = EntityResolver()
    resolver.resolve("John Smith")
    assert resolver.cluster_for("Never Seen") is None


def test_duplicate_mention_not_added_twice():
    resolver = EntityResolver()
    resolver.resolve("John Smith")
    resolver.resolve("John Smith")
    cluster = resolver.cluster_for("John Smith")
    assert cluster.mentions.count("John Smith") == 1


def test_higher_threshold_is_stricter():
    resolver = EntityResolver(similarity_threshold=0.99)
    resolver.resolve("John Smith")
    canonical = resolver.resolve("Jon Smith")  # would normally merge at 0.85
    assert canonical != "John Smith"  # too strict a threshold to merge


def test_realistic_multi_entity_scenario():
    """Simulates resolving entity mentions extracted from several articles
    about the same two people and one company."""
    resolver = EntityResolver()
    mentions = [
        "Marie Curie",
        "Maria Curie",  # variant spelling
        "M. Curie",  # abbreviated
        "Pierre Curie",  # different person, same last name
        "Nobel Prize",
        "the Nobel Prize",  # near-duplicate with article prefix (won't merge without extra normalization, that's fine)
    ]
    mapping = resolver.resolve_all(mentions)
    assert mapping["Marie Curie"] == mapping["M. Curie"]
    assert mapping["Marie Curie"] != mapping["Pierre Curie"]
