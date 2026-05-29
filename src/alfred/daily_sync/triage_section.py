"""Triage Queue section provider — Tier-V2 Ship 3 (2026-05-29).

Reads ``vault/task/*.md``, filters records flagged ``alfred_triage:
True`` with open status, and renders the Daily Sync's Triage Queue
section as a numbered list. The queue surfaces the janitor's
proposed-merge dedup candidates so the operator can resolve them in
their morning sweep.

Priority: 24 — between friction (23) and attribution (25). Triage
items are signal-rich AND actionable (the operator confirms/declines
each dedup proposal), so they sit alongside friction and above the
long-tail attribution audit.

## Read path

The daemon calls :func:`set_vault_path` once at startup (mirroring
:mod:`attribution_section`). On each fire, the provider walks
``vault/task/*.md``, filters records where ``alfred_triage`` is
``True`` AND status is in ``{todo, active}``, and renders a numbered
list ordered by the record's ``name`` (or file stem when ``name``
missing). Numbered list is 1-indexed per the dispatch's worked
example.

## Why ``{todo, active}`` not full ``OPEN_STATUSES``

OPEN_STATUSES includes ``blocked``. Triage items are by definition
NOT blocked — they're proposed by the janitor's dedup scan; if the
operator has blocked the resolution, that's an explicit operator
signal to surface them differently (likely a future Daily Sync
"blocked-triage" section). For now, render only the actionable ones.

## Empty state

Per ``feedback_intentionally_left_blank.md``: when there are zero
triage records, the section still renders with
``### Triage Queue (0)`` + ``*(no triage items today)*`` so the
operator distinguishes "janitor proposed nothing new" from "section
provider didn't fire".

## Cross-agent contract

The :data:`SECTION_HEADER_TEMPLATE` constant is the operator-facing
header string with a ``{count}`` placeholder. Ship 4 SKILL may quote
this when teaching the talker to recognise the section heading. A
rename here = update SKILL in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog

from .config import DailySyncConfig

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Cross-agent contract — operator-facing header template
#
# Format string with a ``{count}`` placeholder. Ship 4 SKILL may quote
# this verbatim so the talker recognises the heading shape. Rename
# here = update SKILL in lockstep.
# ---------------------------------------------------------------------------

SECTION_HEADER_TEMPLATE = "### Triage Queue ({count})"

# Triage queue surfaces only the ACTIONABLE-now statuses. Blocked
# triage records are out-of-scope for today's render (operator
# explicitly blocked them; surfacing alongside fresh proposals would
# muddle the queue). A future "blocked-triage" section could pick them
# up if friction surfaces.
_TRIAGE_OPEN_STATUSES: frozenset[str] = frozenset({"todo", "active"})


@dataclass
class TriageItemSummary:
    """Lightweight summary for state-file persistence.

    Mirror of :class:`alfred.daily_sync.friction_section.FrictionItemSummary`
    + :class:`alfred.daily_sync.attribution_section.AttributionItem` shapes.
    Recorded under ``last_batch.triage_items`` in the Daily Sync state
    file so a future smart-routing dispatcher can resolve "item N" →
    task record. Pre-built here so the dispatcher hook can land later
    without re-shaping state-file rows.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    path: str  # vault-relative (e.g. "task/Triage - Hinge note dedup.md")
    name: str  # operator-facing display string

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "path": self.path,
            "name": self.name,
        }


# ---------------------------------------------------------------------------
# Vault-path holder — mirrors attribution_section's pattern
# ---------------------------------------------------------------------------


_VAULT_PATH_HOLDER: dict[str, Path] = {}


def set_vault_path(vault_path: Path) -> None:
    """Configure the vault path the section provider scans.

    Daemon calls this once at startup. Tests may call it before
    invoking :func:`triage_section` directly. Idempotent.
    """
    _VAULT_PATH_HOLDER["path"] = vault_path


def get_vault_path() -> Path | None:
    """Return the configured vault path, or ``None`` when unset."""
    return _VAULT_PATH_HOLDER.get("path")


# Module-level batch holder so the daemon can read items back after
# the assembler runs. Mirrors friction_section / radar_section /
# attribution_section.
_LAST_BATCH_HOLDER: dict[str, list[TriageItemSummary]] = {"items": []}


def consume_last_batch() -> list[TriageItemSummary]:
    """Return and clear the most recently-built batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after``
    hook so the next section provider's items number continuously
    after triage items."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


# ---------------------------------------------------------------------------
# Triage scan — schema-tolerant
# ---------------------------------------------------------------------------


def _is_open_for_triage(fm: dict[str, Any]) -> bool:
    """Return True when the record's status is in
    :data:`_TRIAGE_OPEN_STATUSES`.

    Missing status defaults to ``"todo"`` (forward-compat with
    operator-authored records that omit the field — todo is the
    safest default for "show in triage queue"). Per the dispatch
    contract, the triage queue surfaces only actionable statuses
    (todo + active); blocked records are out-of-scope.
    """
    status = fm.get("status") or "todo"
    if not isinstance(status, str):
        return False
    return status.lower() in _TRIAGE_OPEN_STATUSES


def _scan_triage_records(
    vault_path: Path,
) -> list[tuple[Path, dict, str]]:
    """Walk ``vault/task/*.md`` and return triage records.

    Filters at this layer:
      * Skip files that fail to parse (logged at warning per
        intentionally-left-blank).
      * Skip non-task ``type:`` (defensive against stray templates).
      * Skip records WITHOUT ``alfred_triage: True``.
      * Skip records whose status is not in
        :data:`_TRIAGE_OPEN_STATUSES`.

    Returns ``[(path, fm, name)]`` sorted by name (case-insensitive)
    so the rendered numbered list is deterministic across consecutive
    Daily Sync fires on a stable vault.

    The per-record status check uses our local
    :data:`_TRIAGE_OPEN_STATUSES` (todo + active) rather than the
    tier system's :data:`alfred.tier.compute.OPEN_STATUSES` (which
    includes blocked) — by design. Blocked triage records are
    explicitly out-of-scope; a future "blocked-triage" section may
    pick them up if friction surfaces.
    """
    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        log.info(
            "daily_sync.triage.no_task_dir",
            path=str(task_dir),
            detail=(
                "vault/task/ does not exist — empty triage queue."
            ),
        )
        return []

    out: list[tuple[Path, dict, str]] = []
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "daily_sync.triage.parse_failed",
                path=str(path),
                error=str(exc),
            )
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "task":
            continue
        if fm.get("alfred_triage") is not True:
            continue
        if not _is_open_for_triage(fm):
            log.info(
                "daily_sync.triage.closed_status_skipped",
                path=str(path),
                status=fm.get("status"),
            )
            continue
        name = str(fm.get("name") or path.stem)
        out.append((path, fm, name))

    out.sort(key=lambda t: t[2].lower())
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_batch(
    records: list[tuple[Path, dict, str]],
    vault_path: Path,
    *,
    start_index: int = 1,
) -> tuple[str, list[TriageItemSummary]]:
    """Render the triage-queue section.

    Returns ``(rendered_section_text, item_summaries)``. The text is
    always non-empty — empty records list still renders the
    ``*(no triage items today)*`` sentinel per intentionally-left-blank.
    The summary list mirrors the rendered items 1:1 in render order
    so item_number stays consistent with what the operator sees.

    Numbering uses ``start_index`` so the assembler can keep item
    numbers continuous across sections (e.g. friction's 3 items take
    1..3 and triage picks up at 4).
    """
    header = SECTION_HEADER_TEMPLATE.format(count=len(records))
    if not records:
        return (f"{header}\n\n*(no triage items today)*", [])

    lines: list[str] = [header, ""]
    summaries: list[TriageItemSummary] = []
    item_no = start_index
    for path, _fm, name in records:
        # DUAL-SEMANTIC NUMBERING (by design — ratified 2026-05-29
        # code-review on Tier-V2 Ship 3):
        # Render LOCAL (operator-facing: "triage 1, 2, 3");
        # summary item_number stays GLOBAL (assembler-facing:
        # cross-section addressability).
        lines.append(f"{item_no - start_index + 1}. {name}")
        # Vault-relative path for the summary — same shape as
        # attribution_section uses for ``record_path``.
        try:
            rel = str(path.relative_to(vault_path)).replace("\\", "/")
        except ValueError:
            # Defensive: path not under vault_path (shouldn't happen
            # given the scan starts from vault_path/task) — fall back
            # to the bare filename.
            rel = f"task/{path.name}"
        summaries.append(TriageItemSummary(
            item_number=item_no,
            path=rel,
            name=name,
        ))
        item_no += 1
    return ("\n".join(lines), summaries)


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


def triage_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — reads vault/task/*.md, renders triage queue.

    Registered with priority 24 (between friction at 23 and
    attribution at 25). Returns the rendered section text — even on
    empty days (per intentionally-left-blank). Returns ``None`` only
    when the daemon hasn't wired ``set_vault_path`` (defensive guard
    for tests that exercise the provider without going through
    daemon setup).
    """
    vault_path = get_vault_path()
    if vault_path is None:
        log.info("daily_sync.triage.vault_path_unset")
        return None
    if not vault_path.is_dir():
        log.info(
            "daily_sync.triage.vault_path_missing",
            path=str(vault_path),
        )
        return None

    records = _scan_triage_records(vault_path)
    rendered, summaries = render_batch(
        records, vault_path, start_index=start_index,
    )
    _LAST_BATCH_HOLDER["items"] = summaries

    log.info(
        "daily_sync.triage.rendered",
        date=today.isoformat(),
        item_count=len(summaries),
        start_index=start_index,
    )
    return rendered


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times."""
    from . import assembler
    if "triage_queue" in assembler.registered_providers():
        return
    assembler.register_provider(
        "triage_queue",
        priority=24,
        provider=triage_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "SECTION_HEADER_TEMPLATE",
    "TriageItemSummary",
    "consume_last_batch",
    "get_vault_path",
    "peek_last_batch_count",
    "register",
    "render_batch",
    "set_vault_path",
    "triage_section",
]
