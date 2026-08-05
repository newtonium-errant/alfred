"""Frontmatter/body parsing and wikilink extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

# Embed pattern: ![[something.base#Section]] or ![[file]]
EMBED_RE = re.compile(r"!\[\[[^\]]+\]\]")

# KEN dynamic section markers
KEN_DYNAMIC_RE = re.compile(
    r"<!-- KEN:DYNAMIC -->.*?<!-- END KEN:DYNAMIC -->",
    re.DOTALL,
)


@dataclass
class VaultRecord:
    rel_path: str
    frontmatter: dict
    body: str
    record_type: str
    wikilinks: list[str] = field(default_factory=list)


def extract_wikilinks(text: str) -> list[str]:
    """Extract all wikilink targets from text (frontmatter + body).

    Targets are whitespace-normalized: YAML frontmatter folds long quoted
    strings across physical lines when a list item's wikilink exceeds the
    line width, so the raw regex capture can contain an embedded ``\\n``
    plus continuation indent. We collapse any internal whitespace run to a
    single space so such wrapped targets resolve the same as single-line
    wikilinks. Wikilinks never contain meaningful internal whitespace
    structure (Obsidian renders them as one token), so this is safe.
    """
    return [
        re.sub(r"\s+", " ", target).strip()
        for target in WIKILINK_RE.findall(text)
    ]


def decode_yaml_apostrophe(target: str) -> str:
    """Decode YAML single-quote-doubled apostrophes for lookup/comparison.

    YAML's single-quoted scalar form escapes a literal apostrophe by doubling
    it (``''``). A wikilink to ``[[constraint/Andrew's Note]]`` landing inside
    a single-quoted YAML scalar is captured by the extraction regex as
    ``Andrew''s`` — the YAML layer has not decoded yet at extraction time — so
    the target misses the on-disk file in stem-index lookups.

    Normalizing ``''`` → ``'`` resolves to the same file Obsidian would. Apply
    it to the LOOKUP/COMPARISON string only; the original raw target is
    preserved for user-visible messages so reviewers see what the source file
    actually contained.

    Safe in practice: filenames never contain literal ``''`` (Obsidian forbids
    it on most filesystems), so this cannot mask a real broken link. See the
    2026-04-30 categorization investigation: 252 of 761 LINK001 were this exact
    pattern.

    Lives here rather than in ``scanner`` because both the scanner's target
    resolution and this module's annotation subtraction need the SAME
    normalization — two spellings of "the same link" would make the
    subtraction below silently miss the 14 apostrophe-bearing note links in the
    live vault.
    """
    return target.replace("''", "'") if "''" in target else target


#: Frontmatter fields whose value is PROSE ABOUT the record rather than a
#: structural reference FROM it.
#:
#: ``janitor_note`` is the janitor's own annotation. Reading wikilinks out of it
#: creates a genuine SELF-FEEDING LOOP: the janitor writes "LINK001 — broken
#: wikilink [[person/Ghost]]" to explain a break, the next sweep extracts
#: ``person/Ghost`` from that very note, and reports a SECOND LINK001 on the
#: same record for the same underlying break. Measured on the 2026-06-25 vault
#: snapshot: 586 records carry a note quoting a wikilink, 855 quoted
#: occurrences in total — so a meaningful slice of the ~2,090 LINK001 headline
#: is the janitor reporting its own prose back to itself.
#:
#: Deliberately NARROW. Relationship fields (``source_a``, ``process``,
#: ``org``, ``project``, …) also carry wikilinks and those are REAL references
#: that must stay checked — a broken one there is a true finding. Only fields
#: that describe the record belong here.
ANNOTATION_FIELDS: frozenset[str] = frozenset({"janitor_note"})


def annotation_wikilinks(fm: dict) -> list[str]:
    """Wikilink targets quoted inside annotation-field prose."""
    out: list[str] = []
    for field in sorted(ANNOTATION_FIELDS):
        value = fm.get(field)
        if isinstance(value, str):
            out.extend(extract_wikilinks(value))
    return out


def structural_wikilinks(raw_text: str, fm: dict) -> list[str]:
    """Wikilinks the record actually REFERENCES — annotation prose removed.

    MULTISET subtraction, not a set difference, and that distinction is the
    whole correctness of it: a record whose body genuinely links
    ``[[person/Ghost]]`` AND whose note quotes the same target must keep ONE
    occurrence. A set difference would delete both and hide a real broken link;
    counting them both is the self-feeding loop. Removing exactly as many as
    the annotation contributed leaves precisely the real references.

    Order is preserved and the ORIGINAL target text is returned, so
    user-visible LINK001 messages still show what the source file contained.
    """
    targets = extract_wikilinks(raw_text)
    noise: dict[str, int] = {}
    for t in annotation_wikilinks(fm):
        key = decode_yaml_apostrophe(t)
        noise[key] = noise.get(key, 0) + 1
    if not noise:
        return targets

    kept: list[str] = []
    for t in targets:
        key = decode_yaml_apostrophe(t)
        if noise.get(key):
            noise[key] -= 1
            continue
        kept.append(t)
    return kept


def parse_file(vault_path: Path, rel_path: str) -> VaultRecord:
    """Parse a vault markdown file into a VaultRecord."""
    full_path = vault_path / rel_path
    raw_text = full_path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw_text)

    fm = dict(post.metadata)
    body = post.content
    record_type = fm.get("type", "")

    wikilinks = structural_wikilinks(raw_text, fm)

    return VaultRecord(
        rel_path=rel_path,
        frontmatter=fm,
        body=body,
        record_type=record_type,
        wikilinks=wikilinks,
    )


def stripped_body_length(body: str) -> int:
    """Return body length after stripping embeds, KEN dynamic sections, and whitespace."""
    text = EMBED_RE.sub("", body)
    text = KEN_DYNAMIC_RE.sub("", text)
    # Strip markdown headings that are just structural
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#") and len(stripped.lstrip("#").strip()) == 0:
            continue
        lines.append(stripped)
    return len("\n".join(lines).strip())
