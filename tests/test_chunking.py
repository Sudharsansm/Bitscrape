"""Heading-aware chunking for retrieval."""

from __future__ import annotations

import re

import pytest

from bitscrape.chunking import Chunk, chunk_markdown, estimate_tokens

DOC = """# Guide

Welcome to the guide. It has several parts.

## Install

Run the installer. It takes a minute.

```bash
pip install bitscrape
bitscrape --version
```

### Linux

Use your package manager. Make sure Python 3.10 or later is present.

## Usage

Call the function. Pass a URL. Read the result.

| a | b |
| --- | --- |
| 1 | 2 |
"""


def test_heading_paths_follow_the_document_structure():
    chunks = chunk_markdown(DOC, max_tokens=200, overlap=0)
    paths = [c.heading for c in chunks]
    assert "Guide" in paths
    assert "Guide > Install" in paths
    assert "Guide > Install > Linux" in paths
    assert "Guide > Usage" in paths
    linux = next(c for c in chunks if c.heading.endswith("Linux"))
    assert "package manager" in linux.text


def test_every_chunk_respects_max_tokens_and_indexes_are_sequential():
    text = "# T\n\n" + "\n\n".join(
        f"Paragraph {i}. " + "Lorem ipsum dolor sit amet. " * 12 for i in range(30)
    )
    chunks = chunk_markdown(text, max_tokens=120, overlap=16)
    assert len(chunks) > 5
    assert all(c.tokens <= 120 for c in chunks), max(c.tokens for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_no_content_is_lost():
    sentences = [f"Sentence number {i} says something unique{i}." for i in range(60)]
    text = "# Doc\n\n" + " ".join(sentences)
    chunks = chunk_markdown(text, max_tokens=64, overlap=8)
    joined = " ".join(c.text for c in chunks)
    for s in sentences:
        assert re.search(re.escape(s.split()[-1]), joined), s


def test_code_block_is_kept_whole_when_it_fits():
    chunks = chunk_markdown(DOC, max_tokens=200, overlap=0)
    holder = [c for c in chunks if "pip install bitscrape" in c.text]
    assert len(holder) == 1
    assert "```bash\npip install bitscrape\nbitscrape --version\n```" in holder[0].text


def test_oversize_code_block_is_split_with_valid_fences():
    code = "\n".join(f"line_{i} = {i}" for i in range(200))
    chunks = chunk_markdown(f"# C\n\n```python\n{code}\n```", max_tokens=100, overlap=0)
    assert len(chunks) > 2
    for c in chunks:
        assert c.text.count("```") % 2 == 0, "every piece must open and close its fence"
        assert c.tokens <= 100


def test_overlap_repeats_context_within_a_section_only():
    text = (
        "# A\n\n"
        + " ".join(f"alpha{i}." for i in range(80))
        + "\n\n# B\n\n"
        + " ".join(f"beta{i}." for i in range(80))
    )
    chunks = chunk_markdown(text, max_tokens=60, overlap=12)
    a = [c for c in chunks if c.heading == "A"]
    b = [c for c in chunks if c.heading == "B"]
    assert len(a) > 1 and len(b) > 1
    assert "…" in a[1].text  # overlap marker inside a section
    assert "…" not in b[0].text  # first chunk of a section has no borrowed context
    assert "alpha" not in b[0].text  # and never leaks the previous section


def test_source_title_and_metadata_are_attached():
    chunks = chunk_markdown(
        DOC, source="https://x.example/g", title="Guide", metadata={"lang": "en"}
    )
    assert all(c.source == "https://x.example/g" and c.title == "Guide" for c in chunks)
    d = chunks[0].to_dict()
    assert d["lang"] == "en" and d["heading"] == "Guide" and d["source"].startswith("https")
    assert chunks[1].with_context().startswith(chunks[1].heading)


def test_skipped_heading_levels_and_leading_text():
    chunks = chunk_markdown("Preamble text.\n\n### Deep\n\nBody.\n\n# Top\n\nMore.", overlap=0)
    assert chunks[0].heading == "" and "Preamble" in chunks[0].text
    assert chunks[1].heading_path == ["Deep"]
    assert chunks[2].heading_path == ["Top"]


def test_custom_token_counter_is_used():
    chunks = chunk_markdown(
        "# T\n\n" + "word " * 100, max_tokens=10, overlap=0, count_tokens=lambda s: len(s.split())
    )
    assert all(len(c.text.split()) <= 10 for c in chunks)


def test_empty_input_and_validation():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []
    with pytest.raises(ValueError):
        chunk_markdown("x", max_tokens=4)
    with pytest.raises(ValueError):
        chunk_markdown("x", max_tokens=50, overlap=50)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("a" * 400) == 100


def test_chunk_dataclass_defaults():
    c = Chunk(text="x", index=0)
    assert c.heading == "" and c.metadata == {}
