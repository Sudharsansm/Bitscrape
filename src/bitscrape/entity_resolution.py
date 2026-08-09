"""
Bitscrape Entity Resolution
===========================

Recognizes that different textual mentions likely refer to the same
real-world entity ("Jon Smith", "John Smith", "J. Smith" -> one cluster),
using normalized string similarity -- deliberately NOT a deep-learning
entity-linking system (that needs a trained model and typically a reference
knowledge base like Wikidata to link against, which this project can't
bundle). This is the same class of heuristic used as a first-pass /
baseline in real entity-resolution pipelines before a learned model refines
it further.

Pairs with ``bitscrape.knowledge_graph``: resolve entity mentions first,
then use the canonical cluster name as the node identity when building
relations, instead of treating "Jon Smith" and "John Smith" as two
unrelated graph nodes.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


def normalize_entity_name(name: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace -- the
    baseline normalization before comparing two mentions."""
    name = name.strip().lower()
    name = re.sub(r"[.,;:!?'\"()\[\]]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _initials_match(a: str, b: str) -> bool:
    """True if one name is an initials-abbreviated form of the other, e.g.
    'j smith' vs 'jon smith' vs 'john smith' -- same last token, and every
    token in the shorter name is a prefix of (or equal to) the
    corresponding token in the longer name."""
    tokens_a, tokens_b = a.split(), b.split()
    if len(tokens_a) != len(tokens_b) or not tokens_a:
        return False
    if tokens_a[-1] != tokens_b[-1]:  # last name/token must match exactly
        return False
    for ta, tb in zip(tokens_a[:-1], tokens_b[:-1]):
        shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if not longer.startswith(shorter):
            return False
    return True


def similarity(name_a: str, name_b: str) -> float:
    """
    Similarity score in [0, 1] between two (already-normalized-friendly,
    but normalization is applied internally) entity name strings. Combines:
      - exact match after normalization -> 1.0
      - initials/abbreviation match ("J Smith" vs "John Smith") -> 0.95
      - substring containment (one name wholly contains the other as
        whitespace-bounded tokens, e.g. "Acme" vs "Acme Corp") -> 0.9
      - otherwise, standard sequence-similarity ratio (difflib), which
        catches typos and minor variations.
    """
    a, b = normalize_entity_name(name_a), normalize_entity_name(name_b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _initials_match(a, b):
        return 0.95

    tokens_a, tokens_b = set(a.split()), set(b.split())
    if tokens_a and tokens_b and (tokens_a <= tokens_b or tokens_b <= tokens_a):
        return 0.9

    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class EntityCluster:
    canonical_name: str
    mentions: list[str] = field(default_factory=list)

    def add(self, mention: str) -> None:
        if mention not in self.mentions:
            self.mentions.append(mention)


class EntityResolver:
    """
    Incrementally clusters entity mentions: each call to ``resolve()``
    either joins an existing cluster (if similar enough to that cluster's
    canonical name) or starts a new one. The canonical name for a cluster
    is the first (typically fullest/most complete) mention seen for it --
    a simple, predictable choice rather than trying to guess which variant
    is "most correct."
    """

    def __init__(self, similarity_threshold: float = 0.85) -> None:
        self._threshold = similarity_threshold
        self._clusters: list[EntityCluster] = []

    def resolve(self, mention: str) -> str:
        """
        Returns the canonical name for the cluster this mention belongs to
        (creating a new cluster if it doesn't match any existing one).
        """
        best_cluster: EntityCluster | None = None
        best_score = 0.0
        for cluster in self._clusters:
            score = similarity(mention, cluster.canonical_name)
            if score > best_score:
                best_score = score
                best_cluster = cluster

        if best_cluster is not None and best_score >= self._threshold:
            best_cluster.add(mention)
            return best_cluster.canonical_name

        new_cluster = EntityCluster(canonical_name=mention, mentions=[mention])
        self._clusters.append(new_cluster)
        return new_cluster.canonical_name

    def resolve_all(self, mentions: list[str]) -> dict[str, str]:
        """Convenience: resolves a batch, returning {original_mention:
        canonical_name}."""
        return {mention: self.resolve(mention) for mention in mentions}

    def clusters(self) -> list[EntityCluster]:
        return list(self._clusters)

    def cluster_for(self, mention: str) -> EntityCluster | None:
        """Looks up the cluster a previously-resolved mention landed in,
        without resolving a new one."""
        for cluster in self._clusters:
            if mention in cluster.mentions:
                return cluster
        return None

    @property
    def cluster_count(self) -> int:
        return len(self._clusters)
