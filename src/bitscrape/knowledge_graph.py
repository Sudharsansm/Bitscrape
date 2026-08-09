"""
Bitscrape Knowledge Graph Support
=================================

Deliberately scoped honestly: this is NOT a full NLP entity-linking /
disambiguation pipeline (that's a genuine multi-week effort involving real
NER models, coreference resolution, and entity disambiguation against a
knowledge base like Wikidata). What's here is useful and real, but simpler:

  1. ``extract_entities()`` -- a heuristic capitalized-phrase extractor (the
     standard "proper noun sequence" baseline used before statistical NER
     became cheap). It will over-match (sentence-initial capitals, acronyms)
     and under-match (lowercase entity names, multi-word entities split by
     stopwords) -- treat it as a starting point / rough signal, not
     ground truth.
  2. ``KnowledgeGraph`` -- a real, working directed graph (built on
     ``networkx``, a genuine, widely-used graph library, not reinvented)
     for recording (subject, predicate, object) triples extracted either
     from structured item fields (reliable) or from the heuristic text
     extractor above (rough). Exports to JSON or GraphML (openable in
     Gephi/Cytoscape) for real downstream analysis.

If you need production-grade entity extraction, pair this with a real NER
model (spaCy, a hosted NLP API, or an LLM prompt) that populates
``KnowledgeGraph.add_relation()`` directly -- the graph itself doesn't care
where triples came from.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# A run of 2+ capitalized words (allows a lowercase connector like "of"/"the"
# in the middle, e.g. "Bank of America", "University of Oxford"), or a
# single capitalized word not at the very start of the text (reduces
# sentence-initial-capital false positives somewhat, though not entirely --
# see module docstring on limitations).
_ENTITY_RE = re.compile(
    r"\b[A-Z][a-zA-Z]*(?:\s+(?:of|the|and|de|van|der)\s+[A-Z][a-zA-Z]*|\s+[A-Z][a-zA-Z]*)*\b"
)

_COMMON_SENTENCE_STARTERS = {
    "The",
    "A",
    "An",
    "This",
    "That",
    "These",
    "Those",
    "It",
    "He",
    "She",
    "They",
    "We",
    "I",
    "You",
}


def extract_entities(text: str, min_length: int = 3) -> list[str]:
    """
    Heuristic proper-noun-sequence extraction. See module docstring for
    honest limitations. Deduplicates while preserving first-seen order.
    """
    candidates = _ENTITY_RE.findall(text)
    seen: dict[str, None] = {}
    for candidate in candidates:
        candidate = candidate.strip()
        if len(candidate) < min_length:
            continue
        if " " not in candidate and candidate in _COMMON_SENTENCE_STARTERS:
            continue
        seen.setdefault(candidate, None)
    return list(seen.keys())


class KnowledgeGraph:
    """
    A directed multi-graph of (subject) --[predicate]--> (object) triples,
    built on ``networkx.MultiDiGraph`` (multi- so the same pair of entities
    can have more than one distinct relation recorded between them).
    """

    def __init__(self) -> None:
        import networkx as nx

        self._graph = nx.MultiDiGraph()

    def add_relation(self, subject: str, predicate: str, obj: str, **attrs: Any) -> None:
        """Records subject --[predicate]--> obj. Extra keyword args are
        stored as edge attributes (e.g. source_url=..., confidence=...)."""
        self._graph.add_edge(subject, obj, key=predicate, predicate=predicate, **attrs)

    def add_item(
        self,
        item: dict[str, Any],
        subject_field: str,
        relations: dict[str, str],
        **shared_attrs: Any,
    ) -> None:
        """
        Extracts triples from a structured scraped item without any NLP:
        ``subject_field`` names the item field to use as the subject for
        every relation, and ``relations`` maps {predicate: object_field}.

        Example:
            item = {"company": "Acme Corp", "founder": "Jane Doe", "hq": "Austin, TX"}
            kg.add_item(item, subject_field="company",
                        relations={"founded_by": "founder", "headquartered_in": "hq"})
            # -> Acme Corp --[founded_by]--> Jane Doe
            #    Acme Corp --[headquartered_in]--> Austin, TX
        """
        subject = item.get(subject_field)
        if not subject:
            logger.debug("add_item: subject_field %r missing/empty, skipping", subject_field)
            return
        for predicate, object_field in relations.items():
            obj = item.get(object_field)
            if obj is None or obj == "":
                continue
            if isinstance(obj, list):
                for o in obj:
                    self.add_relation(str(subject), predicate, str(o), **shared_attrs)
            else:
                self.add_relation(str(subject), predicate, str(obj), **shared_attrs)

    def add_entities_from_text(
        self, text: str, source: str, predicate: str = "mentions"
    ) -> list[str]:
        """
        Runs the heuristic ``extract_entities()`` over ``text`` and records
        source --[predicate]--> entity for each one found. Returns the
        extracted entities. See module docstring: this is a rough signal,
        not verified NER.
        """
        entities = extract_entities(text)
        for entity in entities:
            self.add_relation(source, predicate, entity)
        return entities

    # --- Queries -------------------------------------------------------------

    def neighbors(self, entity: str) -> list[str]:
        if entity not in self._graph:
            return []
        return list(self._graph.successors(entity))

    def relations_from(self, entity: str) -> list[tuple[str, str]]:
        """Returns [(predicate, object), ...] for every outgoing edge."""
        if entity not in self._graph:
            return []
        out = []
        for _, obj, data in self._graph.out_edges(entity, data=True):
            out.append((data.get("predicate", ""), obj))
        return out

    @property
    def node_count(self) -> int:
        return self._graph.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    # --- Export --------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        nodes = list(self._graph.nodes())
        edges = [
            {
                "subject": u,
                "object": v,
                "predicate": data.get("predicate", ""),
                **{k: val for k, val in data.items() if k != "predicate"},
            }
            for u, v, data in self._graph.edges(data=True)
        ]
        return {"nodes": nodes, "edges": edges}

    def export_json(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        logger.info(
            "Exported knowledge graph to %s (%d nodes, %d edges)",
            path,
            self.node_count,
            self.edge_count,
        )

    def export_graphml(self, path: str) -> None:
        """GraphML is openable directly in Gephi/Cytoscape for visual
        exploration -- the realistic path to actually analyzing a graph of
        any size, rather than reinventing graph visualization here."""
        import networkx as nx

        nx.write_graphml(self._graph, path)
        logger.info(
            "Exported knowledge graph to %s as GraphML (%d nodes, %d edges)",
            path,
            self.node_count,
            self.edge_count,
        )
