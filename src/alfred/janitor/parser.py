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


#: A leading YAML frontmatter block. Masking must SKIP it: fences are a body
#: construct and YAML has none, so a record with ``` inside a field value (a
#: ``janitor_note`` explaining fences, say) must not be read as opening a block
#: that swallows the document.
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)", re.DOTALL)

#: An opening (or closing) fence line: three-or-more backticks or tildes,
#: optionally indented, optionally carrying an info string. CommonMark allows
#: both markers and requires the closer to be the SAME character and at least
#: as long — which is what lets #57's grown fences (````csv containing a ```
#: line) survive without the inner run terminating the block early.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(?P<marker>`{3,}|~{3,})(?P<info>[^\r\n]*)$")

#: A matched inline code span on ONE line. Deliberately not multi-line: a stray
#: backtick must not be able to mask links across a paragraph the way an
#: unclosed fence otherwise could.
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?P<body>[^\r\n]*?)(?P=ticks)")


def _blank_out(text: str) -> str:
    """Replace every non-newline character with a space.

    Blanking rather than deleting keeps line and column offsets identical, so
    masking cannot shift anything a later pass measures — and the masked text
    stays readable in a debugger as "a hole exactly this shape".
    """
    return "".join("\n" if ch == "\n" else " " for ch in text)


def mask_code_regions(text: str) -> str:
    """Blank out fenced blocks and inline code spans in a record's BODY.

    #61. Since #57 the ingest path fences uploaded file content into record
    bodies, so ``[[something]]`` sitting in a bank CSV is DATA, not a reference.
    A scanner that cannot tell the difference manufactures findings about text
    nobody linked — and, because a LINK001 work-list drives the drip campaign's
    IRREVERSIBLE removal branch, those findings become automated deletions out
    of a code block.

    **The frontmatter block is left verbatim.** Fences are a body construct;
    YAML has none. Masking it would let backticks inside a field value silence
    the record's real relationship links.

    **An UNCLOSED fence is treated as UNFENCED** — the opposite of the general
    bias, and deliberately. An unterminated fence is a document defect, and if
    it swallowed everything below it a single stray ``` would switch LINK001 off
    for the rest of the file with no signal that it had. One defect must not
    mask every other finding; silent under-reporting is the failure mode this
    codebase rules against everywhere else, and a false positive is noise a
    human triages.
    """
    m = _FRONTMATTER_BLOCK_RE.match(text)
    prefix, body = (m.group(0), text[m.end():]) if m else ("", text)

    lines = body.split("\n")
    out: list[str] = list(lines)
    open_at: int | None = None       # index of the OPENING fence line
    marker: str = ""

    for i, line in enumerate(lines):
        hit = _FENCE_LINE_RE.match(line)
        if open_at is None:
            if hit:
                open_at, marker = i, hit.group("marker")
            continue
        # Inside a block: a closer is the SAME character, at least as long, and
        # carries no info string. Anything else is content.
        if (
            hit
            and hit.group("marker")[0] == marker[0]
            and len(hit.group("marker")) >= len(marker)
            and not hit.group("info").strip()
        ):
            for j in range(open_at, i + 1):
                out[j] = _blank_out(lines[j])
            open_at, marker = None, ""

    # Fence still open at EOF: the block never closed, so nothing was masked —
    # `out` already holds the original lines for that range. Stated explicitly
    # because "we simply don't mask" IS the fail-safe behaviour, not an
    # oversight, and a future edit that "fixes" it by masking to EOF would
    # reintroduce exactly the hiding this guards against.
    if open_at is not None:
        for j in range(open_at, len(lines)):
            out[j] = lines[j]

    masked_body = "\n".join(out)
    # Inline spans, applied to what survived — a fenced region is already
    # blank, so this cannot double-mask.
    masked_body = _INLINE_CODE_RE.sub(lambda mm: _blank_out(mm.group(0)), masked_body)
    return prefix + masked_body


def count_masked_wikilinks(text: str) -> int:
    """How many wikilink occurrences :func:`mask_code_regions` removes.

    The per-link exclusion is silent by design — there is no useful place to
    log each one. The SWEEP-level count is what keeps "the scanner is
    fence-aware" distinguishable from "the scanner stopped finding anything",
    so the scanner reports this per run.
    """
    return len(WIKILINK_RE.findall(text)) - len(
        WIKILINK_RE.findall(mask_code_regions(text))
    )


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

    **Fenced and inline-code regions are masked first** (#61). This is the seam
    the awareness belongs at, rather than inside :func:`extract_wikilinks`,
    because the exclusion is a property of DOCUMENT TEXT and that primitive is
    also called on things that are not documents — a single ``janitor_note``
    value, and a synthesized frontmatter string. Masking there would be
    meaningless at best and actively wrong at worst: a ``janitor_note`` that
    quotes backticks would stop contributing to the subtraction below, and the
    multiset would then fail to cancel real links. Keeping the primitive
    fence-BLIND also leaves #60's drip agreement pins measuring exactly what
    they were written against.
    """
    targets = extract_wikilinks(mask_code_regions(raw_text))
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
