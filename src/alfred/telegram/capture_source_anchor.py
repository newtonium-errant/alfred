"""Capture-mode source/author anchoring + within-session cross-link helpers.

Hypatia's ``capture`` sessions can declare an anchor in the opening turn
("I'm reading Meditations by Marcus Aurelius") that this module turns
into structured links:

    * A ``source/<Title>.md`` record (created if missing, resolved if not)
    * An ``author/<Lastname>.md`` record (created if missing, resolved if not)
    * Session frontmatter populated with ``source: [[source/...]]`` and
      ``author: [[author/...]]`` for downstream extraction.

A second opening pattern — ``This continues from [[note/X]]`` — sets
``continues_from`` on the session record.

The third surface is **re-encounter detection**: at structured-summary
render time, a recency-capped scan over prior records mentioning the
session's source / author / topic terms surfaces 0-5 prior records as
a ``### Re-encounters`` section.

Design notes:
    * Scope discipline — within-session cross-linking only; cross-source
      cross-linking is deferred to v2.
    * Re-encounter scan is recency-bounded (``RE_ENCOUNTER_SCAN_CAP``)
      to bound perf on large vaults.
    * All resolver calls go through ``alfred.vault.ops`` with the
      ``scope="hypatia"`` kwarg so the create-allowlist gate runs.
    * Existing free-text ``author:`` fields on legacy source records
      are tolerated — the resolver checks for both wikilink and bare-
      string matches when deciding whether the source already has an
      author anchor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from alfred.vault import ops

from .utils import get_logger

log = get_logger(__name__)


# --- Constants -----------------------------------------------------------

#: Recency cap on re-encounter scans. Bound vault scan cost; surface the
#: top 5 of however many candidates land below this ceiling.
RE_ENCOUNTER_SCAN_CAP: int = 50

#: Max number of re-encounters rendered into the Structured Summary.
RE_ENCOUNTER_RENDER_MAX: int = 5

#: Minimum shared substantive tokens for a within-session cross-link.
#: Two notes share at least this many non-stopword tokens (3+ chars)
#: in their TITLES → wikilink each other. Threshold of 2 keeps the
#: cross-link conservative; 1 would over-link, 3+ would under-link.
CROSS_LINK_MIN_SHARED_TOKENS: int = 2

#: Minimum token length to count as "substantive". Filters out
#: prepositions / particles that survive stopword filtering.
CROSS_LINK_MIN_TOKEN_LEN: int = 3

#: Substantive-token stopword list. Curated from common English filler
#: words that survive the min-len filter. Not an exhaustive NLP list —
#: just the words that, in practice, create false-positive cross-links
#: ("And X", "About Y" linking unrelated notes).
CROSS_LINK_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from", "into", "onto", "upon",
    "about", "after", "before", "above", "below", "between",
    "this", "that", "these", "those", "then", "than", "thus",
    "what", "when", "where", "while", "which",
    "have", "has", "had", "his", "her", "their", "its", "our",
    "are", "was", "were", "been", "being",
    "you", "your", "yours", "they", "them",
    "one", "two", "three", "first", "second", "third",
    "not", "but", "yet", "all", "any", "some", "many", "much",
    "very", "more", "most", "less", "least",
    "out", "off", "down", "over", "under", "back", "again",
    "like", "just", "even", "also", "only", "still",
})

#: Name-suffix tokens that should NOT be treated as a person's last name.
#: When the trailing whitespace-separated token of an author string is
#: one of these (case-insensitive, with or without trailing period), the
#: previous token is used as the lookup key instead.
NAME_SUFFIXES: frozenset[str] = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v",  # generational
    "phd", "md", "esq",                   # honorifics
})


#: Name particles that belong to the surname rather than between
#: given-name and surname. Per Phase 1 Q1 (ratified): preserve these in
#: the surname token so ``Fiore dei Liberi`` resolves to filename
#: ``Fiore dei Liberi``, not ``Liberi`` or ``Fiore, dei Liberi``.
#: Particles are detected case-insensitively but case-preserved in
#: output (``van`` stays ``van``, ``Van`` stays ``Van``).
#:
#: Source: standard European/Western particle list (van, von, de, der,
#: dei, di, della, du, le, la, mac, mc, fitz, ben, ibn, al). Phase 1
#: starts with the four explicitly named in the brief; expansion is
#: a one-line dict-extension when new cases surface.
NAME_PARTICLES: frozenset[str] = frozenset({
    # Brief explicitly names these.
    "van", "de", "dei", "von", "der",
    # Adjacent particles in the same family — defensively included
    # so adding them later doesn't require a separate commit cycle.
    # Each one is a standard European name particle that belongs to
    # the surname phrase (medieval Italian "dei", Dutch "van der",
    # German "von der", French "de", Spanish "de la").
    "del", "della", "di", "du", "la", "le",
})


# --- Opening pattern parsing ---------------------------------------------

# Source-type shape inference patterns (Phase 2 deliverable #3, 2026-05-17).
#
# Each pattern matches a "I'm <verb> X by Y" opening turn and infers the
# source_type from the verb. The shape table:
#
#   reading           → book (default; or article/substack if title is a URL)
#   watching          → video (default; or lecture if "at a lecture")
#   listening to      → podcast
#   in conversation /  → conversation (no required author author)
#     talking with
#   at a lecture by   → lecture (speaker is the "author")
#
# All patterns share the same {title, author} capture-group contract for
# downstream parsing. ``author`` is optional for some shapes (conversation
# is just "with Person" — interlocutor as author); the source-shape
# inference is the new layer, not a re-architecture of the prior
# {title, author} surface.
#
# The unified parser :func:`parse_opening_anchors` tries the patterns in
# order; first match wins. Order is most-specific-first so e.g.
# "at a lecture by Hadot" matches the lecture pattern before falling
# through to reading.
#
# WARN-2 hardening (2026-05-17): all patterns now anchor at start-of-text
# (``\A\s*``) instead of word-boundary (``\b``). The pre-hardening
# patterns would match a bare verb mid-phrase, e.g.
# ``"I'm reading about watching paint dry"`` → WATCHING bare-verb
# branch matched at the ``watching`` offset → source_type=video
# (WRONG). Sentence-start anchoring eliminates this entire class of
# false positives. Trade-off: greeted openings like ``"Hi Hypatia,
# I'm reading X"`` no longer match — operator must lead with the verb
# pattern. Andrew's actual openings are direct ("I'm reading X by Y"
# as the first sentence), so the trade-off is acceptable.

# "I'm reading X by Y" / variants — BOOK source type (or article/
# substack when title contains URL / Substack hint, detected at infer
# time).
_READING_PATTERN = re.compile(
    r"""
    (?ix)
    \A\s*
    (?:
        i'?m\s+(?:currently\s+)?(?:reading|working\s+through|going\s+through)
      | currently\s+reading
      | i\s+am\s+(?:currently\s+)?reading
      | reading
    )
    \s+
    (?P<title>.+?)
    \s+by\s+
    (?P<author>[A-Z][^.!?\n]+?)
    (?=[.!?\n]|$)
    """,
    re.VERBOSE,
)

# "I'm watching X by Y" / "I'm watching X" — VIDEO source type.
# Author optional (many videos are channel-only, not byline-attributed).
_WATCHING_PATTERN = re.compile(
    r"""
    (?ix)
    \A\s*
    (?:
        i'?m\s+(?:currently\s+)?watching
      | currently\s+watching
      | i\s+am\s+(?:currently\s+)?watching
      | watching
    )
    \s+
    (?P<title>.+?)
    (?:\s+by\s+(?P<author>[A-Z][^.!?\n]+?))?
    (?=[.!?\n]|$)
    """,
    re.VERBOSE,
)

# "I'm listening to X by Y" / variants — PODCAST source type.
_LISTENING_PATTERN = re.compile(
    r"""
    (?ix)
    \A\s*
    (?:
        i'?m\s+(?:currently\s+)?listening\s+to
      | currently\s+listening\s+to
      | i\s+am\s+(?:currently\s+)?listening\s+to
      | listening\s+to
    )
    \s+
    (?P<title>.+?)
    (?:\s+by\s+(?P<author>[A-Z][^.!?\n]+?))?
    (?=[.!?\n]|$)
    """,
    re.VERBOSE,
)

# "I'm in conversation with X about Y" / "I'm talking with X about Y" /
# "talking to X" — CONVERSATION source type. Author = interlocutor.
# Title = topic ("about Y") OR interlocutor name (when no topic given).
_CONVERSATION_PATTERN = re.compile(
    r"""
    (?ix)
    \A\s*
    (?:
        i'?m\s+in\s+conversation\s+with
      | i\s+am\s+in\s+conversation\s+with
      | i'?m\s+talking\s+(?:with|to)
      | i\s+am\s+talking\s+(?:with|to)
      | talking\s+(?:with|to)
    )
    \s+
    (?P<author>[A-Z][^.!?\n]+?)
    (?:\s+about\s+(?P<title>.+?))?
    (?=[.!?\n]|$)
    """,
    re.VERBOSE,
)

# "I'm at a lecture by X" / "I'm at X's lecture" — LECTURE source type.
# Speaker is the "author"; title is the lecture topic if given.
_LECTURE_PATTERN = re.compile(
    r"""
    (?ix)
    \A\s*
    (?:
        i'?m\s+at\s+a\s+lecture\s+by
      | i\s+am\s+at\s+a\s+lecture\s+by
      | at\s+a\s+lecture\s+by
    )
    \s+
    (?P<author>[A-Z][^.!?\n]+?)
    (?:\s+on\s+(?P<title>.+?))?
    (?=[.!?\n]|$)
    """,
    re.VERBOSE,
)


# Pattern → inferred source_type. Ordered tuples (pattern, inferred_type,
# pattern_name_for_logging). The unified parser tries these in order;
# first match wins. Most-specific patterns come first so e.g.
# "at a lecture by Hadot" matches LECTURE before falling through to
# READING.
_SHAPE_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (_LECTURE_PATTERN,      "lecture",      "lecture"),
    (_CONVERSATION_PATTERN, "conversation", "conversation"),
    (_LISTENING_PATTERN,    "podcast",      "listening"),
    (_WATCHING_PATTERN,     "video",        "watching"),
    (_READING_PATTERN,      "book",         "reading"),
)


# Hints that a "reading" title is actually an article/substack URL
# rather than a book title. When the title matches one of these, infer
# ``article`` instead of ``book`` for the source_type. The detection
# is conservative — these hints are presence-of-URL-fragment.
_ARTICLE_URL_HINTS: tuple[str, ...] = (
    "://",          # any URL
    ".com",         # bare domain
    ".org",
    ".net",
    ".substack.com",
    "/p/",          # Substack post path
)


def _refine_reading_source_type(title: str) -> str:
    """Refine the ``book`` default for reading-pattern matches when the
    title carries article/Substack URL hints.

    Reading → book (default), unless title has a URL fragment → article.
    Substack URLs are a sub-case of article (the locked plan calls them
    out separately, but ``article`` is the schema-layer type; ``substack``
    would be a sub-shape the SKILL teaches, not a separate type per the
    type-minimalism guardrail).
    """
    lowered = (title or "").lower()
    for hint in _ARTICLE_URL_HINTS:
        if hint in lowered:
            return "article"
    return "book"

# "This continues from [[note/X]]", "continuing from [[X]]",
# "continuation of [[X]]". Captures the wikilink target (group 1).
_CONTINUES_PATTERN = re.compile(
    r"""
    (?ix)
    \b
    (?:
        this\s+continues\s+from
      | continuing\s+from
      | continuation\s+of
    )
    \s+
    \[\[(?P<target>[^\]]+)\]\]
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class OpeningAnchors:
    """Parsed opening-turn anchors. All fields optional.

    ``source_type`` (Phase 2 deliverable #3, 2026-05-17) carries the
    inferred source shape from the opening-pattern verb:
    book / article / podcast / video / lecture / conversation.
    Empty string when no shape pattern matched.
    """

    title: str = ""
    author: str = ""
    continues_from: str = ""
    source_type: str = ""


def parse_opening_anchors(opening_text: str) -> OpeningAnchors:
    """Parse the first user turn for source/author/continues-from anchors.

    Returns an :class:`OpeningAnchors` with whatever was found. Empty
    strings denote "not detected". Multiple patterns can fire in a
    single text (a session can be both a continuation AND from a new
    source); they are independent regexes.

    Phase 2 deliverable #3 (2026-05-17): source-shape inference. The
    parser now tries multiple verb patterns (reading / watching /
    listening / conversation / lecture) and infers ``source_type``
    from whichever pattern matches first. See ``_SHAPE_PATTERNS`` for
    the ordered iteration; most-specific patterns are tried first so
    e.g. "at a lecture by Hadot" matches LECTURE before falling
    through to READING.

    Reading + URL-fragment-in-title → ``article`` (Substack sub-shape).
    Reading + plain title → ``book`` (default).
    """
    if not opening_text:
        return OpeningAnchors()

    title = ""
    author = ""
    continues_from = ""
    source_type = ""

    # Try shape patterns in order; first match wins. The pattern that
    # matched also drives the source_type inference.
    for pattern, inferred_type, _name in _SHAPE_PATTERNS:
        m = pattern.search(opening_text)
        if not m:
            continue
        # Title and author groups: the new patterns make ``author``
        # optional (videos / podcasts often have no byline; conversation
        # has interlocutor-as-author with title-as-topic optional;
        # lecture has speaker-as-author with topic optional). All
        # patterns expose ``title`` + ``author`` groups so the parsing
        # code is uniform — empty captures coerce to "".
        title_raw = m.groupdict().get("title") or ""
        author_raw = m.groupdict().get("author") or ""
        title = _clean_title(title_raw) if title_raw else ""
        author = _clean_author(author_raw) if author_raw else ""
        # Refine the ``book`` default for reading-pattern matches when
        # the title looks like a URL / Substack post (→ ``article``).
        if inferred_type == "book" and title:
            source_type = _refine_reading_source_type(title)
        else:
            source_type = inferred_type
        break  # first match wins

    continues_match = _CONTINUES_PATTERN.search(opening_text)
    if continues_match:
        continues_from = continues_match.group("target").strip()

    return OpeningAnchors(
        title=title,
        author=author,
        continues_from=continues_from,
        source_type=source_type,
    )


def _clean_title(raw: str) -> str:
    """Strip articles and trailing punctuation from a parsed title."""
    text = raw.strip().strip(",.;:")
    # Strip leading article so "The Iliad" becomes filename-friendly when
    # paired with case-normalisation; keep "The" in the display name via
    # frontmatter ``name``.
    return text


def _clean_author(raw: str) -> str:
    """Strip trailing punctuation from a parsed author string."""
    return raw.strip().strip(",.;:")


def derive_last_name(author_full: str) -> str:
    """Derive the lookup last-name from a full author string.

    Handles:
        * Plain ``"Marcus Aurelius"`` → ``"Aurelius"``
        * Suffixed ``"Foo Bar Jr."`` → ``"Bar"`` (suffix stripped)
        * Suffixed ``"Foo Bar III"`` → ``"Bar"``
        * Comma-form ``"Aurelius, Marcus"`` → ``"Aurelius"``
        * Single name ``"Aristotle"`` → ``"Aristotle"``

    Empty / whitespace-only input → ``""``.

    .. note:: This is the LEGACY last-name-only filename helper from the
        original 2026-05-16 morning ship. Phase 1's resolver overhaul
        uses :func:`derive_canonical_filename` instead (which returns
        ``"Aurelius, Marcus"`` form). ``derive_last_name`` is kept
        for: (a) backward-compat with the migration script reading
        legacy ``author/<lastname>.md`` files; (b) the lookup-key
        scan when resolving an author against pre-Phase-1 records.
    """
    text = (author_full or "").strip()
    if not text:
        return ""

    # Comma form ("Last, First [Middle]") — take everything before the comma.
    if "," in text:
        last = text.split(",", 1)[0].strip()
        if last:
            return last

    tokens = text.split()
    if not tokens:
        return ""

    # Walk backwards skipping suffix tokens.
    for token in reversed(tokens):
        normalized = token.rstrip(".").lower()
        if normalized in NAME_SUFFIXES:
            continue
        return token.rstrip(".")
    # Everything was a suffix — fall back to the original last token.
    return tokens[-1].rstrip(".")


def _strip_suffix_tokens(tokens: list[str]) -> list[str]:
    """Return ``tokens`` with trailing suffix tokens (Jr / Sr / III / PhD)
    removed. Mirror of the legacy ``derive_last_name`` suffix-walk."""
    out = list(tokens)
    while out:
        normalized = out[-1].rstrip(".").lower()
        if normalized in NAME_SUFFIXES:
            out.pop()
            continue
        break
    return out


def derive_canonical_filename(author_full: str) -> str:
    """Derive the canonical Hypatia filename stem from a full author string.

    Phase 1 Q1 heuristic-with-particle-preservation (ratified 2026-05-16):

      * ``"Marcus Aurelius"`` → ``"Aurelius, Marcus"`` (modern Western;
        default Lastname-comma-Firstname).
      * ``"Foo Bar Jr."`` → ``"Bar, Foo"`` (suffix stripped before swap).
      * ``"Fiore dei Liberi"`` → ``"Fiore dei Liberi"`` (medieval particle
        preserved in surname phrase; no comma-swap because the particle
        binds the multi-token surname to the given name).
      * ``"Aurelius, Marcus"`` → ``"Aurelius, Marcus"`` (already canonical
        form; pass-through).
      * ``"Aristotle"`` → ``"Aristotle"`` (single-name historical figure;
        canonical form is the name itself).

    Ambiguous cases (3+ tokens without particles, non-Western patterns,
    operator-corrected forms): the heuristic picks its best guess and
    auto-creates — there is NO clarifier-turn UI in Phase 1 (operator
    renames manually if wrong). See the resolver's TODO marker for the
    Phase 1.5 hook point.

    Empty / whitespace-only input → ``""``.
    """
    text = (author_full or "").strip()
    if not text:
        return ""

    # Comma form — already canonical; pass through (after normalising
    # whitespace). Don't try to reshape an operator-corrected form.
    if "," in text:
        return text  # operator-provided canonical form wins

    tokens = text.split()
    tokens = _strip_suffix_tokens(tokens)
    if not tokens:
        return ""
    if len(tokens) == 1:
        # Single-token historical figure (Aristotle, Plato, Fiore-the-
        # single-name-attribution). Canonical = the name itself.
        return tokens[0]

    # Particle detection — if any token (other than the first) is a
    # particle, we treat ALL tokens from the particle onward as the
    # surname phrase. ``Fiore dei Liberi`` → particle "dei" at index 1
    # → surname = "dei Liberi", first part = "Fiore" → preserve as-is
    # (no comma swap). This handles the "medieval" / "multi-part-
    # surname" cases the brief calls out.
    lowercased = [t.lower() for t in tokens]
    particle_idx: int | None = None
    for i, lt in enumerate(lowercased[1:], start=1):
        if lt in NAME_PARTICLES:
            particle_idx = i
            break

    if particle_idx is not None:
        # Particle present → preserve original form (no comma swap).
        # Andrew's lived example: ``Fiore dei Liberi``.
        return " ".join(tokens)

    # Default: modern Western Firstname Lastname → ``Lastname, Firstname``
    # form. Multiple given names ("John Stuart Mill") → all non-final
    # tokens become the firstname phrase.
    last = tokens[-1]
    rest = " ".join(tokens[:-1])
    # TODO Phase 1.5: clarifier-turn UX hook. For ambiguous patterns
    # (3+ tokens without particles, non-Western names, multi-part
    # given names) we currently just emit the heuristic best-guess.
    # The Phase 1.5 hook surfaces a Telegram clarifier ("I'll create
    # author/<proposed>.md — accept or override?") before commit;
    # operator's reply re-runs create with the chosen form. Wire here
    # by detecting len(tokens) >= 3 + no-particle and returning a
    # PendingAuthor sentinel for the orchestrator to handle.
    return f"{last}, {rest}"


def _normalize_lookup(text: str) -> str:
    """Lowercase + collapse whitespace for case-insensitive comparison.

    Used by the resolver's alias-scan to compare e.g. ``"Marcus Aurelius"``
    against an existing record's ``aliases: ["Marcus Aurelius",
    "Aurelius, Marcus"]`` list.
    """
    return " ".join((text or "").lower().split())


# --- Author resolution ---------------------------------------------------


@dataclass(frozen=True)
class AuthorRef:
    """Result of :func:`resolve_or_create_author`."""

    rel_path: str            # vault-relative path of the author record
    created: bool            # True if newly created, False if pre-existing
    ambiguous_paths: tuple[str, ...] = ()  # populated on disambiguation


def _scan_authors_by_alias(
    vault_path: Path, lookup_form: str,
) -> str | None:
    """Scan ``author/`` directory for a record matching ``lookup_form``.

    Lookup matches the normalised form of any string in:
      * the record's filename stem (without ``.md``)
      * the record's ``name`` frontmatter
      * any entry in the record's ``aliases`` frontmatter list

    Returns the vault-relative path of the first match, or ``None``.

    This handles three cases:
      1. Operator typed ``"Marcus Aurelius"`` and the record was created
         in canonical form ``author/Aurelius, Marcus.md`` with
         ``aliases: ["Marcus Aurelius"]`` — alias match.
      2. Operator typed ``"Aurelius"`` and the record's filename is
         ``Aurelius, Marcus`` — partial-token match on filename.
         (Defensive — operator may use short-form for known figures.)
      3. Pre-Phase-1 legacy records: ``author/Aurelius.md`` with
         ``name: Marcus Aurelius`` and no ``aliases`` field. The
         ``name`` match catches these so the resolver doesn't double-
         create a record post-migration setup but pre-migration-run.

    Scan cost is O(N) over ``author/*.md`` — fine for vaults with
    hundreds of authors. If author counts grow into the thousands, the
    natural extension is a name-indexed registry file.
    """
    author_dir = vault_path / "author"
    if not author_dir.exists():
        return None

    lookup_norm = _normalize_lookup(lookup_form)
    if not lookup_norm:
        return None

    for path in sorted(author_dir.glob("*.md")):
        rel = f"author/{path.name}"
        try:
            rec = ops.vault_read(vault_path, rel)
        except ops.VaultError:
            continue
        fm = rec.get("frontmatter") or {}

        # Filename stem (no .md).
        stem = path.stem
        if _normalize_lookup(stem) == lookup_norm:
            return rel

        # name frontmatter.
        name_field = str(fm.get("name") or "").strip()
        if name_field and _normalize_lookup(name_field) == lookup_norm:
            return rel

        # aliases frontmatter list.
        aliases_raw = fm.get("aliases")
        if isinstance(aliases_raw, list):
            for alias in aliases_raw:
                alias_str = str(alias or "").strip()
                if alias_str and _normalize_lookup(alias_str) == lookup_norm:
                    return rel

    return None


def resolve_or_create_author(
    vault_path: Path,
    author_full: str,
    *,
    scope: str = "hypatia",
) -> AuthorRef | None:
    """Resolve an author via heuristic-canonical-filename + alias scan,
    or create the record if no match.

    Phase 1 Q1 ratified (2026-05-16): the resolver no longer uses
    last-name-only filenames. It now:

      1. Derives the CANONICAL filename via
         :func:`derive_canonical_filename` (e.g. ``"Marcus Aurelius"``
         → ``"Aurelius, Marcus"``; ``"Fiore dei Liberi"`` →
         ``"Fiore dei Liberi"``).
      2. Checks ``author/<canonical>.md`` for direct filename match.
      3. Scans ``author/*.md`` via :func:`_scan_authors_by_alias` for
         a name / aliases match (handles legacy last-name-only
         filenames + operator short-forms).
      4. Falls back to creating ``author/<canonical>.md`` with
         ``aliases:`` carrying BOTH the canonical form AND the input
         form so future lookups in either shape resolve to the same
         record.

    Returns ``None`` when ``author_full`` is empty or canonical
    filename derivation yields nothing.

    No clarifier-turn UI in Phase 1 (Q1 Option A) — ambiguous /
    non-Western / multi-part names take the heuristic best-guess and
    auto-create. Operator renames manually if the heuristic guess is
    wrong. See the TODO marker in :func:`derive_canonical_filename`
    for the Phase 1.5 hook point.

    The ``ambiguous_paths`` field on :class:`AuthorRef` is retained
    for backward compat with the prior call site (same-last-name
    conflict detection) — but Phase 1's heuristic mostly removes the
    last-name collision shape, since canonical-form filenames are
    Lastname-comma-Firstname distinct.
    """
    if not author_full:
        return None

    canonical = derive_canonical_filename(author_full)
    if not canonical:
        return None

    canonical_rel = f"author/{canonical}.md"

    # 1. Direct canonical-filename match.
    if (vault_path / canonical_rel).exists():
        log.info(
            "talker.capture.author_canonical_match",
            canonical=canonical,
            input=author_full,
            rel_path=canonical_rel,
        )
        return AuthorRef(rel_path=canonical_rel, created=False)

    # 2. Alias / name / legacy-filename scan — catches pre-migration
    # records (``author/Aurelius.md`` with ``name: Marcus Aurelius``)
    # AND operator-short-form lookups against existing canonical
    # records. Try BOTH the original input AND the canonical form so
    # either spelling resolves.
    for lookup in (author_full, canonical):
        existing_rel = _scan_authors_by_alias(vault_path, lookup)
        if existing_rel:
            log.info(
                "talker.capture.author_alias_match",
                canonical=canonical,
                input=author_full,
                matched_via=lookup,
                rel_path=existing_rel,
            )
            return AuthorRef(rel_path=existing_rel, created=False)

    # 3. Create at canonical filename. Aliases carry both forms so
    # future "Marcus Aurelius" / "Aurelius, Marcus" / etc. lookups
    # resolve to the same record. Filename and canonical-form alias
    # may be the same string — dedup.
    aliases: list[str] = []
    for candidate in (author_full, canonical):
        if candidate and candidate not in aliases:
            aliases.append(candidate)

    try:
        result = ops.vault_create(
            vault_path,
            "author",
            canonical,
            set_fields={
                "name": author_full,
                "aliases": aliases,
                # ``status: active`` was stripped from the Phase 1
                # author template, but the existing _validate_status
                # tolerates absence; we leave it off so the writer
                # doesn't pin a status the template no longer defaults.
            },
            scope=scope,
        )
    except ops.VaultError as exc:
        log.warning(
            "talker.capture.author_create_failed",
            author=author_full,
            canonical=canonical,
            error=str(exc),
        )
        return None
    log.info(
        "talker.capture.author_created_canonical",
        canonical=canonical,
        input=author_full,
        rel_path=result["path"],
        aliases=aliases,
    )
    return AuthorRef(rel_path=result["path"], created=True)


# --- Source resolution ---------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """Result of :func:`resolve_or_create_source`."""

    rel_path: str
    created: bool
    author_wikilink: str = ""  # populated when we backfill author on a new record


def resolve_or_create_source(
    vault_path: Path,
    title: str,
    author_full: str = "",
    author_wikilink: str = "",
    *,
    scope: str = "hypatia",
    source_type: str = "",
) -> SourceRef | None:
    """Resolve a ``source/<Title>.md`` record or create it.

    When the source doesn't exist and ``author_wikilink`` is supplied,
    the new record carries ``author: <wikilink>``; existing records are
    NOT mutated (the resolver doesn't touch pre-2026-05-16 free-text
    author fields — backward compat).

    Phase 2 deliverable #3 (2026-05-17): ``source_type`` kwarg carries
    the inferred shape from the opening-pattern verb
    (book / article / podcast / video / lecture / conversation). When
    non-empty AND the record is being CREATED (not resolved to an
    existing record), the field lands in frontmatter so downstream
    SKILL-layer logic can dispatch on the shape (page-anchored
    observations for books, timestamps for AV, URL-anchored for
    articles, etc.). When the source already exists, the existing
    record's frontmatter is NOT mutated — operator-set values win.
    """
    if not title:
        return None

    rel_path = f"source/{title}.md"
    file_path = vault_path / rel_path

    if file_path.exists():
        return SourceRef(rel_path=rel_path, created=False)

    set_fields: dict[str, Any] = {"status": "active"}
    if author_wikilink:
        set_fields["author"] = author_wikilink
    elif author_full:
        # No wikilink resolved (e.g. ambiguous author) — keep free-text
        # for traceability, matches the legacy ``source`` records shape.
        set_fields["author"] = author_full
    # Phase 2 source-shape inference. Empty source_type is omitted from
    # frontmatter (no field at all) per the "intentionally left blank"
    # discipline — silent absence is meaningful (parser couldn't infer
    # a shape; operator can fill manually). Non-empty values land
    # on creation.
    if source_type:
        set_fields["source_type"] = source_type

    try:
        result = ops.vault_create(
            vault_path,
            "source",
            title,
            set_fields=set_fields,
            scope=scope,
        )
    except ops.VaultError as exc:
        log.warning(
            "talker.capture.source_create_failed",
            title=title,
            error=str(exc),
        )
        return None
    return SourceRef(
        rel_path=result["path"],
        created=True,
        author_wikilink=author_wikilink,
    )


# --- Combined resolver ---------------------------------------------------


@dataclass(frozen=True)
class ResolvedAnchors:
    """End-to-end result of :func:`resolve_session_anchors`."""

    source_wikilink: str = ""   # "[[source/<Title>]]" or ""
    author_wikilink: str = ""   # "[[author/<Last>]]" or ""
    continues_from: str = ""    # "[[<target>]]" or "" (passthrough; not parsed)
    source_created: bool = False
    author_created: bool = False
    author_ambiguous: bool = False


def resolve_session_anchors(
    vault_path: Path,
    opening_text: str,
    *,
    scope: str = "hypatia",
) -> ResolvedAnchors:
    """Top-level resolver: parse opening turn → resolve/create records.

    Wraps :func:`parse_opening_anchors` + author/source resolution into
    one call the orchestrator can fire once per session-close.

    Returns an empty :class:`ResolvedAnchors` when no patterns match —
    the orchestrator treats this as "no source anchor for this session".
    """
    parsed = parse_opening_anchors(opening_text)

    author_ref: AuthorRef | None = None
    if parsed.author:
        author_ref = resolve_or_create_author(
            vault_path, parsed.author, scope=scope,
        )

    author_wikilink = ""
    author_ambiguous = False
    if author_ref is not None and author_ref.rel_path:
        if author_ref.ambiguous_paths:
            author_ambiguous = True
            # Don't link an ambiguous author into the source — leave the
            # operator to disambiguate before the link is forged.
        else:
            author_wikilink = f"[[{author_ref.rel_path[:-3]}]]"

    source_ref: SourceRef | None = None
    if parsed.title:
        source_ref = resolve_or_create_source(
            vault_path,
            parsed.title,
            author_full=parsed.author,
            author_wikilink=author_wikilink,
            scope=scope,
            # Phase 2 deliverable #3 (2026-05-17): plumb inferred
            # source_type through. Empty string when parser didn't
            # match a shape pattern — resolver omits the field from
            # frontmatter in that case.
            source_type=parsed.source_type,
        )

    source_wikilink = ""
    if source_ref is not None and source_ref.rel_path:
        source_wikilink = f"[[{source_ref.rel_path[:-3]}]]"

    continues_from = ""
    if parsed.continues_from:
        continues_from = f"[[{parsed.continues_from}]]"

    return ResolvedAnchors(
        source_wikilink=source_wikilink,
        author_wikilink=author_wikilink,
        continues_from=continues_from,
        source_created=bool(source_ref and source_ref.created),
        author_created=bool(author_ref and author_ref.created and not author_ambiguous),
        author_ambiguous=author_ambiguous,
    )


# --- Within-session cross-link ------------------------------------------

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _title_tokens(title: str) -> set[str]:
    """Return the set of substantive lowercase tokens from a note title."""
    lowered = title.lower()
    raw = _TOKEN_SPLIT_RE.split(lowered)
    out: set[str] = set()
    for token in raw:
        if len(token) < CROSS_LINK_MIN_TOKEN_LEN:
            continue
        if token in CROSS_LINK_STOPWORDS:
            continue
        out.add(token)
    return out


def compute_peer_cross_links(
    note_paths_and_titles: list[tuple[str, str]],
) -> dict[str, list[str]]:
    """Return ``{rel_path: [peer_wikilink, ...]}`` for the within-session pass.

    Two notes get cross-linked when their titles share at least
    :data:`CROSS_LINK_MIN_SHARED_TOKENS` substantive tokens.

    Empty input → empty dict. A note with no qualifying peers → not
    present in the output dict (caller treats missing key as "no peers
    to link").
    """
    if not note_paths_and_titles:
        return {}

    tokenized: list[tuple[str, str, set[str]]] = [
        (rel_path, title, _title_tokens(title))
        for rel_path, title in note_paths_and_titles
    ]

    out: dict[str, list[str]] = {}
    for i, (rel_a, _title_a, tokens_a) in enumerate(tokenized):
        peers: list[str] = []
        for j, (rel_b, _title_b, tokens_b) in enumerate(tokenized):
            if i == j:
                continue
            if len(tokens_a & tokens_b) < CROSS_LINK_MIN_SHARED_TOKENS:
                continue
            # Strip ``.md`` for the wikilink form.
            peer_link = rel_b[:-3] if rel_b.endswith(".md") else rel_b
            peers.append(f"[[{peer_link}]]")
        if peers:
            out[rel_a] = peers
    return out


# --- Re-encounter detection ----------------------------------------------


@dataclass(frozen=True)
class ReEncounter:
    """One row of the ``### Re-encounters`` summary section."""

    rel_path: str
    name: str
    reason: str  # short tag: "source-anchor" / "author" / "topic:<token>"


def find_re_encounters(
    vault_path: Path,
    source_wikilink: str,
    author_wikilink: str,
    topic_terms: list[str],
    current_session_rel_path: str = "",
    *,
    scan_cap: int = RE_ENCOUNTER_SCAN_CAP,
    render_max: int = RE_ENCOUNTER_RENDER_MAX,
) -> list[ReEncounter]:
    """Find prior records sharing source / author / topic terms.

    Strategy:
        1. If ``source_wikilink`` given — vault_search for records whose
           frontmatter ``source`` field matches (wikilink form OR the
           bare title text for backward compat with free-text legacy
           records).
        2. If ``author_wikilink`` given — vault_search for records whose
           frontmatter ``author`` field matches similarly.
        3. For each ``topic_term``, vault_search for records mentioning
           it (substring grep). Terms shorter than
           ``CROSS_LINK_MIN_TOKEN_LEN`` are skipped.

    Results are deduped by rel_path, the current session is excluded,
    and the top ``render_max`` rows by file mtime (most-recent first)
    are returned.

    Empty result list is legal — caller renders ``(none)`` per
    ``feedback_intentionally_left_blank.md``.
    """
    candidates: dict[str, ReEncounter] = {}
    scan_cap_left = scan_cap

    def _try_add(path: str, name: str, reason: str) -> None:
        # Skip self, skip duplicates (first-write-wins so the most-specific
        # reason — source-anchor — takes precedence over later topic hits).
        if not path or path == current_session_rel_path:
            return
        if path in candidates:
            return
        candidates[path] = ReEncounter(
            rel_path=path,
            name=name or Path(path).stem,
            reason=reason,
        )

    # Source-anchor scan.
    if source_wikilink and scan_cap_left > 0:
        # Strip wikilink brackets for grep — match both ``[[source/X]]``
        # frontmatter form AND any free-text reference.
        bare = source_wikilink.strip("[]")
        try:
            hits = ops.vault_search(vault_path, grep_pattern=bare)
        except Exception as exc:  # noqa: BLE001
            log.info("talker.capture.re_encounter_search_failed",
                     pattern=bare, error=str(exc))
            hits = []
        for hit in hits[:scan_cap_left]:
            _try_add(hit.get("path", ""), hit.get("name", ""), "source-anchor")
        scan_cap_left = max(0, scan_cap - len(candidates))

    # Author-anchor scan.
    if author_wikilink and scan_cap_left > 0:
        bare = author_wikilink.strip("[]")
        try:
            hits = ops.vault_search(vault_path, grep_pattern=bare)
        except Exception as exc:  # noqa: BLE001
            log.info("talker.capture.re_encounter_search_failed",
                     pattern=bare, error=str(exc))
            hits = []
        for hit in hits[:scan_cap_left]:
            _try_add(hit.get("path", ""), hit.get("name", ""), "author")
        scan_cap_left = max(0, scan_cap - len(candidates))

    # Topic scan — one grep per term.
    for term in topic_terms or []:
        if scan_cap_left <= 0:
            break
        clean = term.strip()
        if len(clean) < CROSS_LINK_MIN_TOKEN_LEN:
            continue
        try:
            hits = ops.vault_search(vault_path, grep_pattern=clean)
        except Exception as exc:  # noqa: BLE001
            log.info("talker.capture.re_encounter_search_failed",
                     pattern=clean, error=str(exc))
            hits = []
        for hit in hits[:scan_cap_left]:
            _try_add(
                hit.get("path", ""),
                hit.get("name", ""),
                f"topic:{clean}",
            )
        scan_cap_left = max(0, scan_cap - len(candidates))

    if not candidates:
        return []

    # Order by file mtime DESC (most-recent first); fall back to path
    # alphabetically if mtimes are missing or equal.
    def _mtime(rel: str) -> float:
        try:
            return (vault_path / rel).stat().st_mtime
        except OSError:
            return 0.0

    ordered = sorted(
        candidates.values(),
        key=lambda r: (-_mtime(r.rel_path), r.rel_path),
    )
    return ordered[:render_max]


def render_re_encounters_section(rows: list[ReEncounter]) -> str:
    """Render the ``### Re-encounters`` section markdown body (no header).

    Empty rows → ``"(none)"`` per :doc:`feedback_intentionally_left_blank`.
    Each row is one bullet: ``- [[<path-no-md>]] — <reason>``.
    """
    if not rows:
        return "(none)"
    lines: list[str] = []
    for row in rows:
        path_no_md = row.rel_path[:-3] if row.rel_path.endswith(".md") else row.rel_path
        lines.append(f"- [[{path_no_md}]] — {row.reason}")
    return "\n".join(lines)


# --- Re-encounter source-body append (Phase 2 deliverable #4, 2026-05-17) -


# Section headers used by the re-encounter append. ``## Observations
# During`` is where per-encounter dated subsections accumulate;
# ``## Permanent Notes spawned`` (or a fallback) marks the end of the
# observations section in the canonical Phase 2 source template.
_OBSERVATIONS_DURING_HEADING: str = "## Observations During"
_PERM_NOTES_SPAWNED_HEADING: str = "## Permanent Notes spawned"


def _find_h2_section_start(body: str, heading: str) -> int:
    """Return the start index of a line-anchored H2 heading in ``body``,
    or -1 if not found.

    WARN-1 hardening fix (2026-05-17). The pre-hardening code used
    ``body.find(heading)`` which searches for the substring at any
    offset. False-match surface: an H3 heading like ``### Observations
    During Yesterday`` contains the substring ``## Observations During``
    at offset+1 (because ``### Foo`` = ``#`` + ``## Foo``). A
    substring-find would lock onto that and corrupt subsequent
    section-bounded operations.

    This helper enforces line-anchored detection:
      * The heading must start at byte 0 of the body OR be preceded by
        a newline character.
      * The heading must be followed by end-of-line (newline or
        end-of-body) — no extra ``#`` characters after the ``## Foo``
        match (which would indicate H3+).

    Pattern matches the migration script's regex shape (per
    ``scripts/migrate_2026_05_16_meditations_zettels.py``:
    ``re.compile(r"##\\s+Permanent\\s+Notes\\s+spawned\\s*\\n...")``).
    """
    search_from = 0
    while True:
        idx = body.find(heading, search_from)
        if idx == -1:
            return -1
        # Line-anchored: must be at body start OR preceded by newline.
        at_line_start = (idx == 0) or (body[idx - 1] == "\n")
        # Line-anchored end: the heading must be followed by end-of-line
        # (or end-of-body). NOT followed by another ``#`` character —
        # that would indicate an H3+ heading (e.g. ``## Foo`` followed
        # by ``#`` → actually ``### Foo`` if the prefix overlap held).
        # In practice the after-byte should be ``\n`` or ``\r`` or
        # whitespace. The pre-existing usage relies on ``\n``-followed.
        after_idx = idx + len(heading)
        ends_cleanly = (
            after_idx == len(body)
            or body[after_idx] in ("\n", "\r")
            or body[after_idx].isspace()
        )
        if at_line_start and ends_cleanly:
            return idx
        # False match (e.g. inside an H3 heading) — keep searching.
        search_from = idx + 1


def _render_observations_for_session(
    topics: list[str],
    key_insights: list[str],
    session_rel_path: str,
) -> str:
    """Render the per-session observation bullets for the ``###
    YYYY-MM-DD`` subsection body.

    Phase 2 MVP shape: bullet list of topics + key_insights from the
    structured summary, followed by a backlink to the originating
    session record. Future iterations may enrich this with anchor-
    annotated quotes from derived zettels — but for the first ship,
    topics + insights + backref is enough scaffolding to validate the
    re-encounter flow.

    Empty topics + insights → just the session backref. The bullet
    list is empty in that case (per the "intentionally left blank"
    discipline — explicit emptiness is legal; the backref still ties
    the source to the encounter).
    """
    lines: list[str] = []
    for topic in topics or []:
        lines.append(f"- {topic}")
    for insight in key_insights or []:
        lines.append(f"- {insight}")
    if not lines:
        lines.append("- (no topics or insights surfaced this session)")
    # Backref to the originating session record.
    session_no_md = (
        session_rel_path[:-3] if session_rel_path.endswith(".md")
        else session_rel_path
    )
    lines.append(f"")
    lines.append(f"_From [[{session_no_md}]]_")
    return "\n".join(lines)


def _build_re_encounter_rewriter(
    today_iso: str,
    observation_body: str,
) -> Callable[[str], str]:
    """Build a body_rewriter callable for the re-encounter append.

    Behaviour:
      * If ``## Observations During`` section is missing from the body
        (older source records pre-dating Phase 2 template), no-op —
        return body unchanged. The locked plan says "subsequent
        capture sessions on the same source APPEND a new dated
        subsection under ``## Observations During``" — the section
        must exist. Phase 2 deliverable #1 ships the template with
        the section; operator-synced vaults pick it up.
      * If ``### <today>`` subsection already exists within Observations
        During, append observation_body BELOW the existing subsection
        body (no duplicate ``### <today>`` heading). Same-day idempotent.
      * Otherwise, create the ``### <today>`` subsection at the END of
        the Observations During section (just before ``## Permanent
        Notes spawned`` or, if that section is also missing, just
        before the next ``# `` or ``## `` heading; if there's no next
        heading, append at the end of the section / file).

    The rewriter is pure: takes a body string, returns a new body
    string. Idempotency lives in the same-day-detection branch.
    """
    today_heading = f"### {today_iso}"

    def _rewriter(body: str) -> str:
        # Locate Observations During section — line-anchored detection
        # so an H3 heading like ``### Observations During Yesterday``
        # doesn't false-match (WARN-1 hardening fix, 2026-05-17).
        obs_idx = _find_h2_section_start(body, _OBSERVATIONS_DURING_HEADING)
        if obs_idx == -1:
            # Section missing — pre-Phase-2 template or operator-edited
            # body. No-op rather than write to an arbitrary location.
            return body

        # Find the end of the Observations During section. The section
        # ends at the next H1 or H2 heading (NOT H3 — ### <date> is a
        # sub-heading within the section).
        section_start = obs_idx + len(_OBSERVATIONS_DURING_HEADING)
        section_end = _find_next_top_heading(body, section_start)
        section_body = body[section_start:section_end]

        # Check for an existing ### <today> subsection within the
        # section body.
        today_idx = section_body.find(today_heading)
        if today_idx != -1:
            # Idempotent same-day: append the new observation bullets
            # within the existing ### <today> subsection. The
            # subsection body runs from the heading line's end to the
            # next ### or section boundary.
            today_start = today_idx + len(today_heading)
            # End of THIS subsection = next ### heading OR section_end.
            next_subsection = section_body.find("\n### ", today_start)
            if next_subsection == -1:
                subsection_end = len(section_body)
            else:
                subsection_end = next_subsection
            existing_subsection_body = section_body[today_start:subsection_end]
            # Strip trailing newlines, append new bullets + a blank
            # line separator. The existing bullets stay untouched.
            new_subsection_body = (
                existing_subsection_body.rstrip("\n")
                + "\n\n"
                + observation_body
                + "\n"
            )
            new_section_body = (
                section_body[:today_start]
                + new_subsection_body
                + section_body[subsection_end:]
            )
        else:
            # First encounter today: create a new ### <today> subsection
            # at the END of the Observations During section.
            # Trim trailing whitespace from the section body before
            # appending; preserve any existing per-encounter subsections.
            new_subsection = (
                f"\n\n{today_heading}\n\n{observation_body}\n"
            )
            new_section_body = section_body.rstrip("\n") + new_subsection
            # Re-establish the trailing-newline boundary so the next
            # H1/H2 heading isn't glued to the new subsection.
            new_section_body = new_section_body + "\n"

        return body[:section_start] + new_section_body + body[section_end:]

    return _rewriter


def _find_next_top_heading(body: str, start_idx: int) -> int:
    """Return the index of the next H1 (``# ``) or H2 (``## ``) heading
    line at or after ``start_idx``, or ``len(body)`` if none.

    Used by the re-encounter rewriter to bound the Observations During
    section. H3 (``### ``) and deeper headings are NOT bounds — they're
    subsections within the H2 section.

    A heading line must start at the beginning of a line (after a
    newline) and have exactly 1 or 2 ``#`` characters followed by a
    space.
    """
    i = start_idx
    while i < len(body):
        # Find next newline.
        nl_idx = body.find("\n", i)
        if nl_idx == -1:
            return len(body)
        # Inspect the line immediately after.
        line_start = nl_idx + 1
        if line_start >= len(body):
            return len(body)
        # H1 or H2 (NOT H3 or deeper).
        if body[line_start:line_start + 2] == "# ":
            return line_start
        if body[line_start:line_start + 3] == "## ":
            return line_start
        # NOTE-2 hardening fix (2026-05-17): removed dead
        # ``body[start:start+3] == "## " and body[start:start+4] != "## "``
        # block — the conjunction is impossible (a 3-char slice equaling
        # ``"## "`` means body[start:start+4] is at minimum 4 chars,
        # not the 3-char ``"## "``). The H2 detection happens on the
        # canonical check above; the dead block was a no-op ``pass``.
        i = line_start
    return len(body)


def _build_permanent_notes_rewriter(
    zettel_wikilink: str,
) -> Callable[[str], str]:
    """Build a body_rewriter callable that appends a wikilink to the
    source's ``## Permanent Notes spawned`` section.

    Behaviour:
      * If the wikilink already exists in the section (any form —
        leading dash, no dash, etc.), no-op — return body unchanged
        (idempotent).
      * If ``## Permanent Notes spawned`` section is missing from the
        body (pre-Phase-2 source), no-op — return body unchanged.
      * Otherwise append ``- <wikilink>`` to the section, between the
        existing content and the next H1/H2 heading.

    The wikilink-presence check is conservative: it looks for the
    bare wikilink form ``[[zettel/Title]]`` anywhere within the
    section body — catches dash-prefixed and operator-edited forms
    alike. This means re-runs are idempotent even if the operator
    manually rewrote a bullet to include extra annotation.
    """
    bare_link = zettel_wikilink.strip()

    def _rewriter(body: str) -> str:
        # Locate Permanent Notes spawned section — line-anchored
        # detection (WARN-1 hardening fix, 2026-05-17). The
        # pre-hardening ``body.find(heading)`` would false-match an
        # H3 like ``### Permanent Notes spawned Yesterday`` at offset+1.
        perm_idx = _find_h2_section_start(body, _PERM_NOTES_SPAWNED_HEADING)
        if perm_idx == -1:
            # Section missing — no-op. Conservative behaviour matches
            # the re-encounter rewriter: don't write to an arbitrary
            # location on operator-curated bodies.
            return body

        # Find section bounds. End of section = next H1/H2 heading.
        section_start = perm_idx + len(_PERM_NOTES_SPAWNED_HEADING)
        section_end = _find_next_top_heading(body, section_start)
        section_body = body[section_start:section_end]

        # Idempotency check — wikilink already present anywhere in
        # the section body.
        if bare_link in section_body:
            return body

        # Append ``- <wikilink>`` to the end of the section body.
        # Preserve the section's trailing newline boundary so the
        # next H1/H2 heading isn't glued to the new line.
        new_section_body = (
            section_body.rstrip("\n") + f"\n- {bare_link}\n"
        )
        # Re-establish a blank line between this section's content
        # and the next heading. The right shape: section ends with
        # exactly one blank line before the next H1/H2.
        if not new_section_body.endswith("\n\n"):
            new_section_body = new_section_body + "\n"

        return body[:section_start] + new_section_body + body[section_end:]

    return _rewriter


def append_permanent_note_spawned(
    vault_path: Path,
    source_rel_path: str,
    zettel_wikilink: str,
    *,
    scope: str = "hypatia",
) -> bool:
    """Append a zettel wikilink to a source record's ``## Permanent
    Notes spawned`` section.

    Phase 2 deliverable #5 (2026-05-17). Per the locked plan's
    "Auto-maintenance behaviors" → #5: "when a ``zettel/`` is created
    with ``source:`` set, Hypatia appends ``- [[zettel/Title]]`` to the
    source's ``## Permanent Notes spawned``, idempotent."

    Idempotent — if the wikilink already exists in the section
    (any form, leading-dash or not), the call no-ops.

    Pre-Phase-2 source records (missing the ``## Permanent Notes
    spawned`` section) → no-op, returns False. Conservative
    behaviour matches the re-encounter helper.

    Returns True if the source body was updated; False on no-op
    (idempotent skip, missing section, missing file, or failure).
    Failure logged but never raised — capture-extract calls this
    best-effort post-zettel-create; a write error doesn't block
    the extraction flow.
    """
    rel_path = source_rel_path.lstrip("/")
    if rel_path.startswith("[[") and rel_path.endswith("]]"):
        rel_path = rel_path[2:-2]
    if not rel_path.endswith(".md"):
        rel_path = rel_path + ".md"
    if not (vault_path / rel_path).exists():
        log.info(
            "talker.capture.perm_notes_source_missing",
            source_rel_path=rel_path,
            zettel_wikilink=zettel_wikilink,
        )
        return False

    rewriter = _build_permanent_notes_rewriter(zettel_wikilink)
    # Read pre-state to detect idempotent no-op (rewriter returns body
    # unchanged when wikilink already present or section missing). We
    # use byte-equality of the resulting body to drive the return
    # value — the vault_edit call itself doesn't surface "no change".
    try:
        rec = ops.vault_read(vault_path, rel_path)
        pre_body = rec.get("body", "")
    except Exception:
        pre_body = ""

    try:
        ops.vault_edit(
            vault_path,
            rel_path,
            body_rewriter=rewriter,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.capture.perm_notes_append_failed",
            source_rel_path=rel_path,
            zettel_wikilink=zettel_wikilink,
            error=str(exc),
        )
        return False

    # Confirm whether the rewriter actually changed the body (idempotent
    # skip detection).
    try:
        rec = ops.vault_read(vault_path, rel_path)
        post_body = rec.get("body", "")
    except Exception:
        post_body = pre_body

    changed = post_body != pre_body
    if changed:
        log.info(
            "talker.capture.perm_notes_append_done",
            source_rel_path=rel_path,
            zettel_wikilink=zettel_wikilink,
        )
    else:
        log.info(
            "talker.capture.perm_notes_append_idempotent_skip",
            source_rel_path=rel_path,
            zettel_wikilink=zettel_wikilink,
        )
    return changed


def append_re_encounter_observation(
    vault_path: Path,
    source_rel_path: str,
    today_iso: str,
    topics: list[str],
    key_insights: list[str],
    session_rel_path: str,
    *,
    scope: str = "hypatia",
) -> bool:
    """Append today's observation bullets to a source record's
    ``## Observations During`` section.

    Phase 2 deliverable #4 (2026-05-17) — re-encounter source-body
    growth. Called from
    :func:`alfred.telegram.capture_batch.process_capture_session` when
    the capture session resolved to a PRE-EXISTING source record
    (``anchors.source_created=False``). First encounter on a fresh
    source (source_created=True) doesn't trigger this — the source
    has no prior observations to extend.

    Idempotency:
      * Same-day re-runs append observation bullets WITHIN the
        existing ``### <today>`` subsection (no duplicate heading).
      * Different-day captures get a new ``### <today>`` subsection.
      * Pre-Phase-2 source records (missing ``## Observations During``
        section) → no-op, returns False.

    Returns True if the source body was updated; False on no-op or
    failure (logged but never raised — failure isolated from the
    capture-batch orchestrator).
    """
    rel_path = source_rel_path.lstrip("/")
    if rel_path.startswith("[[") and rel_path.endswith("]]"):
        rel_path = rel_path[2:-2]
    if not rel_path.endswith(".md"):
        rel_path = rel_path + ".md"
    if not (vault_path / rel_path).exists():
        log.info(
            "talker.capture.re_encounter_source_missing",
            source_rel_path=rel_path,
        )
        return False

    observation_body = _render_observations_for_session(
        topics, key_insights, session_rel_path,
    )
    rewriter = _build_re_encounter_rewriter(today_iso, observation_body)
    try:
        ops.vault_edit(
            vault_path,
            rel_path,
            body_rewriter=rewriter,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.capture.re_encounter_append_failed",
            source_rel_path=rel_path,
            session_rel_path=session_rel_path,
            error=str(exc),
        )
        return False
    log.info(
        "talker.capture.re_encounter_append_done",
        source_rel_path=rel_path,
        session_rel_path=session_rel_path,
        today=today_iso,
    )
    return True


# --- Helpers -------------------------------------------------------------


def _normalize_for_compare(text: str) -> str:
    """Lowercase + collapse internal whitespace for case-insensitive compare."""
    return " ".join((text or "").lower().split())


__all__ = [
    "RE_ENCOUNTER_SCAN_CAP",
    "RE_ENCOUNTER_RENDER_MAX",
    "CROSS_LINK_MIN_SHARED_TOKENS",
    "CROSS_LINK_MIN_TOKEN_LEN",
    "CROSS_LINK_STOPWORDS",
    "NAME_SUFFIXES",
    # Phase 1 author-resolver overhaul (2026-05-16).
    "NAME_PARTICLES",
    "derive_canonical_filename",
    "OpeningAnchors",
    "ResolvedAnchors",
    "AuthorRef",
    "SourceRef",
    "ReEncounter",
    "parse_opening_anchors",
    "derive_last_name",
    "resolve_or_create_author",
    "resolve_or_create_source",
    "resolve_session_anchors",
    "compute_peer_cross_links",
    "find_re_encounters",
    "render_re_encounters_section",
    # Phase 2 re-encounter source-body append (2026-05-17).
    "append_re_encounter_observation",
    # Phase 2 Permanent Notes spawned auto-append (2026-05-17).
    "append_permanent_note_spawned",
]
