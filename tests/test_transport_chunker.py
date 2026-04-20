"""Tests for ``alfred.transport.utils.chunk_for_telegram``.

Covers every branch of the chunker the brief daemon depends on:
short text (one chunk), multi-paragraph under limit (one chunk),
multi-paragraph over limit (multiple chunks preserving paragraph
breaks), long single paragraph (sentence split fallback), very long
single sentence (hard-wrap), empty body.
"""

from __future__ import annotations

from alfred.transport.utils import chunk_for_telegram


def test_empty_text_returns_single_empty_chunk() -> None:
    assert chunk_for_telegram("") == [""]


def test_short_text_is_one_chunk() -> None:
    text = "Short brief content."
    assert chunk_for_telegram(text) == [text]


def test_multi_paragraph_under_limit_stays_one_chunk() -> None:
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    result = chunk_for_telegram(text, max_chars=3800)
    assert result == [text]


def test_multi_paragraph_over_limit_splits_on_blank_lines() -> None:
    # Three 2000-char paragraphs. Limit 3800 → 2+1 split.
    p1 = "A" * 2000
    p2 = "B" * 2000
    p3 = "C" * 2000
    text = f"{p1}\n\n{p2}\n\n{p3}"
    chunks = chunk_for_telegram(text, max_chars=3800)

    # At least two chunks — we can't pack all three into one.
    assert len(chunks) >= 2
    # Every chunk must respect the limit.
    for chunk in chunks:
        assert len(chunk) <= 3800
    # The content round-trips when rejoined on the split separator.
    joined = "\n\n".join(chunks)
    # Chunks are paragraph-cohesive: no paragraph is split across
    # chunks, so rejoining recovers the exact original.
    assert joined == text


def test_long_single_paragraph_splits_on_sentence_boundary() -> None:
    """A single paragraph exceeding the limit splits at ``. `` boundaries."""
    # Three ~1500-char sentences, no paragraph breaks.
    sentence = "This is a sentence with enough length to fill the buffer. " * 30
    sentence = sentence.strip()
    # Now append two more, still as one paragraph.
    para = f"{sentence} {sentence} {sentence}"
    # limit at 2000 so chunking is forced.
    chunks = chunk_for_telegram(para, max_chars=2000)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 2000


def test_single_sentence_longer_than_limit_hard_wraps() -> None:
    """No paragraph breaks, no sentence terminators — hard wrap kicks in."""
    text = "abc" * 2000  # 6000 chars, no space, no punctuation
    chunks = chunk_for_telegram(text, max_chars=1000)

    # Every chunk is under the limit.
    for chunk in chunks:
        assert len(chunk) <= 1000
    # Reassembling yields the original.
    assert "".join(chunks) == text


def test_chunker_preserves_paragraph_break_within_chunk() -> None:
    """Paragraphs that fit into the same chunk stay joined with ``\\n\\n``."""
    p1 = "First paragraph."
    p2 = "Second paragraph."
    p3 = "Third paragraph."
    text = f"{p1}\n\n{p2}\n\n{p3}"
    chunks = chunk_for_telegram(text, max_chars=3800)
    assert chunks == [text]
    assert "\n\n" in chunks[0]


def test_chunker_with_mixed_sizes_packs_greedily() -> None:
    """Small paragraphs pack together; a big paragraph rolls to its own chunk."""
    small = "Small paragraph."
    big = "X" * 3500
    text = f"{small}\n\n{small}\n\n{big}\n\n{small}"
    chunks = chunk_for_telegram(text, max_chars=3800)

    # The small paragraphs before the big one should pack; the big one
    # gets its own chunk; the trailing small joins whatever's left.
    for chunk in chunks:
        assert len(chunk) <= 3800
    # Every input paragraph appears somewhere.
    joined = "\n\n".join(chunks)
    assert joined.count(small) == 3
    assert big in joined
