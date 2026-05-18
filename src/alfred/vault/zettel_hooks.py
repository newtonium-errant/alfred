"""Zettel auto-maintenance hooks (Phase 3 + Phase 4, 2026-05-18).

Per ``project_hypatia_zettelkasten_redesign.md`` "Auto-maintenance
behaviors" items 6 + 7 + 8:

  6. **Author Contents maintenance** (Phase 3): when a ``zettel/``
     is created with ``author:`` set, Hypatia appends
     ``- [[zettel/Title]]`` to the author's ``# Contents``.
     Sources NEVER auto-append to author Contents — only zettels do.
  7. **MOC member maintenance** (Phase 4): when a ``zettel/`` /
     ``source/`` / ``question/`` / ``research-pointer/`` record is
     created or edited with a non-empty ``mocs:`` frontmatter list,
     Hypatia appends ``- [[<type>/<Title>]]`` to each referenced
     MOC's ``# Contents`` section. Operator-paced cleanup —
     removing a MOC from the record's ``mocs:`` does NOT cascade
     a remove from the MOC's Contents (consistent with the author
     Contents append-only discipline).
  8. **Supersede chain mirroring** (Phase 3): operator sets
     ``supersedes: [[OldZ]]`` on new zettel; Hypatia auto-mirrors
     ``superseded_by: [[NewZ]]`` to old zettel + adds
     ``## Superseded by`` body callout. Operator writes the WHY
     narrative in new zettel's ``## Supersedes`` callout.

All hooks fire from inside ``vault_create`` / ``vault_edit`` when
the relevant frontmatter is set on the appropriate record type.
They are INTERNAL — not exposed to LLM agents, not registered
through the event-hook registry. The pattern is the post-write
callback used by the GCal event hooks but type-scoped to
zettelkasten records and purely local (no external syncer).

Error model: every helper here is failure-isolated. Each catches
its own exceptions and logs without propagating, so a hook failure
NEVER breaks the originating ``vault_create`` / ``vault_edit`` —
the vault is canonical, the cross-record mirroring is a projection.
Pre-Phase-3/4 records without the expected scaffolding (missing
``## Superseded by`` section in old zettel, missing ``# Contents``
in author/MOC) are handled by appending the section if absent
rather than no-op'ing — the mirroring intent is to make the audit
log real on disk.
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

#: MOC body section where member bullets accumulate when zettels /
#: sources / questions / research-pointers cite this MOC via their
#: ``mocs:`` frontmatter list. Auto-maintained by the MOC member
#: append hook (Phase 4). Operator owns hierarchy restructuring;
#: Hypatia only appends flat bullets at the section tail.
_MOC_CONTENTS_HEADING: str = "# Contents"

#: Record types whose ``mocs:`` frontmatter triggers the MOC member
#: append hook on ``vault_create`` / ``vault_edit``. All four
#: Zettelkasten types that ship a ``mocs: []`` frontmatter field in
#: their template are eligible:
#:
#:   * ``zettel``           — research-grounded atomic notes
#:   * ``source``           — running notes on consumed material
#:   * ``question``         — elevated atomic question records
#:   * ``research-pointer`` — elevated atomic research actions
#:
#: Memo records are deliberately excluded — memos are fleeting and
#: write-once-by-design; their fleeting nature means MOC indexing
#: doesn't fit (per locked-plan; memos do NOT carry ``mocs:`` in their
#: template). MOC records themselves are excluded too — a MOC's
#: ``parent_mocs:`` field is the MOC-to-MOC linkage surface, not
#: ``mocs:``; tree-of-MOCs maintenance is Phase 5+ work.
_MOC_TRIGGER_TYPES: frozenset[str] = frozenset({
    "zettel", "source", "question", "research-pointer",
})


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


# ---------------------------------------------------------------------------
# MOC member auto-append (Phase 4, locked-plan item 7)
# ---------------------------------------------------------------------------


def _normalize_mocs_field(value: Any) -> list[str]:
    """Coerce a frontmatter ``mocs:`` value to a list of normalized
    wikilink-target strings (no brackets, no pipe-alias, no ``.md``
    suffix).

    Accepts (defense-in-depth for operator typos):
      * List of wikilinks   — ``["[[MOC/Stoicism]]", "[[MOC/HEMA MOC]]"]``
      * List of bare paths  — ``["MOC/Stoicism"]``
      * Mixed list          — ``["[[MOC/X]]", "MOC/Y"]``
      * Single string       — ``"[[MOC/Stoicism]]"`` (operator-typo:
                              wrote scalar where template expects list)
      * None / empty list / non-iterable → ``[]``

    Returns the list of normalized targets, preserving the operator's
    order and de-duplicating empty entries. Empty-string entries (a
    ``mocs:`` list containing an empty placeholder) are dropped — they
    don't represent a real MOC reference.

    The output is ALWAYS a list (possibly empty) so callers can iterate
    unconditionally.
    """
    if value is None:
        return []
    # Operator-typo defense: scalar string where the template expects
    # a list. Coerce to single-element list before processing.
    if isinstance(value, str):
        normalized = _normalize_wikilink_target(value)
        return [normalized] if normalized else []
    # Non-iterable, non-string, non-None → defensive empty.
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for entry in value:
        normalized = _normalize_wikilink_target(entry)
        if normalized:
            out.append(normalized)
    return out


def _build_moc_contents_rewriter(
    member_wikilink: str,
) -> Callable[[str], str]:
    """Build a body_rewriter that appends ``- <member_wikilink>`` to a
    MOC's ``# Contents`` section.

    Mirrors :func:`_build_author_contents_rewriter` shape:
      * Idempotent — checks for bare wikilink presence (pipe-alias
        tolerant) anywhere in the section before appending.
      * Auto-creates ``# Contents`` if absent (pre-Phase-4 MOC
        records may not have the section if hand-authored without
        the template; auto-maintenance intent is to make the index
        real on disk).
      * Appends FLAT at section tail — does NOT restructure existing
        hierarchical Contents tree above the append point. Operator
        owns the tree shape (per the operator-only-zones discipline);
        Hypatia just adds the new bullet at the end of the section
        so the operator can subsequently move it into the right
        position in the hierarchy.
      * Section bounded by next H1/H2 — H3 subsections are part of
        the MOC's Contents (sub-trees) and NOT a section boundary.
    """
    bare_link = member_wikilink.strip()
    bullet = f"- {member_wikilink}"

    def _rewriter(body: str) -> str:
        contents_idx = _find_h2_or_h1_section_start(
            body, _MOC_CONTENTS_HEADING,
        )
        if contents_idx == -1:
            # No # Contents section — append the section + first
            # bullet at end-of-body. Conservative shape: one blank
            # line before the heading, the heading, blank line, the
            # bullet.
            tail = body.rstrip("\n")
            if tail:
                return (
                    tail + "\n\n"
                    + _MOC_CONTENTS_HEADING + "\n\n"
                    + bullet + "\n"
                )
            return _MOC_CONTENTS_HEADING + "\n\n" + bullet + "\n"

        section_start = contents_idx + len(_MOC_CONTENTS_HEADING)
        section_end = _find_next_top_heading(body, section_start)
        section_body = body[section_start:section_end]

        if _wikilink_target_present(section_body, bare_link):
            # Idempotent — bullet already exists somewhere in the
            # section (plain OR pipe-aliased form). Don't duplicate.
            return body

        new_section_body = section_body.rstrip("\n") + f"\n{bullet}\n"
        if not new_section_body.endswith("\n\n"):
            new_section_body = new_section_body + "\n"

        return body[:section_start] + new_section_body + body[section_end:]

    return _rewriter


def _resolve_moc_target(
    vault_path: Path,
    moc_value: Any,
) -> str | None:
    """Resolve a record's ``mocs:`` entry to an existing MOC record's
    rel_path.

    Returns the resolved rel_path (e.g.
    ``"MOC/Practical Stoicism MOC.md"``) or None if no record can be
    located.

    Strategy:
      1. Normalize the wikilink target (strips brackets, pipe-alias,
         ``.md`` suffix).
      2. If the target includes a directory prefix:
         a. Try exact rel_path resolution first.
         b. Failing that, scan ``MOC/`` for aliases (back-compat for
            operators who hand-wrote an aliased wikilink against
            a MOC record with ``aliases:`` set; rare but legal).
      3. If the target has no directory prefix, try ``MOC/<target>.md``
         then scan ``MOC/`` aliases.

    Unlike :func:`_resolve_author_target`, only the ``MOC/`` directory
    is scanned — MOC records are a single canonical type with one home
    directory. There's no second-directory fallback (author/ + person/
    were paired in Phase 3 to accommodate person-as-author back-compat;
    MOC has no analogous pairing).
    """
    target = _normalize_wikilink_target(moc_value)
    if not target:
        return None

    if "/" in target:
        # Operator-provided directory.
        rel = f"{target}.md"
        if (vault_path / rel).exists():
            return rel
        # Aliases scan IN THE SAME directory the operator specified.
        # Only ``MOC/`` is scanned — other directories' aliases
        # conventions aren't established for MOC-shaped lookups.
        dir_name, bare_name = target.split("/", 1)
        if dir_name == "MOC":
            return _scan_dir_for_alias(vault_path, dir_name, bare_name)
        return None

    # No directory prefix — try ``MOC/<target>.md`` then aliases scan.
    rel = f"MOC/{target}.md"
    if (vault_path / rel).exists():
        return rel
    return _scan_dir_for_alias(vault_path, "MOC", target)


def append_to_moc_contents(
    vault_path: Path,
    moc_value: Any,
    member_rel_path: str,
    *,
    scope: str = "hypatia",
) -> bool:
    """Append ``- [[<type>/<Title>]]`` to a MOC's ``# Contents`` section.

    Triggered by ``dispatch_moc_appends`` (one call per MOC entry in
    the source record's ``mocs:`` list). The function operates on a
    SINGLE MOC; the iteration over the ``mocs:`` list lives in
    :func:`dispatch_moc_appends` so the per-call retry/log granularity
    matches the per-call file write.

    Edge cases:
      * ``moc_value`` empty / not-a-wikilink → no-op, return False.
      * MOC record not found → log info, return False. The new
        zettel/source/question/research-pointer's ``mocs:`` field
        survives on disk; manual reconciliation when the MOC record
        is later created. (Common during forward-flow: operator types
        ``[[MOC/Stoicism]]`` in a zettel before having created the
        MOC record. Symmetric with Phase 3 author-missing fail-open.)
      * Existing bullet present (plain or pipe-aliased) → idempotent
        no-op (helper rewriter checks for ``[[<type>/<Title>]]``
        presence in section).
      * Missing ``# Contents`` section on MOC → section auto-created
        at end of body.

    Returns True when the MOC was updated; False on no-op.
    Failure-isolated — any unexpected exception logs + returns False.
    """
    from . import ops as _ops

    target_normalized = _normalize_wikilink_target(moc_value)
    if not target_normalized:
        return False

    moc_rel_path = _resolve_moc_target(vault_path, moc_value)
    if moc_rel_path is None:
        log.info(
            "vault.zettel_hooks.moc_target_missing",
            member_rel_path=member_rel_path,
            moc_value=str(moc_value),
        )
        return False

    member_no_md = (
        member_rel_path[:-3]
        if member_rel_path.endswith(".md")
        else member_rel_path
    )
    member_wikilink = f"[[{member_no_md}]]"

    rewriter = _build_moc_contents_rewriter(member_wikilink)
    try:
        _ops.vault_edit(
            vault_path,
            moc_rel_path,
            body_rewriter=rewriter,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vault.zettel_hooks.moc_append_failed",
            member_rel_path=member_rel_path,
            moc_rel_path=moc_rel_path,
            error=str(exc),
        )
        return False

    log.info(
        "vault.zettel_hooks.moc_contents_appended",
        member_rel_path=member_rel_path,
        moc_rel_path=moc_rel_path,
    )
    return True


def dispatch_moc_appends(
    vault_path: Path,
    member_rel_path: str,
    member_type: str,
    mocs_value: Any,
    *,
    scope: str = "hypatia",
) -> int:
    """Iterate a record's ``mocs:`` list, call
    :func:`append_to_moc_contents` for each entry.

    Returns the count of MOCs successfully appended (informational —
    used by the dispatch-site log line so operators can see
    "3 of 4 MOCs received the bullet, 1 was missing").

    Type-gates against ``_MOC_TRIGGER_TYPES`` — calling with a
    non-eligible record type is a no-op (returns 0). Empty / malformed
    ``mocs_value`` is also a no-op (returns 0).

    Per-MOC failure isolation: a single MOC that doesn't exist or
    that throws during write produces a per-call log line + False
    return from :func:`append_to_moc_contents`, but does NOT stop the
    iteration. The next MOC in the list still gets processed. This
    matches the "vault is canonical; cross-record mirroring is a
    projection" discipline — partial mirroring is better than abandoning
    the rest of the list because one MOC was missing.

    Per the ``feedback_intentionally_left_blank.md`` discipline, this
    emits a single ``vault.zettel_hooks.moc_dispatch_summary`` info
    log per call so operators can distinguish "ran, nothing to do"
    (empty mocs list, ineligible type) from "ran, dispatched N writes."
    """
    if member_type not in _MOC_TRIGGER_TYPES:
        log.info(
            "vault.zettel_hooks.moc_dispatch_summary",
            member_rel_path=member_rel_path,
            member_type=member_type,
            mocs_count=0,
            appended_count=0,
            reason="type_not_in_moc_trigger_types",
        )
        return 0

    moc_targets = _normalize_mocs_field(mocs_value)
    if not moc_targets:
        log.info(
            "vault.zettel_hooks.moc_dispatch_summary",
            member_rel_path=member_rel_path,
            member_type=member_type,
            mocs_count=0,
            appended_count=0,
            reason="empty_mocs_field",
        )
        return 0

    appended = 0
    for raw_target in moc_targets:
        # Pass the bare target through ``append_to_moc_contents`` —
        # which re-normalizes (idempotent) and resolves against the
        # MOC/ directory + aliases scan.
        success = append_to_moc_contents(
            vault_path, raw_target, member_rel_path, scope=scope,
        )
        if success:
            appended += 1

    log.info(
        "vault.zettel_hooks.moc_dispatch_summary",
        member_rel_path=member_rel_path,
        member_type=member_type,
        mocs_count=len(moc_targets),
        appended_count=appended,
    )
    return appended


__all__ = [
    "mirror_supersedes_chain",
    "append_to_author_contents",
    "append_to_moc_contents",
    "dispatch_moc_appends",
]
