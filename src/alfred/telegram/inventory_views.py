"""Inventory-view rendering for ``/questions`` + ``/research-pointers``
slash commands (Phase 4 Sub-arc C, 2026-05-18).

These commands surface the same set of records that Sub-arc B's
inventory MOCs index (``MOC/_Open Questions.md``,
``MOC/_Open Research Pointers.md``), but as a fresh-rendered Telegram
reply rather than a vault-resident markdown file. Three reasons the
slash command is a separate surface:

  1. **Glance-view from anywhere.** Operator can `/questions` from a
     phone without opening Obsidian — the live state of open work
     accessible at chat layer.
  2. **Predicate consistency.** The same predicates that drive
     ``INVENTORY_MOC_DISPATCH`` in ``vault/zettel_hooks.py`` drive
     these renderings — single source of truth for "what counts as
     open."
  3. **Grouping by ``mocs:``.** The slash command groups records by
     their topic-MOC membership (operator-set, frontmatter ``mocs:``
     list). The inventory MOC is a flat list; the slash command is
     hierarchical-by-MOC. Different views of the same data.

Read-only — these commands do NOT write to the vault, do NOT mutate
session state, do NOT create or edit records.

Output format (mirrors existing slash-command conventions —
explicit empty-state per ``feedback_intentionally_left_blank.md``;
fixed-width capping + "+N more" hint when overflowing the per-group
cap; Telegram MarkdownV2 NOT used because the wikilink syntax
collides with Telegram's bold/italic delimiters — we send plain
text + Obsidian wikilinks the operator can click in Obsidian).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import frontmatter
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Predicates — re-exported from zettel_hooks.INVENTORY_MOC_DISPATCH
# ---------------------------------------------------------------------------
#
# Sub-arc B's dispatch table is the single source of truth for "is
# this record open?" The slash command consults that same table so
# any future predicate change (e.g. adding ``status: refined`` to the
# research-pointer set) flows to both the inventory MOC and the
# slash command without code drift.


def _predicate_for_type(record_type: str) -> Callable[[dict], bool] | None:
    """Return the predicate for ``record_type`` from
    ``INVENTORY_MOC_DISPATCH``, or None if no entry matches.

    Lazy import to avoid a circular-import risk if telegram-layer
    code ever ends up referenced from the vault layer. Today the
    direction is one-way (telegram → vault) but keeping the import
    lazy mirrors the same discipline used elsewhere in zettel_hooks.
    """
    from alfred.vault.zettel_hooks import INVENTORY_MOC_DISPATCH

    for entry_type, predicate, _moc_rel_path, _moc_name in INVENTORY_MOC_DISPATCH:
        if entry_type == record_type:
            return predicate
    return None


# ---------------------------------------------------------------------------
# Record collection + grouping
# ---------------------------------------------------------------------------


_UNCATEGORIZED_GROUP_KEY: str = "__uncategorized__"


def _normalize_moc_key(value: Any) -> str:
    """Normalize a ``mocs:`` list entry to a stable grouping key.

    Strips wikilink brackets + pipe-alias display so
    ``"[[MOC/Stoicism|Stoic Practice]]"`` and ``"[[MOC/Stoicism]]"`` and
    ``"MOC/Stoicism"`` all group together.
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


def collect_records(
    vault_path: Path,
    record_type: str,
) -> list[dict[str, Any]]:
    """Scan the vault for records of ``record_type`` matching the
    inventory predicate.

    Returns a list of dicts with the per-record fields the renderer
    needs: ``path`` (rel_path), ``name`` (display title), ``status``,
    ``created`` (ISO date string or empty), ``mocs`` (normalized list).

    Records that fail to parse (corrupt frontmatter) are skipped
    silently — same defensive shape as ``vault_list`` /
    ``vault_context``. The slash command is glance-view; one
    unparseable record shouldn't break the whole reply.

    Records that don't match the predicate (answered questions,
    completed pointers, etc.) are filtered OUT here so the renderer
    only sees in-scope work.

    Failure-isolated at per-record granularity. If the predicate
    itself raises (defensive against weird operator frontmatter),
    the record is excluded.
    """
    predicate = _predicate_for_type(record_type)
    if predicate is None:
        # Caller is asking for a type we have no inventory predicate
        # for. Return empty list — handler will render the empty-
        # state message naming the type, which is the correct UX
        # (not "broken — pretend nothing exists silently").
        log.warning(
            "inventory_views.unknown_record_type",
            record_type=record_type,
        )
        return []

    type_dir = vault_path / record_type
    if not type_dir.is_dir():
        # No records of this type yet — operator hasn't created any.
        return []

    out: list[dict[str, Any]] = []
    for md_file in sorted(type_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != record_type:
            continue
        try:
            if not predicate(fm):
                continue
        except Exception:  # noqa: BLE001
            continue

        # Normalize ``mocs`` — operator-typo defense + uniform shape
        # for the grouping step.
        mocs_raw = fm.get("mocs")
        mocs_list: list[str] = []
        if isinstance(mocs_raw, list):
            for entry in mocs_raw:
                norm = _normalize_moc_key(entry)
                if norm:
                    mocs_list.append(norm)
        elif isinstance(mocs_raw, str):
            norm = _normalize_moc_key(mocs_raw)
            if norm:
                mocs_list.append(norm)

        created = fm.get("created", "")
        if not isinstance(created, str):
            created = str(created)

        rel_path = f"{record_type}/{md_file.name}"
        name = fm.get("name") or md_file.stem
        if not isinstance(name, str):
            name = str(name)

        out.append({
            "path": rel_path,
            "name": name,
            "status": str(fm.get("status", "")),
            "created": created,
            "mocs": mocs_list,
        })

    return out


def group_by_moc(
    records: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group records by their ``mocs:`` membership.

    A record with multiple ``mocs:`` entries appears under EACH MOC
    group (intentional — operator can see the same record from any
    of its membership perspectives). A record with empty ``mocs:``
    lands in the ``_UNCATEGORIZED_GROUP_KEY`` bucket.

    Returns dict: ``{moc_target: [records]}`` plus the uncategorized
    bucket. Within each group, records are sorted by ``created``
    descending (newest first) — matches the "what's most recent
    open work" surfacing intent.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if not rec["mocs"]:
            groups.setdefault(_UNCATEGORIZED_GROUP_KEY, []).append(rec)
            continue
        for moc in rec["mocs"]:
            groups.setdefault(moc, []).append(rec)

    for moc, recs in groups.items():
        # Newest-created first. Records with empty created sort to
        # the bottom (empty string sorts before any ISO date when
        # reversed; we want it last → use a sort key that pushes
        # empty values to the end).
        recs.sort(
            key=lambda r: (r["created"] or "0000-00-00"),
            reverse=True,
        )

    return groups


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


_TITLE_BY_TYPE: dict[str, str] = {
    "question": "Open Questions",
    "research-pointer": "Open Research Pointers",
}

# Lowercased bare-noun for the empty-state message — separate from the
# title because "Open Questions" → "questions" (drop "Open" prefix
# so the empty-state reads "No open questions." not "No open open
# questions.").
_EMPTY_NOUN_BY_TYPE: dict[str, str] = {
    "question": "questions",
    "research-pointer": "research pointers",
}

_EMPTY_HINT_BY_TYPE: dict[str, str] = {
    "question": "status in {open, refined}",
    "research-pointer": "status == open",
}


def render_inventory(
    record_type: str,
    records: list[dict[str, Any]],
    *,
    per_group_cap: int = 20,
) -> str:
    """Render a grouped Markdown reply for the inventory.

    Empty case:
        ``📋 No open questions. (Filter active: status in {open,
        refined})``
        — per the ``intentionally_left_blank`` discipline. Always
        emits a recognizable reply so the operator can distinguish
        "no records match" from "command broken".

    Non-empty case:
        ``📋 Open Questions (N total)

        ## [[MOC/Stoicism]] (3)
        - [[question/...]] (open, 2026-05-15)
        - ...

        ## [[MOC/HEMA MOC]] (1)
        - [[question/...]] (refined, 2026-05-08)

        ## Uncategorized (1)
        - [[question/...]] (open, 2026-05-18)``

    Cap behaviour: if a group has more than ``per_group_cap`` records,
    only the most-recent ``per_group_cap`` render; a summary line
    ``- +N more (open in vault)`` follows.

    Group ordering: alphabetical by MOC key (deterministic for the
    operator), except the Uncategorized bucket goes LAST. Within each
    group, records sort newest-created first (matches the "what's
    most recently active" surfacing intent).
    """
    title = _TITLE_BY_TYPE.get(record_type, record_type)
    filter_hint = _EMPTY_HINT_BY_TYPE.get(record_type, "")
    empty_noun = _EMPTY_NOUN_BY_TYPE.get(record_type, record_type)

    if not records:
        suffix = (
            f" (Filter active: {filter_hint})"
            if filter_hint
            else ""
        )
        return f"📋 No open {empty_noun}.{suffix}"

    groups = group_by_moc(records)
    total = len(records)

    lines: list[str] = [f"📋 {title} ({total} total)", ""]

    # Sort group keys: alphabetical, but push the uncategorized bucket
    # to the end.
    moc_keys = sorted(
        k for k in groups.keys() if k != _UNCATEGORIZED_GROUP_KEY
    )
    if _UNCATEGORIZED_GROUP_KEY in groups:
        moc_keys.append(_UNCATEGORIZED_GROUP_KEY)

    for key in moc_keys:
        recs = groups[key]
        full_count = len(recs)
        if key == _UNCATEGORIZED_GROUP_KEY:
            header = f"## Uncategorized ({full_count})"
        else:
            header = f"## [[{key}]] ({full_count})"
        lines.append(header)

        # Cap: render at most per_group_cap; append "+N more" hint
        # if truncated.
        capped = recs[:per_group_cap]
        for rec in capped:
            wikilink = f"[[{rec['path'][:-3] if rec['path'].endswith('.md') else rec['path']}]]"
            status = rec["status"] or "open"
            created = rec["created"] or "no-date"
            lines.append(f"- {wikilink} ({status}, {created})")
        if full_count > per_group_cap:
            remaining = full_count - per_group_cap
            lines.append(f"- +{remaining} more (open in vault)")

        lines.append("")  # Blank line between groups for readability.

    return "\n".join(lines).rstrip("\n")
