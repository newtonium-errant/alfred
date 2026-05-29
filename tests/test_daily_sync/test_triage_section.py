"""Tier-V2 Ship 3 — Daily Sync Triage Queue tests (2026-05-29).

Covers ``src/alfred/daily_sync/triage_section.py``:

  * Numbered render from ``alfred_triage: True`` task records.
  * Empty-state sentinel per ``feedback_intentionally_left_blank``.
  * Status filter: ``todo`` / ``active`` surface; ``done`` /
    ``cancelled`` / ``blocked`` excluded.
  * Defensive type filter: non-task records under ``task/`` skipped.
  * Defensive triage filter: records WITHOUT ``alfred_triage: True``
    skipped (even when status is open).
  * Numbering format pin: ``1. <name>`` / ``2. <name>`` …
  * Section header includes count: ``### Triage Queue (N)``.
  * Registration test: ``register()`` adds the provider at
    priority 24.
  * Per-instance vault_path holder: ``set_vault_path`` /
    ``get_vault_path`` round-trip.
  * start_index parameter respected (assembler-driven global
    numbering).
  * Boundary: 100+ triage items — no truncation; full list emits.
  * Log emissions pinned per builder.md rule #9.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import structlog

from alfred.daily_sync import assembler
from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.triage_section import (
    SECTION_HEADER_TEMPLATE,
    TriageItemSummary,
    consume_last_batch,
    get_vault_path,
    peek_last_batch_count,
    register,
    render_batch,
    set_vault_path,
    triage_section,
)


TODAY = date(2026, 5, 29)


@pytest.fixture(autouse=True)
def _isolate_module_holders():
    """Per-test isolation: clear the module-level vault-path holder +
    last-batch holder + assembler registry before AND after each
    test. Mirrors the friction_section / radar_section test
    discipline — without this, holder state bleeds between tests
    and the suite becomes order-dependent."""
    from alfred.daily_sync import triage_section as _tc
    _tc._VAULT_PATH_HOLDER.clear()
    _tc._LAST_BATCH_HOLDER["items"] = []
    assembler.clear_providers()
    yield
    _tc._VAULT_PATH_HOLDER.clear()
    _tc._LAST_BATCH_HOLDER["items"] = []
    assembler.clear_providers()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_task(
    vault_path: Path,
    filename: str,
    fm_yaml: str,
) -> Path:
    """Write a task record under ``<vault>/task/<filename>``."""
    task_dir = vault_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / filename
    path.write_text(f"---\n{fm_yaml}---\n\n# body\n", encoding="utf-8")
    return path


def _bare_config() -> DailySyncConfig:
    """Minimal DailySyncConfig for provider invocation."""
    return DailySyncConfig(enabled=True)


# ---------------------------------------------------------------------------
# Cross-agent contract — module-level constant pin
# ---------------------------------------------------------------------------


def test_section_header_template_pinned() -> None:
    """Ship 4 SKILL may quote this — pin the format string so a
    rename surfaces immediately."""
    assert SECTION_HEADER_TEMPLATE == "### Triage Queue ({count})"


# ---------------------------------------------------------------------------
# Vault-path holder round-trip
# ---------------------------------------------------------------------------


def test_set_get_vault_path_round_trip(tmp_path: Path) -> None:
    """``set_vault_path`` configures the per-daemon vault path;
    ``get_vault_path`` returns it. Mirrors the friction_section /
    attribution_section pattern."""
    set_vault_path(tmp_path)
    assert get_vault_path() == tmp_path


# ---------------------------------------------------------------------------
# Empty state — intentionally-left-blank
# ---------------------------------------------------------------------------


def test_empty_vault_renders_zero_count_sentinel(tmp_path: Path) -> None:
    """No ``task/`` directory → ``### Triage Queue (0)`` + sentinel
    (idle-not-broken signal)."""
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (0)" in rendered
    assert "*(no triage items today)*" in rendered


def test_empty_task_dir_renders_zero_count_sentinel(tmp_path: Path) -> None:
    """``task/`` directory exists but no triage records → still
    the (0) sentinel."""
    (tmp_path / "task").mkdir()
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (0)" in rendered
    assert "*(no triage items today)*" in rendered


def test_no_alfred_triage_records_renders_zero_count(tmp_path: Path) -> None:
    """Records exist in ``task/`` but none have ``alfred_triage: True``
    → still the (0) sentinel — alfred_triage is the gate."""
    _write_task(
        tmp_path,
        "Normal Task.md",
        "type: task\nstatus: todo\nname: Normal Task\n",
    )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (0)" in rendered
    assert "Normal Task" not in rendered


def test_vault_path_unset_returns_none(tmp_path: Path) -> None:
    """When the daemon hasn't called ``set_vault_path``, the provider
    returns None + emits the ``vault_path_unset`` log event.

    (Autouse fixture clears the holder before each test, so this
    test sees the un-set state naturally.)"""
    with structlog.testing.capture_logs() as captured:
        result = triage_section(_bare_config(), TODAY)

    assert result is None
    events = [
        c for c in captured
        if c.get("event") == "daily_sync.triage.vault_path_unset"
    ]
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Numbered render
# ---------------------------------------------------------------------------


def test_renders_numbered_list_from_triage_records(tmp_path: Path) -> None:
    """Three ``alfred_triage: True`` records → numbered ``1. …`` /
    ``2. …`` / ``3. …`` list with count in the header."""
    _write_task(
        tmp_path,
        "Triage - Hinge note dedup.md",
        "type: task\nstatus: todo\nname: 'Triage - Hinge note dedup'\n"
        "alfred_triage: true\n",
    )
    _write_task(
        tmp_path,
        "Triage - TurboTax note dedup.md",
        "type: task\nstatus: todo\nname: 'Triage - TurboTax note dedup'\n"
        "alfred_triage: true\n",
    )
    _write_task(
        tmp_path,
        "Triage - Tim Denning Marketing Email note dedup.md",
        "type: task\nstatus: active\n"
        "name: 'Triage - Tim Denning Marketing Email note dedup'\n"
        "alfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)

    assert rendered is not None
    assert "### Triage Queue (3)" in rendered
    # Sorted alphabetically by name — Hinge < Tim Denning < TurboTax.
    assert "1. Triage - Hinge note dedup" in rendered
    assert "2. Triage - Tim Denning Marketing Email note dedup" in rendered
    assert "3. Triage - TurboTax note dedup" in rendered


def test_numbered_render_format_exact(tmp_path: Path) -> None:
    """Format pin: ``1. <name>`` — single-digit, period, space, name.
    Mirrors the dispatch's worked example verbatim."""
    _write_task(
        tmp_path,
        "Triage - X.md",
        "type: task\nstatus: todo\nname: 'Triage - X'\n"
        "alfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    # Exact line match — period after number, single space before name.
    assert "\n1. Triage - X" in rendered


# ---------------------------------------------------------------------------
# Status filter
# ---------------------------------------------------------------------------


def test_done_and_cancelled_excluded(tmp_path: Path) -> None:
    """Closed statuses excluded — only open triage records surface."""
    _write_task(
        tmp_path,
        "Done.md",
        "type: task\nstatus: done\nname: Done\nalfred_triage: true\n",
    )
    _write_task(
        tmp_path,
        "Cancelled.md",
        "type: task\nstatus: cancelled\nname: Cancelled\n"
        "alfred_triage: true\n",
    )
    _write_task(
        tmp_path,
        "Open.md",
        "type: task\nstatus: todo\nname: Open\nalfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (1)" in rendered
    assert "Open" in rendered
    assert "Done" not in rendered
    assert "Cancelled" not in rendered


def test_blocked_status_excluded_from_triage_queue(tmp_path: Path) -> None:
    """``blocked`` is in the broader ``OPEN_STATUSES`` (tier surfaces
    show blocked tasks) but the Triage Queue is actionable-only;
    blocked records are out-of-scope per Ship 3 design."""
    _write_task(
        tmp_path,
        "Blocked.md",
        "type: task\nstatus: blocked\nname: Blocked\n"
        "alfred_triage: true\n",
    )
    _write_task(
        tmp_path,
        "Open.md",
        "type: task\nstatus: todo\nname: Open\nalfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (1)" in rendered
    assert "Open" in rendered
    assert "Blocked" not in rendered


def test_active_status_surfaces(tmp_path: Path) -> None:
    """``active`` is open and actionable → surfaces."""
    _write_task(
        tmp_path,
        "Active.md",
        "type: task\nstatus: active\nname: Active Triage\n"
        "alfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (1)" in rendered
    assert "Active Triage" in rendered


# ---------------------------------------------------------------------------
# Type filter — defensive
# ---------------------------------------------------------------------------


def test_non_task_records_excluded(tmp_path: Path) -> None:
    """Defensive: a non-task record under ``task/`` (operator paste)
    is skipped even if it has ``alfred_triage: True``."""
    _write_task(
        tmp_path,
        "Stray.md",
        "type: note\nstatus: todo\nname: Stray\nalfred_triage: true\n",
    )
    _write_task(
        tmp_path,
        "Real Triage.md",
        "type: task\nstatus: todo\nname: Real Triage\n"
        "alfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (1)" in rendered
    assert "Real Triage" in rendered
    assert "Stray" not in rendered


# ---------------------------------------------------------------------------
# render_batch unit — start_index pin (assembler-driven numbering)
# ---------------------------------------------------------------------------


def test_render_batch_respects_start_index(tmp_path: Path) -> None:
    """DUAL-SEMANTIC NUMBERING contract (ratified 2026-05-29 code-
    review): the render line uses LOCAL numbering (operator-facing
    "triage 1, 2, 3" reads naturally regardless of which sections
    rendered above), while the TriageItemSummary.item_number is
    GLOBAL (assembler-facing cross-section addressability — Andrew
    can reply "item 7" and the dispatcher resolves it unambiguously
    even though the rendered text shows "3").

    Pinned per dual-semantic memo candidate (#24). When start_index=5
    (e.g. friction emitted items 1..4 above), the triage queue
    SHOULD render lines "1. ..., 2. ..." (local) AND emit summaries
    with item_number=5, 6 (global)."""
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    p1 = task_dir / "Triage - A.md"
    p1.write_text(
        "---\ntype: task\nstatus: todo\nname: 'Triage - A'\n"
        "alfred_triage: true\n---\n",
        encoding="utf-8",
    )
    p2 = task_dir / "Triage - B.md"
    p2.write_text(
        "---\ntype: task\nstatus: todo\nname: 'Triage - B'\n"
        "alfred_triage: true\n---\n",
        encoding="utf-8",
    )

    from alfred.daily_sync.triage_section import _scan_triage_records
    records = _scan_triage_records(tmp_path)

    rendered, summaries = render_batch(records, tmp_path, start_index=5)
    # Render line uses LOCAL numbering — operator-facing
    # "triage 1, 2..." regardless of assembler offset.
    assert "1. Triage - A" in rendered
    assert "2. Triage - B" in rendered
    # The 5/6 globals MUST NOT appear in the rendered text — that
    # would be a regression to single-semantic numbering.
    assert "5. Triage - A" not in rendered
    assert "6. Triage - B" not in rendered
    # Summary item_number is GLOBAL — assembler-facing
    # cross-section addressability.
    assert len(summaries) == 2
    assert summaries[0].item_number == 5
    assert summaries[1].item_number == 6


def test_render_batch_item_summary_path_is_vault_relative(
    tmp_path: Path,
) -> None:
    """Item summary ``path`` field is vault-relative (e.g.
    ``"task/Triage - X.md"``), matching attribution_section's
    ``record_path`` convention."""
    _write_task(
        tmp_path,
        "Triage - X.md",
        "type: task\nstatus: todo\nname: 'Triage - X'\n"
        "alfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    triage_section(_bare_config(), TODAY)
    items = consume_last_batch()
    assert len(items) == 1
    assert items[0].path == "task/Triage - X.md"
    assert items[0].name == "Triage - X"
    assert items[0].item_number == 1


# ---------------------------------------------------------------------------
# Boundary — large queue
# ---------------------------------------------------------------------------


def test_100_triage_items_full_list_no_truncation(tmp_path: Path) -> None:
    """100+ triage records render as a full numbered list — no
    truncation. The Triage Queue is the operator's morning sweep
    surface; truncating would silently hide work."""
    for i in range(100):
        # Pad with leading zeros so alphabetical sort gives numeric
        # order — makes the assertion checkable.
        _write_task(
            tmp_path,
            f"Triage - {i:03d}.md",
            f"type: task\nstatus: todo\nname: 'Triage - {i:03d}'\n"
            "alfred_triage: true\n",
        )
    set_vault_path(tmp_path)
    rendered = triage_section(_bare_config(), TODAY)
    assert rendered is not None
    assert "### Triage Queue (100)" in rendered
    assert "1. Triage - 000" in rendered
    assert "100. Triage - 099" in rendered
    # Spot-check: every item appears.
    items = consume_last_batch()
    assert len(items) == 100


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_adds_provider_at_priority_24() -> None:
    """``register()`` adds ``triage_queue`` to the assembler at
    priority 24, between friction (23) and attribution (25)."""
    register()
    assert "triage_queue" in assembler.registered_providers()


def test_register_is_idempotent() -> None:
    """Double-call must not raise — mirrors friction_section.register's
    idempotency contract."""
    register()
    register()  # MUST NOT raise
    assert "triage_queue" in assembler.registered_providers()


def test_registration_priority_slot_is_24() -> None:
    """Verify the priority slot directly — pin so a refactor that
    nudges the slot surfaces here (friction at 23 + attribution at
    25 leave 24 as the only correct value for triage)."""
    register()
    # Reach into the registry to inspect the entry — same surface
    # other section tests use (no public API for "lookup by name").
    matches = [
        entry for entry in assembler._REGISTRY
        if entry.name == "triage_queue"
    ]
    assert len(matches) == 1
    assert matches[0].priority == 24
    assert matches[0].item_count_after is peek_last_batch_count


# ---------------------------------------------------------------------------
# Log emissions
# ---------------------------------------------------------------------------


def test_rendered_log_event_fires_with_counts(tmp_path: Path) -> None:
    """The ``rendered`` log event surfaces ``date`` + ``item_count``
    + ``start_index`` per builder.md rule #9."""
    _write_task(
        tmp_path,
        "Triage - A.md",
        "type: task\nstatus: todo\nname: 'Triage - A'\n"
        "alfred_triage: true\n",
    )
    set_vault_path(tmp_path)
    with structlog.testing.capture_logs() as captured:
        triage_section(_bare_config(), TODAY, start_index=7)
    events = [
        c for c in captured if c.get("event") == "daily_sync.triage.rendered"
    ]
    assert len(events) == 1
    e = events[0]
    assert e["date"] == "2026-05-29"
    assert e["item_count"] == 1
    assert e["start_index"] == 7


def test_triage_item_summary_round_trips_to_dict() -> None:
    """``TriageItemSummary.to_dict`` is the state-file persistence
    shape — pin the field names for the future dispatcher hook."""
    s = TriageItemSummary(
        item_number=7,
        path="task/Triage - X.md",
        name="Triage - X",
    )
    d = s.to_dict()
    assert d == {
        "item_number": 7,
        "path": "task/Triage - X.md",
        "name": "Triage - X",
    }
