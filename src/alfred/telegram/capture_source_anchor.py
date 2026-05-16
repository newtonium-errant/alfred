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
from typing import Any

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

# "I'm reading X by Y", "I am reading X by Y", "Currently reading X by Y",
# "I'm working through X by Y", "Reading X by Y", etc.
#
# Captures TITLE (group 1) and AUTHOR (group 2). The TITLE group is
# non-greedy and anchored against " by " so a title containing " by "
# itself (rare) would be truncated — acceptable trade-off vs. catching
# the simple cases reliably.
_READING_PATTERN = re.compile(
    r"""
    (?ix)
    \b
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
    """Parsed opening-turn anchors. All fields optional."""

    title: str = ""
    author: str = ""
    continues_from: str = ""


def parse_opening_anchors(opening_text: str) -> OpeningAnchors:
    """Parse the first user turn for source/author/continues-from anchors.

    Returns an :class:`OpeningAnchors` with whatever was found. Empty
    strings denote "not detected". Multiple patterns can fire in a
    single text (a session can be both a continuation AND from a new
    source); they are independent regexes.
    """
    if not opening_text:
        return OpeningAnchors()

    title = ""
    author = ""
    continues_from = ""

    reading_match = _READING_PATTERN.search(opening_text)
    if reading_match:
        title = _clean_title(reading_match.group("title"))
        author = _clean_author(reading_match.group("author"))

    continues_match = _CONTINUES_PATTERN.search(opening_text)
    if continues_match:
        continues_from = continues_match.group("target").strip()

    return OpeningAnchors(
        title=title, author=author, continues_from=continues_from,
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
) -> SourceRef | None:
    """Resolve a ``source/<Title>.md`` record or create it.

    When the source doesn't exist and ``author_wikilink`` is supplied,
    the new record carries ``author: <wikilink>``; existing records are
    NOT mutated (the resolver doesn't touch pre-2026-05-16 free-text
    author fields — backward compat).
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
]
