"""
Tests for bitscrape.knowledge_graph.
"""

from __future__ import annotations

import json


from bitscrape.knowledge_graph import KnowledgeGraph, extract_entities


# ---------------------------------------------------------------------------
# extract_entities (heuristic, tested against its documented behavior --
# not claimed to be real NER)
# ---------------------------------------------------------------------------


def test_extracts_multi_word_proper_nouns():
    text = "Marie Curie won the Nobel Prize while working in Paris."
    entities = extract_entities(text)
    assert "Marie Curie" in entities
    assert "Nobel Prize" in entities
    assert "Paris" in entities


def test_extracts_names_with_connector_words():
    text = "She studied at the University of Oxford and later the Bank of America."
    entities = extract_entities(text)
    assert "University of Oxford" in entities
    assert "Bank of America" in entities


def test_deduplicates_preserving_order():
    text = "Apple makes phones. Apple also makes laptops."
    entities = extract_entities(text)
    assert entities.count("Apple") == 1


def test_filters_common_sentence_starters():
    text = "The company grew fast. The result was strong revenue."
    entities = extract_entities(text)
    assert "The" not in entities


def test_respects_min_length():
    text = "Al went to NY."
    entities = extract_entities(text, min_length=3)
    for e in entities:
        assert len(e) >= 3


def test_empty_text_returns_empty_list():
    assert extract_entities("") == []


def test_no_capitalized_words_returns_empty():
    assert extract_entities("the quick brown fox jumps") == []


# ---------------------------------------------------------------------------
# KnowledgeGraph -- structured (reliable) relation extraction
# ---------------------------------------------------------------------------


def test_add_relation_basic():
    kg = KnowledgeGraph()
    kg.add_relation("Acme Corp", "founded_by", "Jane Doe")
    assert kg.node_count == 2
    assert kg.edge_count == 1
    assert kg.neighbors("Acme Corp") == ["Jane Doe"]


def test_add_item_extracts_multiple_relations():
    kg = KnowledgeGraph()
    item = {"company": "Acme Corp", "founder": "Jane Doe", "hq": "Austin, TX"}
    kg.add_item(
        item,
        subject_field="company",
        relations={"founded_by": "founder", "headquartered_in": "hq"},
    )
    relations = dict(kg.relations_from("Acme Corp"))
    assert relations["founded_by"] == "Jane Doe"
    assert relations["headquartered_in"] == "Austin, TX"


def test_add_item_skips_missing_subject():
    kg = KnowledgeGraph()
    item = {"founder": "Jane Doe"}  # no "company" field
    kg.add_item(item, subject_field="company", relations={"founded_by": "founder"})
    assert kg.node_count == 0


def test_add_item_skips_missing_object_fields():
    kg = KnowledgeGraph()
    item = {"company": "Acme Corp"}  # no "founder" field
    kg.add_item(item, subject_field="company", relations={"founded_by": "founder"})
    assert kg.edge_count == 0


def test_add_item_handles_list_valued_fields():
    kg = KnowledgeGraph()
    item = {"company": "Acme Corp", "products": ["Widget", "Gadget"]}
    kg.add_item(item, subject_field="company", relations={"makes": "products"})
    relations = kg.relations_from("Acme Corp")
    objects = {obj for _, obj in relations}
    assert objects == {"Widget", "Gadget"}


def test_multiple_relations_between_same_pair_are_both_kept():
    """MultiDiGraph: two distinct predicates between the same two nodes
    shouldn't overwrite each other."""
    kg = KnowledgeGraph()
    kg.add_relation("Acme Corp", "founded_by", "Jane Doe")
    kg.add_relation("Acme Corp", "advised_by", "Jane Doe")
    relations = kg.relations_from("Acme Corp")
    assert len(relations) == 2
    assert ("founded_by", "Jane Doe") in relations
    assert ("advised_by", "Jane Doe") in relations


def test_neighbors_of_unknown_entity_returns_empty():
    kg = KnowledgeGraph()
    assert kg.neighbors("Nobody") == []
    assert kg.relations_from("Nobody") == []


# ---------------------------------------------------------------------------
# KnowledgeGraph -- text-based (heuristic) relation extraction
# ---------------------------------------------------------------------------


def test_add_entities_from_text_links_source_to_entities():
    kg = KnowledgeGraph()
    entities = kg.add_entities_from_text(
        "Marie Curie won the Nobel Prize.", source="article-123"
    )
    assert "Marie Curie" in entities
    relations = kg.relations_from("article-123")
    objects = {obj for _, obj in relations}
    assert "Marie Curie" in objects
    assert "Nobel Prize" in objects


def test_add_entities_from_text_custom_predicate():
    kg = KnowledgeGraph()
    kg.add_entities_from_text("Acme Corp", source="page-1", predicate="discusses")
    relations = kg.relations_from("page-1")
    assert relations[0][0] == "discusses"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_to_dict_shape():
    kg = KnowledgeGraph()
    kg.add_relation("A", "rel", "B")
    data = kg.to_dict()
    assert set(data["nodes"]) == {"A", "B"}
    assert data["edges"] == [{"subject": "A", "object": "B", "predicate": "rel"}]


def test_export_json_writes_valid_file(tmp_path):
    kg = KnowledgeGraph()
    kg.add_relation("A", "rel", "B")
    path = str(tmp_path / "graph.json")
    kg.export_json(path)

    with open(path) as f:
        data = json.load(f)
    assert set(data["nodes"]) == {"A", "B"}


def test_export_graphml_writes_valid_file(tmp_path):
    kg = KnowledgeGraph()
    kg.add_relation("A", "rel", "B")
    path = str(tmp_path / "graph.graphml")
    kg.export_graphml(path)

    import networkx as nx

    reloaded = nx.read_graphml(path)
    assert set(reloaded.nodes()) == {"A", "B"}


def test_edge_attrs_are_preserved_in_export():
    kg = KnowledgeGraph()
    kg.add_relation("A", "rel", "B", source_url="https://example.com")
    data = kg.to_dict()
    assert data["edges"][0]["source_url"] == "https://example.com"
