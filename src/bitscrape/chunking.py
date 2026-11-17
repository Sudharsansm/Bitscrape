"""
Chunking for retrieval
======================

Embedding a whole page as one vector blurs it; embedding fixed-width slices
cuts sentences and code in half. :func:`chunk_markdown` splits Markdown along
its own structure instead:

* sections follow **headings**, and each chunk carries its ``heading_path``
  (``"Guide > Install > Linux"``) so a retrieved fragment still says where it
  came from;
* within a section, paragraphs/list/table blocks are packed up to
  ``max_tokens``; an oversize block is split on sentence boundaries, and a
  fenced **code block is kept whole** whenever it fits;
* consecutive chunks of a section overlap by ``overlap`` tokens so an answer
  straddling a boundary is still retrievable.

Token counts are estimated (about 4 characters per token) unless you pass a
real tokenizer as ``count_tokens``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(`{3,}|~{3,})")
_SENTENCE = re.compile(r"(?<=[.!?。！？])\s+(?=[A-Z0-9\"'(\[#*`])")


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars/token, never zero for non-empty text."""
    return max(1, (len(text) + 3) // 4) if text else 0


@dataclass
class Chunk:
    """One retrievable piece of a document."""

    text: str
    index: int
    heading_path: list[str] = field(default_factory=list)
    tokens: int = 0
    source: str = ""
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def heading(self) -> str:
        return " > ".join(self.heading_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "index": self.index,
            "heading": self.heading,
            "heading_path": list(self.heading_path),
            "tokens": self.tokens,
            "source": self.source,
            "title": self.title,
            **self.metadata,
        }

    def with_context(self) -> str:
        """Chunk text prefixed with its heading path -- better embedding input."""
        return f"{self.heading}\n\n{self.text}" if self.heading else self.text


def _blocks(markdown: str) -> list[tuple[str, str]]:
    """Split Markdown into ``(kind, text)`` blocks; kind is ``heading``,
    ``code`` or ``text``. Fenced code is never split at blank lines."""
    out: list[tuple[str, str]] = []
    lines = markdown.splitlines()
    buf: list[str] = []
    i = 0

    def flush() -> None:
        text = "\n".join(buf).strip()
        buf.clear()
        if text:
            out.append(("text", text))

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            code = [line]
            i += 1
            while i < len(lines) and not lines[i].startswith(marker):
                code.append(lines[i])
                i += 1
            if i < len(lines):
                code.append(lines[i])
                i += 1
            out.append(("code", "\n".join(code)))
            continue
        if _HEADING.match(line):
            flush()
            out.append(("heading", line))
        elif not line.strip():
            flush()
        else:
            buf.append(line)
        i += 1
    flush()
    return out


def _split_oversize(text: str, max_tokens: int, count: Callable[[str], int]) -> list[str]:
    """Split one block that exceeds ``max_tokens`` on sentence, then line, then
    word boundaries."""
    if count(text) <= max_tokens:
        return [text]
    pieces: list[str] = []
    for unit in _SENTENCE.split(text) if "\n" not in text else text.split("\n"):
        if count(unit) > max_tokens:
            words = unit.split(" ")
            cur: list[str] = []
            for w in words:
                if cur and count(" ".join([*cur, w])) > max_tokens:
                    pieces.append(" ".join(cur))
                    cur = []
                cur.append(w)
            if cur:
                pieces.append(" ".join(cur))
        else:
            pieces.append(unit)
    # re-pack neighbouring small pieces
    packed: list[str] = []
    cur_txt = ""
    for pc in pieces:
        cand = f"{cur_txt}\n{pc}" if cur_txt else pc
        if cur_txt and count(cand) > max_tokens:
            packed.append(cur_txt)
            cur_txt = pc
        else:
            cur_txt = cand
    if cur_txt:
        packed.append(cur_txt)
    return packed


def _overlap_tail(text: str, overlap: int, count: Callable[[str], int]) -> str:
    """The last ~``overlap`` tokens of ``text``, cut at a word boundary."""
    if overlap <= 0 or not text:
        return ""
    words = text.split()
    tail: list[str] = []
    for w in reversed(words):
        tail.insert(0, w)
        if count(" ".join(tail)) >= overlap:
            break
    return " ".join(tail) if len(tail) < len(words) else ""


def chunk_markdown(
    markdown: str,
    *,
    max_tokens: int = 512,
    overlap: int = 48,
    source: str = "",
    title: str = "",
    count_tokens: Callable[[str], int] = estimate_tokens,
    metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """Split ``markdown`` into heading-aware chunks of at most ``max_tokens``.

    Args:
        max_tokens: upper bound per chunk (a code block larger than this is the
            one exception -- it is split on line boundaries).
        overlap: tokens of the previous chunk repeated at the start of the next
            *within the same section* (0 disables).
        source / title / metadata: copied onto every chunk.
        count_tokens: token counter (default: ~4 chars/token estimate).
    """
    if max_tokens < 8:
        raise ValueError("max_tokens must be >= 8")
    if overlap < 0 or overlap >= max_tokens:
        raise ValueError("overlap must be in [0, max_tokens)")

    chunks: list[Chunk] = []
    path: list[str] = []
    section: list[str] = []  # blocks belonging to the current heading

    def emit(section_blocks: list[str], heading_path: list[str]) -> None:
        if not section_blocks:
            return
        budget = max_tokens - overlap if overlap else max_tokens
        current = ""
        previous = ""
        pieces: list[str] = []
        for blk in section_blocks:
            if blk.lstrip().startswith(("```", "~~~")) and count_tokens(blk) > budget:
                pieces.extend(_split_code(blk, budget, count_tokens))
            else:
                pieces.extend(_split_oversize(blk, budget, count_tokens))
        for piece in pieces:
            cand = f"{current}\n\n{piece}" if current else piece
            if current and count_tokens(cand) > budget:
                _add(current, previous, heading_path)
                previous = current
                current = piece
            else:
                current = cand
        if current:
            _add(current, previous, heading_path)

    def _add(text: str, previous: str, heading_path: list[str]) -> None:
        body = text
        tail = _overlap_tail(previous, overlap, count_tokens)
        if tail:
            body = f"{tail} … {text}" if not text.startswith("```") else f"{tail}\n\n{text}"
        chunks.append(
            Chunk(
                text=body,
                index=len(chunks),
                heading_path=list(heading_path),
                tokens=count_tokens(body),
                source=source,
                title=title,
                metadata=dict(metadata or {}),
            )
        )

    for kind, text in _blocks(markdown):
        if kind == "heading":
            emit(section, path)
            section = []
            m = _HEADING.match(text)
            assert m is not None
            level, name = len(m.group(1)), m.group(2).strip()
            path = [*path[: level - 1], name]
        else:
            section.append(text)
    emit(section, path)
    return chunks


def _split_code(block: str, budget: int, count: Callable[[str], int]) -> list[str]:
    """Split a fenced block on line boundaries, re-fencing each piece."""
    lines = block.splitlines()
    opener = lines[0]
    closer = opener[: len(opener) - len(opener.lstrip("`~"))] or "```"
    body = lines[1:-1] if len(lines) > 1 and lines[-1].startswith(closer[:3]) else lines[1:]
    out: list[str] = []
    cur: list[str] = []
    for ln in body:
        trial = "\n".join([opener, *cur, ln, closer])
        if cur and count(trial) > budget:
            out.append("\n".join([opener, *cur, closer]))
            cur = []
        cur.append(ln)
    if cur:
        out.append("\n".join([opener, *cur, closer]))
    return out
