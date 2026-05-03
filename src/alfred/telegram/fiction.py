"""Fiction project scaffolding for Hypatia (Phase 2.5).

Per ``project_hypatia_phase2_followups.md``, Hypatia gets fiction-
posture support via:
  * SKILL revision (prompt-tuner's lane) — natural-language "let's
    start a fiction project called X" detection + structure-framework
    reference
  * ``/fiction <title>`` slash command (this module + a handler in
    ``bot.py``) — explicit/deterministic path to the same scaffold

Both paths produce the same on-disk shape: a per-project directory
under ``draft/fiction/<slug>/`` with five element files + a
``characters/`` directory + a load-bearing ``continuity.md`` index
that Hypatia reads FIRST at session-open.

Why per-element files (not one big project doc):
  * Obsidian's links + outline pane work best when each element is
    its own document — wikilinks like ``[[draft/fiction/<slug>/world]]``
    open cleanly
  * Andrew can edit one element without seeing the rest
  * Future Hypatia rituals (e.g., "freeze world for chapter 3") can
    snapshot just one file rather than the whole project

Why ``continuity.md`` first:
  * It's the orientation index — Hypatia reads it at session-open to
    rebuild context. Other files go deeper; this is the front-page
  * Wikilinks point INTO the siblings (world / voice / structure /
    characters) so a single read primes Hypatia for the whole project
  * The "Recent canonical updates" section accumulates across sessions
    — it's the running log of confirmed plot/world changes

Idempotency:
  * If the directory already exists, do NOT overwrite. Return a
    structured "already_exists" result so the slash-command handler
    can produce an informative reply rather than silently clobbering
    the user's working manuscript

Path layout (the contract — shared with the SKILL natural-language
path; deviations break parity):

    draft/fiction/<slug>/
      continuity.md           # index Hypatia reads first
      story.md                # working manuscript
      structure.md            # framework placeholder
      world.md                # setting / world details
      voice.md                # voice + register
      characters/
        .gitkeep              # makes the empty dir survive in git
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Literal

import frontmatter
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------
#
# Same character class as ``telegram.session._TOPIC_SLUG_KEEP``
# (lowercase a-z + 0-9 + hyphen). Crucially, NO word cap — the user's
# fiction title is canonical and shouldn't be silently truncated.
# A 5-word cap (which session.py uses for derived session names)
# would turn ``"The Long Dark Night of the Soul"`` into
# ``"the-long-dark-night-of"`` and lose two title words. For fiction
# titles we keep the whole thing, capped only by a reasonable
# filesystem-safe length.

_FICTION_SLUG_DROP = re.compile(r"[^a-z0-9-]+")
_FICTION_SLUG_MAX_LEN = 80


def slug_from_title(title: str) -> str:
    """Derive a filename-safe slug from a fiction project title.

    Convention (matches Hypatia's substack-draft slug shape from
    ``2e21fc6``):

      * NFKD-normalize Unicode + strip combining marks so accented
        Latin characters (``café`` / ``über`` / ``naïve``) keep their
        ASCII base letters instead of being dropped wholesale
      * Lowercase
      * Whitespace collapsed to single hyphens
      * Non-alphanumeric characters dropped
      * Multiple consecutive hyphens collapsed to one
      * Leading / trailing hyphens stripped
      * Max length 80 characters (filesystem-safe + readable)
      * Empty / all-punctuation input returns ``"untitled-fiction"``
        so the scaffold always lands at a real path

    Examples:
      ``"The Glass Forest"`` → ``"the-glass-forest"``
      ``"Storm's End"`` → ``"storms-end"`` (apostrophe dropped)
      ``"50/50"`` → ``"5050"`` (slash dropped, digits collapse)
      ``"  multiple   spaces  "`` → ``"multiple-spaces"``
      ``"!!!"`` → ``"untitled-fiction"`` (no alphanumerics)
      ``"café"`` → ``"cafe"`` (NFKD: é → e + combining acute, then
        the combining mark is stripped, then the e survives the
        ASCII char-class filter)
      ``"über"`` → ``"uber"`` (same NFKD path — ü → u + combining
        diaeresis, mark stripped)
      ``"São Paulo"`` → ``"sao-paulo"``

    Non-Latin scripts (CJK, Cyrillic, Arabic, etc.) decompose to
    bare codepoints with no ASCII base — those still get dropped by
    the char-class filter and fall through to ``untitled-fiction``.
    NFKD only rescues accented Latin (the common case for English /
    European fiction titles); broader script support would need a
    transliteration library (defer until a real use case demands it).
    """
    if not isinstance(title, str):
        return "untitled-fiction"
    # NFKD-normalize first so "café" (1 codepoint é) becomes
    # "café" (e + combining acute), then strip combining marks
    # so the bare e survives the ASCII char-class filter below.
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = "".join(
        c for c in normalized if not unicodedata.combining(c)
    )
    s = ascii_only.strip().lower()
    if not s:
        return "untitled-fiction"
    # Collapse runs of whitespace to single hyphens BEFORE filtering
    # so "the long night" → "the-long-night" not "thelongnight".
    s = re.sub(r"\s+", "-", s)
    # Drop everything that isn't a-z / 0-9 / hyphen.
    s = _FICTION_SLUG_DROP.sub("", s)
    # Collapse runs of hyphens (post-filter) and strip leading/trailing.
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        return "untitled-fiction"
    if len(s) > _FICTION_SLUG_MAX_LEN:
        # Trim to the last full segment that fits — avoids cutting a
        # word mid-character. Worst case (no hyphens), hard-trim.
        truncated = s[:_FICTION_SLUG_MAX_LEN]
        last_hyphen = truncated.rfind("-")
        if last_hyphen >= _FICTION_SLUG_MAX_LEN // 2:
            s = truncated[:last_hyphen]
        else:
            s = truncated
        s = s.rstrip("-")
    return s or "untitled-fiction"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


_ScaffoldStatus = Literal["created", "already_exists"]


@dataclass
class ScaffoldResult:
    """Outcome of a ``scaffold_fiction_project`` call.

    Always populated — caller branches on ``status`` to decide
    between the success-reply and idempotent-skip-reply paths.
    """

    status: _ScaffoldStatus
    title: str
    slug: str
    rel_dir: str  # vault-relative directory path
    created_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    detail: str = ""


# ---------------------------------------------------------------------------
# Element files
# ---------------------------------------------------------------------------
#
# Five element types live as siblings under the project directory.
# Each has a frontmatter ``type`` of ``fiction-<element>`` so future
# vault tooling (a janitor rule, a brief section, etc.) can filter
# fiction records cleanly without grep-on-path heuristics.
#
# The order here matches the order they're referenced from
# ``continuity.md``'s wikilinks — keeping the "what does Hypatia see
# first" surface predictable across both the file write order and
# the index doc.


_ELEMENT_FILES: tuple[tuple[str, str, str], ...] = (
    # (element_kind, filename, body_template)
    (
        "story",
        "story.md",
        "# {title}\n\n_Working manuscript. Begin writing here._\n",
    ),
    (
        "structure",
        "structure.md",
        (
            "# Structure — {title}\n\n"
            "_Framework not yet chosen._\n\n"
            "When ready, pick from the structural frameworks documented\n"
            "in Hypatia's SKILL (3-act, Kishōtenketsu, jo-ha-kyū,\n"
            "Hero's Journey, Heroine's Journey, Save the Cat, Story\n"
            "Circle, Freytag's Pyramid, Seven-Point Structure) and replace\n"
            "this placeholder with the chosen framework's beat-by-beat\n"
            "outline for this project.\n"
        ),
    ),
    (
        "world",
        "world.md",
        (
            "# World — {title}\n\n"
            "_Setting, rules, geography, history, atmosphere._\n\n"
            "Capture as you build. Hypatia consults this file when\n"
            "you ask about world-grounded continuity.\n"
        ),
    ),
    (
        "voice",
        "voice.md",
        (
            "# Voice — {title}\n\n"
            "_Narrator register, tense, POV, sentence rhythm, vocabulary\n"
            "preferences, things to avoid._\n\n"
            "Hypatia treats this as the active voice contract for\n"
            "the project — copy edits + ghostwriting honor what's here.\n"
        ),
    ),
)


def _build_continuity_body(title: str, slug: str) -> str:
    """Render the ``continuity.md`` body. Frontmatter is added by caller."""
    return (
        f"# Continuity Index — {title}\n\n"
        f"**READ THIS FIRST.** This file is the orientation index for the\n"
        f"fiction project. Other files in this directory go deeper; this\n"
        f"is where Hypatia gets oriented at session-open.\n\n"
        f"## Synopsis\n"
        f"(to be filled)\n\n"
        f"## Characters\n"
        f"(none yet — see [[draft/fiction/{slug}/characters/]] as added)\n\n"
        f"## World\n"
        f"See [[draft/fiction/{slug}/world]]\n\n"
        f"## Voice\n"
        f"See [[draft/fiction/{slug}/voice]]\n\n"
        f"## Structure\n"
        f"See [[draft/fiction/{slug}/structure]] — framework not yet chosen\n\n"
        f"## Plot state\n"
        f"(no scenes written yet)\n\n"
        f"## Recent canonical updates\n"
        f"(empty — log each session's confirmed updates here)\n"
    )


def _make_frontmatter(
    *, element: str, title: str, slug: str, today: str,
) -> dict:
    """Build the frontmatter dict for one element file.

    Per the contract:
      ``type``: ``fiction-<element>``
      ``project``: human-readable title (NOT the slug)
      ``created``: ISO date
      ``fiction_slug``: slug, so Hypatia can navigate back to siblings
    """
    return {
        "type": f"fiction-{element}",
        "project": title,
        "created": today,
        "fiction_slug": slug,
    }


def _write_with_frontmatter(
    file_path: Path, frontmatter_dict: dict, body: str,
) -> None:
    """Write a markdown file with frontmatter + body, ending in newline."""
    post = frontmatter.Post(body, **frontmatter_dict)
    text = frontmatter.dumps(post)
    if not text.endswith("\n"):
        text += "\n"
    file_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def scaffold_fiction_project(
    vault_root: Path,
    title: str,
    *,
    today: str | None = None,
) -> ScaffoldResult:
    """Create a fiction project's directory + element files atomically.

    Args:
        vault_root: The instance's vault root (Hypatia's
            ``~/library-alexandria/``). The fiction project lands at
            ``vault_root/draft/fiction/<slug>/``.
        title: Human-readable project title. Used as the ``project``
            frontmatter field across all element files; slugified for
            the directory + ``fiction_slug`` field.
        today: ISO date string for the ``created`` field. Defaults
            to ``date.today().isoformat()``. Tests inject a fixed
            value for determinism.

    Returns:
        :class:`ScaffoldResult` describing the outcome:
          * ``status="created"`` — directory + files written; new
            project ready for Hypatia to populate
          * ``status="already_exists"`` — directory already on disk;
            no files written, no overwrites. Caller surfaces an
            informative reply instead of silently clobbering

    Per ``feedback_intentionally_left_blank.md``: the
    ``already_exists`` branch is an explicit "ran, did nothing on
    purpose" signal, distinct from a successful create. Idempotent
    by construction.
    """
    today_iso = today or _date.today().isoformat()
    slug = slug_from_title(title)
    rel_dir = f"draft/fiction/{slug}"
    project_dir = vault_root / "draft" / "fiction" / slug

    if project_dir.exists():
        log.info(
            "fiction.scaffold_skipped",
            slug=slug,
            title=title[:80],
            rel_dir=rel_dir,
            reason="directory_already_exists",
        )
        return ScaffoldResult(
            status="already_exists",
            title=title,
            slug=slug,
            rel_dir=rel_dir,
            detail=(
                f"Fiction project '{title}' already exists at {rel_dir}. "
                f"Use the existing files or pick a different title."
            ),
        )

    # Create the directory tree.
    project_dir.mkdir(parents=True, exist_ok=False)
    characters_dir = project_dir / "characters"
    characters_dir.mkdir(parents=False, exist_ok=False)
    # ``.gitkeep`` so the empty characters/ directory survives a
    # ``git add .`` cycle. Without it, an empty directory wouldn't
    # round-trip through git, and Hypatia's first character would
    # land in a directory git creates fresh — losing the structural
    # signal that "this project has a characters/ surface ready".
    gitkeep = characters_dir / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    created: list[str] = []

    # continuity.md — the index Hypatia reads first.
    continuity_path = project_dir / "continuity.md"
    _write_with_frontmatter(
        continuity_path,
        _make_frontmatter(
            element="continuity", title=title, slug=slug, today=today_iso,
        ),
        _build_continuity_body(title=title, slug=slug),
    )
    created.append(f"{rel_dir}/continuity.md")

    # The four other element files (story / structure / world / voice).
    for element_kind, filename, body_template in _ELEMENT_FILES:
        element_path = project_dir / filename
        _write_with_frontmatter(
            element_path,
            _make_frontmatter(
                element=element_kind,
                title=title,
                slug=slug,
                today=today_iso,
            ),
            body_template.format(title=title),
        )
        created.append(f"{rel_dir}/{filename}")

    created.append(f"{rel_dir}/characters/.gitkeep")

    log.info(
        "fiction.scaffold_created",
        slug=slug,
        title=title[:80],
        rel_dir=rel_dir,
        files_created=len(created),
    )
    return ScaffoldResult(
        status="created",
        title=title,
        slug=slug,
        rel_dir=rel_dir,
        created_files=created,
        detail=(
            f"Fiction project '{title}' scaffolded at {rel_dir}. "
            f"continuity.md is the orientation index — start there."
        ),
    )
