"""Zettel auto-maintenance hooks (Phase 3, 2026-05-18).

Per ``project_hypatia_zettelkasten_redesign.md`` "Auto-maintenance
behaviors" items 6 + 8:

  6. **Author Contents maintenance**: when a ``zettel/`` is created
     with ``author:`` set, Hypatia appends ``- [[zettel/Title]]`` to
     the author's ``# Contents``. Sources NEVER auto-append to
     author Contents — only zettels do.
  8. **Supersede chain mirroring**: operator sets
     ``supersedes: [[OldZ]]`` on new zettel; Hypatia auto-mirrors
     ``superseded_by: [[NewZ]]`` to old zettel + adds
     ``## Superseded by`` body callout. Operator writes the WHY
     narrative in new zettel's ``## Supersedes`` callout.

Both hooks fire from inside ``vault_create`` / ``vault_edit`` when
the relevant frontmatter is set on a ``zettel/`` record. They are
INTERNAL — not exposed to LLM agents, not registered through the
event-hook registry. The pattern is the post-write callback used
by the GCal event hooks but type-scoped to zettel records and
purely local (no external syncer).

Error model: every helper here is failure-isolated. Each catches
its own exceptions and logs without propagating, so a hook failure
NEVER breaks the originating ``vault_create`` / ``vault_edit`` —
the vault is canonical, the cross-record mirroring is a projection.
Pre-Phase-3 records without the expected scaffolding (missing
``## Superseded by`` section in old zettel, missing ``# Contents``
in author) are handled by appending the section if absent rather
than no-op'ing — the mirroring intent is to make the audit log
real on disk.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Callable

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Section header constants (canonical names — match scaffold/_templates/)
# ---------------------------------------------------------------------------

#: Old-zettel body section where ``- [[zettel/NewTitle]] (YYYY-MM-DD)``
#: bullets accumulate when newer zettels supersede this one. Auto-
#: maintained by the supersede mirror. Operator-readable audit log.
_SUPERSEDED_BY_HEADING: str = "## Superseded by"

#: New-zettel body section where the operator writes the WHY-this-
#: changed narrative. Hypatia scaffolds the empty section on create
#: (via the zettel template); operator fills the paragraph. Hypatia
#: does NOT auto-write content here — that's an operator-only zone.
_SUPERSEDES_HEADING: str = "# Supersedes"

#: Author body section where ``- [[zettel/Title]]`` bullets accumulate
#: when zettels reference this author. Z-centric per the locked plan —
#: sources never auto-append here.
_AUTHOR_CONTENTS_HEADING: str = "# Contents"


# ---------------------------------------------------------------------------
# Shared body-section utilities (duplicated from capture_source_anchor.py)
# ---------------------------------------------------------------------------
#
# These two helpers were originally introduced in
# ``alfred.telegram.capture_source_anchor`` for Phase 2 source-body
# growth. Phase 3 needs the same line-anchored heading detection +
# next-heading boundary logic but on different sections (Superseded
# by, # Contents). Duplicating here rather than cross-importing for
# two reasons:
#
#   1. ``capture_source_anchor`` lives under ``telegram/``; pulling
#      vault-layer code from telegram-layer would create a layer
#      direction violation (vault layer should be telegram-
#      independent).
#   2. The helper is small (~20 lines each) — duplication cost is
#      low; consolidation into a shared ``vault/_body_section.py``
#      would touch Phase 2 code and is out-of-scope for the Phase 3
#      ship.
#
# If a third call-site lands later, that's the trigger to consolidate
# into a shared utility module. For now, two-site duplication is
# acceptable per the codebase's existing pattern (cf. ``_first_user_text``
# duplicated between capture_batch.py + session.py).


def _find_h2_or_h1_section_start(body: str, heading: str) -> int:
    """Return the start index of a line-anchored heading match in
    ``body``, or -1 if not found.

    Line-anchored detection: heading must be at body byte 0 OR
    preceded by ``\\n``, AND followed by newline / whitespace /
    end-of-body (NOT another ``#`` — that would indicate a deeper
    heading level whose substring happens to overlap).

    Works for both H1 (``# Supersedes``) and H2 (``## Superseded by``)
    headings — generic helper that doesn't care about the heading
    depth, just the line-anchored shape.
    """
    search_from = 0
    while True:
        idx = body.find(heading, search_from)
        if idx == -1:
            return -1
        at_line_start = (idx == 0) or (body[idx - 1] == "\n")
        after_idx = idx + len(heading)
        ends_cleanly = (
            after_idx == len(body)
            or body[after_idx] in ("\n", "\r")
            or body[after_idx].isspace()
        )
        if at_line_start and ends_cleanly:
            return idx
        search_from = idx + 1


def _find_next_top_heading(body: str, start_idx: int) -> int:
    """Return the index of the next H1 (``# ``) or H2 (``## ``) heading
    line at or after ``start_idx``, or ``len(body)`` if none.

    Used to bound a section. H3 (``### ``) and deeper headings are
    NOT bounds — they're subsections within the H1/H2 section.
    """
    i = start_idx
    while i < len(body):
        nl_idx = body.find("\n", i)
        if nl_idx == -1:
            return len(body)
        line_start = nl_idx + 1
        if line_start >= len(body):
            return len(body)
        if body[line_start:line_start + 2] == "# ":
            return line_start
        if body[line_start:line_start + 3] == "## ":
            return line_start
        i = line_start
    return len(body)


def _normalize_wikilink_target(value: Any) -> str:
    """Coerce a wikilink-shaped frontmatter value to its rel_path target.

    Accepts:
      * ``"[[zettel/Title]]"`` → ``"zettel/Title"``
      * ``"zettel/Title"`` → ``"zettel/Title"``
      * ``"[[zettel/Title|Display]]"`` → ``"zettel/Title"``
      * Anything else (None, empty, not-a-wikilink) → ``""``

    Returns the bare wikilink target (no brackets, no pipe-alias, no
    ``.md`` suffix). Empty string when input doesn't look like a
    wikilink — caller treats empty as "no link set."
    """
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    if "|" in text:
        text = text.split("|", 1)[0]
    if text.endswith(".md"):
        text = text[:-3]
    return text.strip()


def _wikilink_target_present(text: str, wikilink: str) -> bool:
    """Detect whether ``wikilink`` is already referenced anywhere in
    ``text``, tolerating pipe-alias display forms.

    Treats ``[[zettel/Title]]`` and ``[[zettel/Title|Display]]`` as
    the SAME logical reference. Without this tolerance, bare-substring
    idempotency checks (``bare_link in section_body``) miss the pipe-
    aliased form and append a duplicate bullet — the same idempotency
    hole hit Phase 2 (Permanent Notes spawned auto-append) and both
    Phase 3 hooks (superseded_by callout, author Contents append).
    Centralizing the check here so future zettel auto-maintenance
    hooks inherit the contract.

    ``wikilink`` must include the full ``[[…]]`` brackets. Returns
    True if the target stem (everything between ``[[`` and the
    closing ``]]`` or first ``|``) appears in ``text`` under either
    shape; False otherwise.
    """
    if not wikilink or not wikilink.startswith("[[") or not wikilink.endswith("]]"):
        return False
    target_stem = wikilink[2:-2]
    if "|" in target_stem:
        target_stem = target_stem.split("|", 1)[0]
    target_stem = target_stem.strip()
    if not target_stem:
        return False
    pattern = re.compile(
        r"\[\[" + re.escape(target_stem) + r"(?:\|[^\]]*)?\]\]"
    )
    return bool(pattern.search(text))


# ---------------------------------------------------------------------------
# Supersede chain mirror (Deliverable A, locked-plan item 8)
# ---------------------------------------------------------------------------


def _build_superseded_by_rewriter(
    new_zettel_wikilink: str,
    today_iso: str,
) -> Callable[[str], str]:
    """Build a body_rewriter callable that appends a ``- <wikilink>
    (YYYY-MM-DD)`` bullet to the old zettel's ``## Superseded by``
    section.

    Behaviour:
      * If the wikilink already exists anywhere in the section, no-op
        (idempotent — re-running the mirror doesn't duplicate
        bullets).
      * If ``## Superseded by`` section is missing, CREATE it at the
        end of the body. Auto-maintenance intent is to make the
        audit log real on disk; refusing to write because the old
        zettel pre-dates Phase 3 templates would lose the signal.
      * Otherwise append the bullet between the existing section
        content and the next H1/H2 heading.

    The wikilink-presence check tolerates both plain
    ``[[zettel/Title]]`` and pipe-aliased ``[[zettel/Title|Display]]``
    forms via :func:`_wikilink_target_present` — same logical
    reference, no duplicate bullet either way.
    """
    bullet = f"- {new_zettel_wikilink} ({today_iso})"
    bare_link = new_zettel_wikilink.strip()

    def _rewriter(body: str) -> str:
        sb_idx = _find_h2_or_h1_section_start(body, _SUPERSEDED_BY_HEADING)
        if sb_idx == -1:
            # Section missing — pre-Phase-3 zettel. Append the
            # section at the end of the body. Conservative shape: one
            # blank line before the heading, the heading, blank line,
            # the bullet.
            tail = body.rstrip("\n")
            if tail:
                return (
                    tail + "\n\n"
                    + _SUPERSEDED_BY_HEADING + "\n\n"
                    + bullet + "\n"
                )
            return _SUPERSEDED_BY_HEADING + "\n\n" + bullet + "\n"

        # Section exists. Locate its bounds.
        section_start = sb_idx + len(_SUPERSEDED_BY_HEADING)
        section_end = _find_next_top_heading(body, section_start)
        section_body = body[section_start:section_end]

        if _wikilink_target_present(section_body, bare_link):
            # Idempotent — wikilink already recorded somewhere in
            # the section (plain OR pipe-aliased form). Don't
            # duplicate the audit-log bullet.
            return body

        new_section_body = (
            section_body.rstrip("\n") + f"\n{bullet}\n"
        )
        if not new_section_body.endswith("\n\n"):
            new_section_body = new_section_body + "\n"

        return body[:section_start] + new_section_body + body[section_end:]

    return _rewriter


def mirror_supersedes_chain(
    vault_path: Path,
    new_zettel_rel_path: str,
    supersedes_value: Any,
    *,
    scope: str = "hypatia",
    today_iso: str | None = None,
) -> bool:
    """Mirror a new zettel's ``supersedes:`` field onto the old zettel's
    ``superseded_by:`` + ``## Superseded by`` body section.

    Triggered post-write by ``vault_create`` / ``vault_edit`` when a
    ``zettel/`` record gains a non-empty ``supersedes:`` field. The
    mirror is idempotent — re-runs don't duplicate frontmatter
    fields or body bullets.

    Edge cases:
      * ``supersedes_value`` empty / not-a-wikilink → no-op, return
        False.
      * Self-supersede (new zettel supersedes itself) → log warning,
        return False. The caller (``vault_create``) should have
        rejected this upstream; the hook is defense-in-depth.
      * Target file missing → log warning, return False. The
        operator may be staging a chain before the old zettel exists;
        the supersede frontmatter survives on the new zettel and
        manual reconciliation is possible.
      * Existing ``superseded_by:`` on old zettel: overwritten ONLY
        when the new target differs from what's already there
        (idempotent on the common case; chain-extension takes
        precedence — the most recent supersede wins). Old wikilink
        is preserved as a body-section bullet in either case so
        the audit trail isn't lost.

    Returns True when the old zettel was updated; False on no-op
    (empty input, self-supersede, missing target, already mirrored).
    Failure isolated — any unexpected exception logs + returns
    False without raising. ``vault_create`` MUST NOT be broken by
    a hook failure.
    """
    # Lazy import to avoid the cycle (zettel_hooks is imported by
    # ops.py; ops.py provides vault_edit which we call back into).
    from . import ops as _ops

    target = _normalize_wikilink_target(supersedes_value)
    if not target:
        return False

    new_zettel_no_md = (
        new_zettel_rel_path[:-3]
        if new_zettel_rel_path.endswith(".md")
        else new_zettel_rel_path
    )
    new_zettel_wikilink = f"[[{new_zettel_no_md}]]"

    # Self-supersede guard. The caller (vault_create) is expected to
    # reject this upstream — but the hook defends in depth since the
    # validation site is far from the hook fire site.
    if target == new_zettel_no_md:
        log.warning(
            "vault.zettel_hooks.self_supersede",
            new_zettel_rel_path=new_zettel_rel_path,
            supersedes_value=str(supersedes_value),
        )
        return False

    # Resolve the old zettel's path. Per the locked-plan schema,
    # ``supersedes:`` points at another zettel (``zettel/Title``);
    # be defensive if the target lacks a leading directory.
    if "/" not in target:
        target = f"zettel/{target}"
    old_rel_path = target + ".md"

    if not (vault_path / old_rel_path).exists():
        log.info(
            "vault.zettel_hooks.supersede_target_missing",
            new_zettel_rel_path=new_zettel_rel_path,
            old_zettel_rel_path=old_rel_path,
        )
        return False

    # Read the old zettel to determine whether frontmatter needs
    # updating + body needs appending.
    try:
        old_rec = _ops.vault_read(vault_path, old_rel_path)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.zettel_hooks.supersede_read_failed",
            old_rel_path=old_rel_path,
            error=str(exc),
        )
        return False

    old_fm = old_rec.get("frontmatter") or {}
    existing_superseded_by = _normalize_wikilink_target(
        old_fm.get("superseded_by")
    )
    needs_frontmatter_update = existing_superseded_by != new_zettel_no_md

    # Date stamp for the body-bullet audit line.
    if today_iso is None:
        today_iso = date.today().isoformat()

    rewriter = _build_superseded_by_rewriter(new_zettel_wikilink, today_iso)

    set_fields: dict[str, Any] = {}
    if needs_frontmatter_update:
        set_fields["superseded_by"] = new_zettel_wikilink

    try:
        if set_fields:
            _ops.vault_edit(
                vault_path,
                old_rel_path,
                set_fields=set_fields,
                body_rewriter=rewriter,
                scope=scope,
            )
        else:
            # Frontmatter already correct; just ensure the body
            # callout exists (idempotent rewriter no-ops if so).
            _ops.vault_edit(
                vault_path,
                old_rel_path,
                body_rewriter=rewriter,
                scope=scope,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.zettel_hooks.supersede_mirror_failed",
            new_zettel_rel_path=new_zettel_rel_path,
            old_zettel_rel_path=old_rel_path,
            error=str(exc),
        )
        return False

    log.info(
        "vault.zettel_hooks.supersede_mirrored",
        new_zettel_rel_path=new_zettel_rel_path,
        old_zettel_rel_path=old_rel_path,
        frontmatter_updated=needs_frontmatter_update,
    )
    return True


# ---------------------------------------------------------------------------
# Author Contents auto-append (Deliverable B, locked-plan item 6)
# ---------------------------------------------------------------------------


def _build_author_contents_rewriter(
    zettel_wikilink: str,
) -> Callable[[str], str]:
    """Build a body_rewriter that appends ``- <zettel_wikilink>`` to
    the author's ``# Contents`` section.

    Behaviour:
      * Idempotent — checks for bare ``[[zettel/Title]]`` presence
        anywhere in the section before appending.
      * If ``# Contents`` section is missing, CREATE it at the end of
        the body. Pre-Phase-3 person/org records may not have the
        section; the auto-maintenance intent is to make the index
        real on disk rather than silently dropping the signal.
      * The section is Z-centric — only zettel wikilinks should land
        here. Caller filters by ``record_type == "zettel"`` upstream;
        this helper doesn't second-guess.
      * Section is bounded by next H1/H2 (``_find_next_top_heading``).
        Inline dataview blocks (``<!-- ```dataview ... ``` -->``)
        inside the section are NOT preserved verbatim — bullets
        append AFTER existing content, before the next heading, so
        a dataview-only section gains a manual bullet list below
        the comment block.
    """
    bare_link = zettel_wikilink.strip()
    bullet = f"- {zettel_wikilink}"

    def _rewriter(body: str) -> str:
        contents_idx = _find_h2_or_h1_section_start(
            body, _AUTHOR_CONTENTS_HEADING,
        )
        if contents_idx == -1:
            # No # Contents section — append the section + first
            # bullet at end-of-body.
            tail = body.rstrip("\n")
            if tail:
                return (
                    tail + "\n\n"
                    + _AUTHOR_CONTENTS_HEADING + "\n\n"
                    + bullet + "\n"
                )
            return _AUTHOR_CONTENTS_HEADING + "\n\n" + bullet + "\n"

        section_start = contents_idx + len(_AUTHOR_CONTENTS_HEADING)
        section_end = _find_next_top_heading(body, section_start)
        section_body = body[section_start:section_end]

        if _wikilink_target_present(section_body, bare_link):
            # Idempotent — bullet already exists somewhere in section
            # (plain OR pipe-aliased form). Don't duplicate.
            return body

        new_section_body = section_body.rstrip("\n") + f"\n{bullet}\n"
        if not new_section_body.endswith("\n\n"):
            new_section_body = new_section_body + "\n"

        return body[:section_start] + new_section_body + body[section_end:]

    return _rewriter


def _normalize_lookup(text: str) -> str:
    """Lowercase + collapse whitespace for case-insensitive alias compare.

    Mirrors Phase 1's :func:`alfred.telegram.capture_source_anchor.
    _normalize_lookup`. Reimplemented inline (not imported) to keep
    the vault layer free of telegram-layer dependencies. If a third
    site needs this normalisation, consolidate into a shared helper
    in ``vault/`` then.
    """
    return " ".join((text or "").lower().split())


def _scan_dir_for_alias(
    vault_path: Path, dir_name: str, lookup_form: str,
) -> str | None:
    """Scan one vault subdirectory for a record whose filename stem,
    ``name`` frontmatter, or ``aliases`` list matches ``lookup_form``.

    Case-insensitive + whitespace-normalised compare via
    :func:`_normalize_lookup`. Mirrors Phase 1's
    :func:`_scan_authors_by_alias` shape. Returns rel_path
    (``"<dir>/<filename>.md"``) on first match; None if no match or
    directory absent.
    """
    # Lazy import to avoid the cycle (zettel_hooks ← ops at module
    # import time would loop since ops imports zettel_hooks lazily
    # inside the dispatch site).
    from . import ops as _ops

    scan_dir = vault_path / dir_name
    if not scan_dir.is_dir():
        return None

    lookup_norm = _normalize_lookup(lookup_form)
    if not lookup_norm:
        return None

    for md_path in sorted(scan_dir.glob("*.md")):
        rel = f"{dir_name}/{md_path.name}"
        try:
            rec = _ops.vault_read(vault_path, rel)
        except Exception:  # noqa: BLE001
            continue
        fm = rec.get("frontmatter") or {}

        # Filename stem match.
        if _normalize_lookup(md_path.stem) == lookup_norm:
            return rel

        # ``name`` frontmatter match.
        name_field = str(fm.get("name") or "").strip()
        if name_field and _normalize_lookup(name_field) == lookup_norm:
            return rel

        # ``aliases:`` list match.
        aliases = fm.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                alias_str = str(alias or "").strip()
                if alias_str and _normalize_lookup(alias_str) == lookup_norm:
                    return rel
    return None


def _resolve_author_target(
    vault_path: Path,
    author_value: Any,
) -> str | None:
    """Resolve a zettel's ``author:`` field to an existing record's
    rel_path, following the ``aliases:`` chain.

    Returns the resolved rel_path (e.g. ``"author/Aurelius, Marcus.md"``
    or ``"person/Doe, Jane.md"``) or None if no record can be located.

    Strategy:
      1. Normalize the wikilink target (strips brackets, pipe-alias,
         ``.md`` suffix).
      2. If the target includes a directory prefix:
         a. Try exact rel_path resolution first.
         b. Failing that, scan THAT directory for an aliases-chain
            match against the bare stem.
      3. If the target has no directory prefix, try ``author/`` then
         ``person/`` (in that order — Phase 1's canonical author
         location is ``author/``; ``person/`` is back-compat for
         operators who hand-write a wikilink against a person record
         that's the author of a zettel, e.g. a colleague's notebook).

    The ``author/`` and ``person/`` directories are both scanned for
    aliases because:
      * Phase 1 ratified ``author/`` as the canonical bibliographic-
        author location (``capture_source_anchor.resolve_or_create_
        author`` writes here).
      * ``person/`` records may ALSO be authors when the operator is
        writing zettels about a colleague's ideas; the resolver
        accommodates both shapes rather than forcing the operator to
        duplicate records.
      * Operator-typed ``[[author/Marcus]]`` (short form) routes
        through the ``author/`` aliases scan to find
        ``author/Aurelius, Marcus.md`` with ``aliases: [Marcus]``.

    Mirrors :func:`alfred.telegram.capture_source_anchor.
    _scan_authors_by_alias` for compat with Phase 1's resolver. The
    scan is O(N) over each scanned directory's ``.md`` files —
    fine for hundreds, grows linearly.
    """
    target = _normalize_wikilink_target(author_value)
    if not target:
        return None

    if "/" in target:
        # Operator-provided directory (``author/X``, ``person/X``,
        # ``org/X``, etc.).
        rel = f"{target}.md"
        if (vault_path / rel).exists():
            return rel
        # Path doesn't exist — fall through to aliases scan IN THE
        # SAME directory the operator specified. Phase 1's canonical
        # author location is ``author/``; ``person/`` is the back-
        # compat shape. Other directories don't get an aliases scan
        # (org/etc. aliases conventions aren't established).
        dir_name, bare_name = target.split("/", 1)
        if dir_name in ("author", "person"):
            return _scan_dir_for_alias(vault_path, dir_name, bare_name)
        return None

    # No directory prefix — try canonical ``author/`` first, then
    # ``person/`` for back-compat. Exact-path check at each layer
    # before the aliases scan to short-circuit common cases.
    for dir_name in ("author", "person"):
        rel = f"{dir_name}/{target}.md"
        if (vault_path / rel).exists():
            return rel
        scanned = _scan_dir_for_alias(vault_path, dir_name, target)
        if scanned is not None:
            return scanned
    return None


def append_to_author_contents(
    vault_path: Path,
    author_value: Any,
    new_zettel_rel_path: str,
    *,
    scope: str = "hypatia",
) -> bool:
    """Append ``- [[zettel/Title]]`` to the author record's
    ``# Contents`` section.

    Triggered post-write by ``vault_create`` / ``vault_edit`` when a
    ``zettel/`` record gains a non-empty ``author:`` field.

    Edge cases:
      * ``author_value`` empty / not-a-wikilink → no-op, return False.
      * Author record not found (after aliases scan) → log info,
        return False. The new zettel's ``author:`` field survives;
        manual reconciliation when the author record is created.
      * Existing bullet present → idempotent no-op (helper rewriter
        checks for ``[[zettel/Title]]`` presence in section).
      * Missing ``# Contents`` section on author → section auto-
        created at end of body.

    Returns True when the author record was updated; False on no-op.
    Failure-isolated — any unexpected exception logs + returns False.
    """
    from . import ops as _ops

    target_normalized = _normalize_wikilink_target(author_value)
    if not target_normalized:
        return False

    author_rel_path = _resolve_author_target(vault_path, author_value)
    if author_rel_path is None:
        log.info(
            "vault.zettel_hooks.author_target_missing",
            new_zettel_rel_path=new_zettel_rel_path,
            author_value=str(author_value),
        )
        return False

    new_zettel_no_md = (
        new_zettel_rel_path[:-3]
        if new_zettel_rel_path.endswith(".md")
        else new_zettel_rel_path
    )
    zettel_wikilink = f"[[{new_zettel_no_md}]]"

    rewriter = _build_author_contents_rewriter(zettel_wikilink)
    try:
        _ops.vault_edit(
            vault_path,
            author_rel_path,
            body_rewriter=rewriter,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.zettel_hooks.author_append_failed",
            new_zettel_rel_path=new_zettel_rel_path,
            author_rel_path=author_rel_path,
            error=str(exc),
        )
        return False

    log.info(
        "vault.zettel_hooks.author_contents_appended",
        new_zettel_rel_path=new_zettel_rel_path,
        author_rel_path=author_rel_path,
    )
    return True


__all__ = [
    "mirror_supersedes_chain",
    "append_to_author_contents",
]
