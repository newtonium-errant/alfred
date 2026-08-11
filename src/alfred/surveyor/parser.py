"""Frontmatter/body parsing, wikilink extraction, embedding text builder."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

# #70 — IMPORTED, not re-derived. Delegating to janitor's masker rather than
# copying the rule follows the precedent ``drip.campaigns.unfenced_view``
# already set, and for its stated reason: two implementations of "is this
# fenced" is how they drift, and here the drift would be invisible (a wrong
# relationship written into the vault reads exactly like a right one).
#
# Deliberately NOT lifted to a shared module in this commit. The function
# carries hardening (the CRLF ``\r?`` form) whose reachability claim is
# pinned in ``tests/test_janitor_fence_awareness.py``, and moving it would
# put that pin, three janitor call sites and drip's delegation in the blast
# radius of a lane already carrying two other items. Cross-tool consumers
# reach THREE with this commit (drip, surveyor, distiller) — a lift to
# ``alfred/common`` is the right follow-up at the next consumer; flagged in
# the report rather than done mid-lane.
from alfred.janitor.parser import mask_code_regions

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

# Max chars for embedding text. Previous value (8_000) was based on a wrong
# assumption that nomic-embed-text exposes its 8192-token native context via
# Ollama's /api/embeddings endpoint. In practice Ollama's legacy embeddings
# endpoint caps context at 2048 tokens regardless of model metadata, and the
# /api/embed endpoint would require an options={"num_ctx": 8192} override this
# codepath doesn't currently send. Empirical probe (see surveyor.log around
# 2026-04-24T04:28 where a 31-file diff failed with HTTP 500 "the input length
# exceeds the context length"): 7_500 chars on code-dense session notes already
# busts the window; 6_000 chars embeds cleanly across sessions + synthesis +
# assumption records. Keeping 6_000 leaves headroom for token-dense content
# (wikilinks, code fences, non-English) that tokenizes worse than 3 chars/token.
# If we ever migrate to /api/embed + num_ctx=8192, this can grow back.
MAX_EMBEDDING_CHARS = 6_000

# Frontmatter keys to include in embedding text
EMBEDDING_FM_KEYS = ["type", "status", "name", "description", "intent", "source", "channel"]

# Frontmatter keys to exclude (links, dates, tags, machine fields)
EXCLUDE_FM_KEYS = [
    "tags", "alfred_tags", "relationships", "created", "updated", "date",
    "aliases", "cssclass", "cssclasses",
]


@dataclass
class VaultRecord:
    rel_path: str
    frontmatter: dict
    body: str
    record_type: str
    wikilinks: list[str] = field(default_factory=list)


def extract_wikilinks(text: str) -> list[str]:
    """Extract all wikilink targets from text (frontmatter + body)."""
    return WIKILINK_RE.findall(text)


def _coerce_record_type(raw) -> str:
    """Normalise a `type:` frontmatter value into a scalar string.

    Curator-generated records have occasionally been written with a one-element
    YAML block-list (`type:\n- contradiction`) instead of a scalar — pyyaml
    parses that as a Python `list`, and Milvus's VARCHAR `record_type` field
    rejects it with a schema-mismatch error that crashes the entire surveyor
    subprocess. Coerce defensively so a single malformed record can never
    stop the embed pipeline.
    """
    if raw is None:
        return "unknown"
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        # Take the first non-empty string; fall back to "unknown".
        for item in raw:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return "unknown"
    # Any other scalar (int/float/bool) — stringify so Milvus accepts it.
    return str(raw)


def parse_file(vault_path: Path, rel_path: str) -> VaultRecord:
    """Parse a vault markdown file into a VaultRecord."""
    full_path = vault_path / rel_path
    raw_text = full_path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw_text)

    fm = dict(post.metadata)
    body = post.content
    record_type = _coerce_record_type(fm.get("type"))

    # Extract wikilinks from the entire raw text (both frontmatter and body),
    # with fenced blocks and inline code spans MASKED first (#70).
    #
    # Since #57 the ingest path fences uploaded file content into record
    # bodies, and the SKILL pass now has all four agents writing fenced CSV
    # into bodies too — so the fenced population is growing. A ``[[thing]]``
    # inside a bank CSV is DATA, not a reference. Surveyor's links feed the
    # clusterer, the labeler and ``writer.write_relationships``, so an
    # unmasked read manufactures relationship signal out of code blocks and
    # then WRITES it back into the vault as wikilinks.
    #
    # ``mask_code_regions`` leaves the frontmatter block verbatim (fences are
    # a body construct; YAML has none), so passing the whole raw text is
    # correct — real frontmatter links survive.
    wikilinks = extract_wikilinks(mask_code_regions(raw_text))

    return VaultRecord(
        rel_path=rel_path,
        frontmatter=fm,
        body=body,
        record_type=record_type,
        wikilinks=wikilinks,
    )


def build_embedding_text(record: VaultRecord) -> str:
    """Build text blob for embedding. Includes type/status/name/description + body.
    Excludes link arrays, dates, tags."""
    parts: list[str] = []

    # Include select frontmatter fields
    for key in EMBEDDING_FM_KEYS:
        val = record.frontmatter.get(key)
        if val and isinstance(val, str):
            parts.append(f"{key}: {val}")

    # Include body
    if record.body:
        parts.append(record.body.strip())

    text = "\n".join(parts)

    # Truncate to max chars
    if len(text) > MAX_EMBEDDING_CHARS:
        text = text[:MAX_EMBEDDING_CHARS]

    return text
