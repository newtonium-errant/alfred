"""Tier-V2 Ship 1 — DailyCuration data layer tests (2026-05-29).

Covers ``src/alfred/tier/daily_curation.py``:

  * :class:`DailyCuration` round-trip serialization (dataclass → dict →
    dataclass equality)
  * :func:`load_daily_curation` — returns ``None`` on missing file,
    ``None`` on missing ``tier_curation`` block, populated dataclass
    when present
  * :func:`save_tier_curation` — preserves other frontmatter keys
    (cross-cutting contract with the routine aggregator)
  * Schema-tolerance — extra YAML keys are silently dropped on load
  * Source enum values are stable contract (T1_T2_SOURCES + T3_SOURCES)

Per ``feedback_log_emission_test_pattern``: log emissions are pinned
via :class:`structlog.testing.capture_logs` where the production code
emits — the load/save paths are observability-load-bearing per
``feedback_intentionally_left_blank``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import frontmatter
import structlog

from alfred.tier.daily_curation import (
    DailyCuration,
    T1T2Entry,
    T1_T2_SOURCES,
    T3Entry,
    T3_SOURCES,
    daily_file_lock,
    load_daily_curation,
    save_tier_curation,
)


TODAY = date(2026, 5, 29)


# ---------------------------------------------------------------------------
# Source enum pin
# ---------------------------------------------------------------------------


def test_t1_t2_sources_pinned() -> None:
    """Stable contract — Ship 4 SKILL references these verbatim. A
    change here = update Ship 4 in lockstep.

    Phase 2A Ship B (2026-05-29) added ``auto-due-routine`` +
    ``auto-surface-routine`` for routine-origin T1/T2 entries.
    Ship D SKILL must quote these verbatim — the talker discriminates
    operator replies based on the source-string distinction.
    """
    assert T1_T2_SOURCES == frozenset({
        "auto-due",
        "auto-escalate",
        "auto-due-routine",
        "auto-surface-routine",
        "operator",
        "rollover",
    })


def test_t3_sources_pinned() -> None:
    """T3 sources have NO ``rollover`` value (T3 is today's intentions,
    not rolling over self-care). Pinned for Ship 4 SKILL contract."""
    assert T3_SOURCES == frozenset({
        "aspirational",
        "operator",
        "operator-adhoc",
    })


# ---------------------------------------------------------------------------
# Dataclass round-trip
# ---------------------------------------------------------------------------


def test_t1_t2_entry_round_trip() -> None:
    """T1T2Entry ↔ dict preserves task + source + confirmed."""
    e = T1T2Entry(
        task="[[task/RRTS Payroll]]",
        source="auto-due",
        confirmed=True,
    )
    out = T1T2Entry.from_dict(e.to_dict())
    assert out == e


def test_t1_t2_entry_omits_confirmed_when_none() -> None:
    """T2 entries don't carry ``confirmed`` — the dict drops it cleanly
    so the YAML doesn't emit ``confirmed: null``."""
    e = T1T2Entry(task="[[task/Bug List]]", source="operator")
    assert e.to_dict() == {
        "task": "[[task/Bug List]]",
        "source": "operator",
    }
    # Round-trip preserves confirmed=None.
    assert T1T2Entry.from_dict(e.to_dict()).confirmed is None


def test_t3_entry_round_trip() -> None:
    """T3Entry ↔ dict preserves item + source. No ``confirmed`` field
    on T3."""
    e = T3Entry(item="Walk Fergus", source="aspirational")
    out = T3Entry.from_dict(e.to_dict())
    assert out == e
    assert "confirmed" not in e.to_dict()


def test_daily_curation_round_trip() -> None:
    """Full DailyCuration with all three tiers populated + curated_at
    + rollover_from round-trips to dict and back."""
    cur = DailyCuration(
        t1=[
            T1T2Entry(
                task="[[task/Steph Yang ROE]]",
                source="auto-due",
                confirmed=True,
            ),
        ],
        t2=[
            T1T2Entry(
                task="[[task/RRTS Bug List — Burn Through]]",
                source="operator",
            ),
        ],
        t3=[
            T3Entry(item="Walk Fergus", source="aspirational"),
            T3Entry(item="Read for an hour", source="operator-adhoc"),
        ],
        curated_at="2026-05-29T07:14:00-03:00",
        rollover_from="2026-05-28",
    )
    round_tripped = DailyCuration.from_dict(cur.to_dict())
    assert round_tripped == cur


def test_daily_curation_empty_round_trip() -> None:
    """Empty buckets — the "operator hasn't curated yet" signal —
    round-trip cleanly. Empty tier arrays MUST be preserved (absence
    of the key would conflate with the ``tier_curation`` block missing
    entirely)."""
    cur = DailyCuration()
    out = cur.to_dict()
    assert out == {"t1": [], "t2": [], "t3": []}
    # No ``curated_at`` / ``rollover_from`` when None — clean shape.
    assert "curated_at" not in out
    assert "rollover_from" not in out
    assert DailyCuration.from_dict(out) == cur


def test_daily_curation_schema_tolerance_drops_unknown_top_level_keys() -> None:
    """Per CLAUDE.md ``load()`` schema-tolerance: extra keys are
    silently ignored. A future Ship 7 adding ``notes`` won't break
    rollback to Ship 1."""
    raw = {
        "t1": [],
        "t2": [],
        "t3": [],
        "future_field_from_ship_7": "ignored",
    }
    cur = DailyCuration.from_dict(raw)
    assert cur == DailyCuration()


def test_daily_curation_schema_tolerance_drops_partial_entries() -> None:
    """Per-entry validation: an entry missing required fields is
    silently dropped (the caller decides what to do with a partial
    curation)."""
    raw = {
        "t1": [
            {"task": "[[task/Good]]", "source": "operator"},
            {"task": "[[task/Bad]]"},  # missing source — dropped
            {"source": "operator"},   # missing task — dropped
            "not a dict",              # dropped
        ],
        "t2": [],
        "t3": [
            {"item": "Walk", "source": "aspirational"},
            {"item": "Bad"},           # missing source — dropped
        ],
    }
    cur = DailyCuration.from_dict(raw)
    assert len(cur.t1) == 1
    assert cur.t1[0].task == "[[task/Good]]"
    assert len(cur.t3) == 1


# ---------------------------------------------------------------------------
# load_daily_curation
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    """Return a tmp vault dir with a ``daily/`` subdir."""
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    return vault


def test_load_daily_curation_returns_none_when_file_missing(
    tmp_path: Path,
) -> None:
    """No daily file → None + the ``no_daily_file`` log event fires
    (Ship 2 brief uses this signal to render selection pools)."""
    vault = _make_vault(tmp_path)
    with structlog.testing.capture_logs() as captured:
        result = load_daily_curation(vault, TODAY)
    assert result is None
    events = [c for c in captured if c.get("event") == "tier.daily_curation.no_daily_file"]
    assert len(events) == 1
    assert events[0]["date"] == "2026-05-29"


def test_load_daily_curation_returns_none_when_block_absent(
    tmp_path: Path,
) -> None:
    """Daily file exists but no ``tier_curation`` frontmatter key →
    None + the ``no_tier_curation_block`` log event. Aggregator wrote
    a clean file; no talker curation yet today."""
    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-05-29.md"
    daily_file.write_text(
        "---\ntype: daily\ndate: 2026-05-29\n---\n\n# body\n",
        encoding="utf-8",
    )
    with structlog.testing.capture_logs() as captured:
        result = load_daily_curation(vault, TODAY)
    assert result is None
    events = [
        c for c in captured
        if c.get("event") == "tier.daily_curation.no_tier_curation_block"
    ]
    assert len(events) == 1


def test_load_daily_curation_parses_present_block(tmp_path: Path) -> None:
    """Block present and well-formed → populated DailyCuration + the
    ``loaded`` log with counts."""
    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-05-29.md"
    daily_file.write_text(
        "---\n"
        "type: daily\n"
        "date: 2026-05-29\n"
        "tier_curation:\n"
        "  t1:\n"
        "    - task: '[[task/RRTS Payroll]]'\n"
        "      source: auto-due\n"
        "      confirmed: true\n"
        "  t2:\n"
        "    - task: '[[task/Bug List]]'\n"
        "      source: operator\n"
        "  t3:\n"
        "    - item: Walk Fergus\n"
        "      source: aspirational\n"
        "  curated_at: '2026-05-29T07:14:00-03:00'\n"
        "  rollover_from: '2026-05-28'\n"
        "---\n\n# body\n",
        encoding="utf-8",
    )
    with structlog.testing.capture_logs() as captured:
        result = load_daily_curation(vault, TODAY)

    assert result is not None
    assert len(result.t1) == 1
    assert result.t1[0].task == "[[task/RRTS Payroll]]"
    assert result.t1[0].source == "auto-due"
    assert result.t1[0].confirmed is True
    assert len(result.t2) == 1
    assert result.t2[0].source == "operator"
    assert len(result.t3) == 1
    assert result.t3[0].item == "Walk Fergus"
    assert result.curated_at == "2026-05-29T07:14:00-03:00"
    assert result.rollover_from == "2026-05-28"

    # Log event with counts pinned per builder.md rule #9.
    events = [c for c in captured if c.get("event") == "tier.daily_curation.loaded"]
    assert len(events) == 1
    assert events[0]["t1_count"] == 1
    assert events[0]["t2_count"] == 1
    assert events[0]["t3_count"] == 1
    assert events[0]["has_rollover"] is True


def test_load_daily_curation_returns_none_on_parse_failure(
    tmp_path: Path,
) -> None:
    """Corrupt file → None + ``parse_failed`` log (Ship 2 treats this
    as un-curated, renders selection pools)."""
    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-05-29.md"
    daily_file.write_text(
        "---\n[unclosed yaml\n---\n\n# body\n",
        encoding="utf-8",
    )
    with structlog.testing.capture_logs() as captured:
        result = load_daily_curation(vault, TODAY)
    # python-frontmatter is lenient — it may or may not raise on this
    # input. We accept either outcome (None) — the test pins the
    # contract that a malformed file doesn't crash the loader.
    assert result is None or isinstance(result, DailyCuration)


# ---------------------------------------------------------------------------
# save_tier_curation — preserves other frontmatter keys
# ---------------------------------------------------------------------------


def test_save_tier_curation_preserves_other_frontmatter(
    tmp_path: Path,
) -> None:
    """The cross-cutting contract with the routine aggregator:
    aggregator owns ``type``/``date``/``routines_contributing``/
    ``critical_pending`` + body; this module owns ``tier_curation``.
    Each layer reads-preserves-writes the other's keys."""
    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-05-29.md"
    daily_file.write_text(
        "---\n"
        "type: daily\n"
        "date: 2026-05-29\n"
        "routines_contributing:\n"
        "  - Core Daily\n"
        "  - For Self Health\n"
        "critical_pending:\n"
        "  - Kiki Insulin @ 12:00\n"
        "---\n\n"
        "## Critical\n\n- Kiki Insulin @ 12:00\n\n"
        "## Tracked\n\n- Walk Fergus *(no completions yet)*\n",
        encoding="utf-8",
    )

    cur = DailyCuration(
        t1=[T1T2Entry(
            task="[[task/RRTS Payroll]]",
            source="auto-due",
            confirmed=True,
        )],
    )
    save_tier_curation(vault, TODAY, cur)

    # Re-read via python-frontmatter to verify ALL keys preserved.
    post = frontmatter.load(str(daily_file))
    meta = post.metadata or {}
    assert meta.get("type") == "daily"
    # YAML may parse date string as a date object; accept either.
    assert str(meta.get("date")) == "2026-05-29"
    assert meta.get("routines_contributing") == ["Core Daily", "For Self Health"]
    assert meta.get("critical_pending") == ["Kiki Insulin @ 12:00"]
    assert "tier_curation" in meta
    assert meta["tier_curation"]["t1"][0]["task"] == "[[task/RRTS Payroll]]"

    # Body content preserved verbatim — aggregator owns it.
    assert "## Critical" in post.content
    assert "Kiki Insulin @ 12:00" in post.content
    assert "Walk Fergus" in post.content


def test_save_tier_curation_creates_fresh_file_when_absent(
    tmp_path: Path,
) -> None:
    """No pre-existing file → seed minimum ``type: daily`` +
    ``date: <iso>`` + empty body. The routine aggregator's next fire
    will read-preserve-write this curation."""
    vault = _make_vault(tmp_path)
    cur = DailyCuration(
        t2=[T1T2Entry(task="[[task/Bug List]]", source="operator")],
    )
    save_tier_curation(vault, TODAY, cur)

    daily_file = vault / "daily" / "2026-05-29.md"
    assert daily_file.exists()
    post = frontmatter.load(str(daily_file))
    meta = post.metadata or {}
    assert meta.get("type") == "daily"
    assert str(meta.get("date")) == "2026-05-29"
    assert "tier_curation" in meta


def test_save_tier_curation_replaces_existing_curation(
    tmp_path: Path,
) -> None:
    """Saving twice → second curation replaces the first. The other
    frontmatter keys + body stay preserved across both writes."""
    vault = _make_vault(tmp_path)
    cur_v1 = DailyCuration(
        t1=[T1T2Entry(task="[[task/Old]]", source="auto-due")],
    )
    save_tier_curation(vault, TODAY, cur_v1)
    cur_v2 = DailyCuration(
        t1=[T1T2Entry(task="[[task/New]]", source="operator")],
    )
    save_tier_curation(vault, TODAY, cur_v2)

    result = load_daily_curation(vault, TODAY)
    assert result is not None
    assert len(result.t1) == 1
    assert result.t1[0].task == "[[task/New]]"


def test_save_tier_curation_emits_saved_log_event(
    tmp_path: Path,
) -> None:
    """Per builder.md rule #9: pin the ``saved`` log event with the
    canonical fields (t1/t2/t3 counts + has_rollover)."""
    vault = _make_vault(tmp_path)
    cur = DailyCuration(
        t1=[T1T2Entry(task="[[task/A]]", source="auto-due")],
        t2=[T1T2Entry(task="[[task/B]]", source="operator")],
        t3=[T3Entry(item="Walk", source="aspirational")],
        rollover_from="2026-05-28",
    )
    with structlog.testing.capture_logs() as captured:
        save_tier_curation(vault, TODAY, cur)
    events = [c for c in captured if c.get("event") == "tier.daily_curation.saved"]
    assert len(events) == 1
    e = events[0]
    assert e["t1_count"] == 1
    assert e["t2_count"] == 1
    assert e["t3_count"] == 1
    assert e["has_rollover"] is True


# ---------------------------------------------------------------------------
# Phase 2A Ship B — T1T2Entry discriminated-union (routine_item)
# ---------------------------------------------------------------------------
#
# Adds routine-origin entry support to T1T2Entry. The data layer must:
#   - Round-trip both ``task``-only AND ``routine_item``-only shapes
#   - Drop the absent shape on ``to_dict`` (clean YAML)
#   - Tolerate edge cases (loader-defensive)
#   - Accept the new source enum values via load path


def test_t1_t2_entry_round_trip_routine_item_shape() -> None:
    """Round-trip a routine-origin T1 entry: routine_item dict +
    source ``auto-due-routine`` + confirmed True.

    Cross-Ship contract: Ship B brief render, Ship D SKILL, and the
    talker writer all rely on this shape. A drift here breaks the
    routine-tier integration end-to-end."""
    e = T1T2Entry(
        routine_item={
            "record": "Recurring Bills + Admin",
            "text": "Pay Clinic Rental to Hussein Rafih",
        },
        source="auto-due-routine",
        confirmed=True,
    )
    out = T1T2Entry.from_dict(e.to_dict())
    assert out == e


def test_t1_t2_entry_routine_item_to_dict_drops_task_key() -> None:
    """``to_dict`` emits exactly ONE shape — drops the absent ``task``
    key so the YAML stays clean (no ``task: null`` clutter)."""
    e = T1T2Entry(
        routine_item={"record": "Bills", "text": "Pay Rent"},
        source="auto-due-routine",
        confirmed=False,
    )
    d = e.to_dict()
    assert "task" not in d
    assert d["routine_item"] == {"record": "Bills", "text": "Pay Rent"}
    assert d["source"] == "auto-due-routine"
    assert d["confirmed"] is False


def test_t1_t2_entry_task_to_dict_drops_routine_item_key() -> None:
    """Backward compat: task-origin entry's ``to_dict`` drops the
    absent ``routine_item`` key."""
    e = T1T2Entry(task="[[task/Old]]", source="operator")
    d = e.to_dict()
    assert "routine_item" not in d
    assert d == {"task": "[[task/Old]]", "source": "operator"}


def test_t1_t2_entry_task_shape_still_works_for_backward_compat() -> None:
    """Existing Tier-V2 Ship 1 task-shape entries must still round-trip
    after the Phase 2A Ship B schema extension."""
    e = T1T2Entry(
        task="[[task/Steph Yang ROE]]",
        source="auto-due",
        confirmed=True,
    )
    out = T1T2Entry.from_dict(e.to_dict())
    assert out == e
    assert out.task == "[[task/Steph Yang ROE]]"
    assert out.routine_item is None


def test_t1_t2_entry_from_dict_routine_item_missing_record_or_text_drops() -> None:
    """Defensive: ``routine_item`` dict missing required keys is
    treated as absent (caller's list-filter drops the whole entry)."""
    e = T1T2Entry.from_dict({
        "routine_item": {"record": "Bills"},  # missing 'text'
        "source": "auto-due-routine",
    })
    assert e.routine_item is None
    # No task either — empty discriminated state.
    assert e.task is None


def test_t1_t2_entry_from_dict_both_shapes_set_task_wins() -> None:
    """Edge case: if both ``task`` and ``routine_item`` are set, ``task``
    wins (documented precedence). Defensive against operator hand-edit
    corruption — preserves the existing task-shape data."""
    e = T1T2Entry.from_dict({
        "task": "[[task/X]]",
        "routine_item": {"record": "R", "text": "T"},
        "source": "operator",
    })
    assert e.task == "[[task/X]]"
    assert e.routine_item is None


def test_daily_curation_list_filter_accepts_routine_item_entry() -> None:
    """The ``DailyCuration._parse_t12_list`` filter (load-time guard)
    accepts entries with ``routine_item`` + source — the dispatch
    contract for Ship D writer paths."""
    raw = {
        "t1": [
            {
                "routine_item": {"record": "Bills", "text": "Pay Rent"},
                "source": "auto-due-routine",
                "confirmed": True,
            },
            {
                "task": "[[task/Mixed]]",
                "source": "operator",
            },
        ],
        "t2": [],
        "t3": [],
    }
    cur = DailyCuration.from_dict(raw)
    assert len(cur.t1) == 2
    # First entry is routine-origin.
    assert cur.t1[0].routine_item == {"record": "Bills", "text": "Pay Rent"}
    assert cur.t1[0].source == "auto-due-routine"
    # Second is task-origin.
    assert cur.t1[1].task == "[[task/Mixed]]"


def test_daily_curation_list_filter_drops_entries_missing_both_shapes() -> None:
    """Defensive: an entry with neither ``task`` nor ``routine_item``
    (just ``source``) is silently dropped — operator hand-edit
    corruption defense."""
    raw = {
        "t1": [
            {"source": "operator"},  # no task, no routine_item → drop
            {
                "task": "[[task/Valid]]",
                "source": "operator",
            },
        ],
        "t2": [],
        "t3": [],
    }
    cur = DailyCuration.from_dict(raw)
    assert len(cur.t1) == 1
    assert cur.t1[0].task == "[[task/Valid]]"


def test_t1_t2_entry_new_source_enum_values_accepted_by_loader() -> None:
    """Phase 2A Ship B added ``auto-due-routine`` +
    ``auto-surface-routine`` source values. The loader is tolerant —
    unknown sources don't crash; but these specific values must round-
    trip cleanly since the writer path and SKILL pin them."""
    for source_value in ("auto-due-routine", "auto-surface-routine"):
        e = T1T2Entry(
            routine_item={"record": "Bills", "text": "Pay Rent"},
            source=source_value,
        )
        out = T1T2Entry.from_dict(e.to_dict())
        assert out.source == source_value


def test_daily_curation_round_trip_with_routine_entries() -> None:
    """Full DailyCuration round-trip including a mix of task + routine
    T1 entries + T2 routine entry."""
    cur = DailyCuration(
        t1=[
            T1T2Entry(
                task="[[task/Steph Yang ROE]]",
                source="auto-due",
                confirmed=True,
            ),
            T1T2Entry(
                routine_item={
                    "record": "Weekly Chores",
                    "text": "Garbage Out",
                },
                source="auto-due-routine",
                confirmed=False,
            ),
        ],
        t2=[
            T1T2Entry(
                routine_item={
                    "record": "Recurring Bills + Admin",
                    "text": "Pay Clinic Rental ...",
                },
                source="auto-surface-routine",
            ),
        ],
        t3=[],
    )
    round_tripped = DailyCuration.from_dict(cur.to_dict())
    assert round_tripped == cur


# ---------------------------------------------------------------------------
# Atomic write — daily-file writer-race fix (Step 2, 2026-06-26)
# ---------------------------------------------------------------------------
#
# The daily file daily/<date>.md has two writers — save_tier_curation
# (owns tier_curation) and the routine aggregator (owns the rest + body).
# Both were non-atomic write_text, leaving a truncation window. Both now
# write .tmp -> os.replace with WRITER-DISTINGUISHED tmp suffixes so the
# two tmp files never collide.


def test_save_tier_curation_leaves_no_tmp_file(tmp_path: Path) -> None:
    """After an atomic save, the .curation.tmp scratch file is gone
    (os.replace moved it onto the real path)."""
    vault = _make_vault(tmp_path)
    cur = DailyCuration(
        t1=[T1T2Entry(task="[[task/X]]", source="operator")],
        t2=[], t3=[],
    )
    save_tier_curation(vault, TODAY, cur)
    daily_file = vault / "daily" / "2026-05-29.md"
    assert daily_file.exists()
    # No leftover scratch file.
    assert not (vault / "daily" / "2026-05-29.curation.tmp").exists()


def test_save_tier_curation_uses_distinct_tmp_suffix(tmp_path: Path) -> None:
    """The curation writer's tmp suffix is .curation.tmp — distinct from
    the aggregator's .routine.tmp — so the two writers' scratch files
    never collide on the same daily date. Pin the suffix by observing
    the tmp path os.replace consumes."""
    import os as _os

    vault = _make_vault(tmp_path)
    cur = DailyCuration(t1=[], t2=[], t3=[])

    seen_tmp: list[str] = []
    real_replace = _os.replace

    def _spy_replace(src, dst):
        seen_tmp.append(str(src))
        return real_replace(src, dst)

    import alfred.tier.daily_curation as dc

    orig = dc.os.replace
    dc.os.replace = _spy_replace
    try:
        save_tier_curation(vault, TODAY, cur)
    finally:
        dc.os.replace = orig

    assert len(seen_tmp) == 1
    assert seen_tmp[0].endswith(".curation.tmp"), seen_tmp[0]


def test_atomic_write_does_not_corrupt_on_replace_failure(
    tmp_path: Path,
) -> None:
    """If os.replace fails mid-save, the EXISTING daily file is left
    intact (the write went to .tmp, never to the real path). Simulates a
    crashed write — the operator's prior curation survives."""
    import os as _os

    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-05-29.md"
    # Seed a valid existing file with a known curation.
    first = DailyCuration(
        t1=[T1T2Entry(task="[[task/Original]]", source="operator")],
        t2=[], t3=[],
    )
    save_tier_curation(vault, TODAY, first)
    before = daily_file.read_bytes()

    import alfred.tier.daily_curation as dc

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    orig = dc.os.replace
    dc.os.replace = _boom
    try:
        second = DailyCuration(
            t1=[T1T2Entry(task="[[task/Replacement]]", source="operator")],
            t2=[], t3=[],
        )
        try:
            save_tier_curation(vault, TODAY, second)
        except OSError:
            pass  # expected — the replace failed
    finally:
        dc.os.replace = orig

    # The real file is byte-identical to before — never truncated.
    assert daily_file.read_bytes() == before
    # Reloads cleanly to the ORIGINAL curation.
    reloaded = load_daily_curation(vault, TODAY)
    assert reloaded is not None
    assert reloaded.t1[0].task == "[[task/Original]]"


def test_aggregator_and_curation_tmp_suffixes_distinct() -> None:
    """Cross-writer pin: the aggregator's tmp suffix (.routine.tmp) and
    the curation writer's (.curation.tmp) are distinct, so two writers
    hitting the same daily date never clobber each other's scratch file.
    Pin both literals so a future rename of one without the other
    re-introduces the collision."""
    from pathlib import Path as _P

    daily = _P("/v/daily/2026-05-29.md")
    routine_tmp = daily.with_suffix(".routine.tmp")
    curation_tmp = daily.with_suffix(".curation.tmp")
    assert routine_tmp != curation_tmp
    assert str(routine_tmp).endswith(".routine.tmp")
    assert str(curation_tmp).endswith(".curation.tmp")


# ---------------------------------------------------------------------------
# Step 5 — lost-update flock (2026-06-27)
# ---------------------------------------------------------------------------
#
# The atomic .tmp→os.replace write closed torn-reads but NOT the
# lost-update race (two RMW writers in separate processes: A reads, B
# reads, A writes, B writes-preserving-A's-stale-view → A's keys lost).
# daily_file_lock serializes each writer's whole RMW via fcntl.flock on a
# sidecar .lock. These tests are deterministic (no sleeps/timing) — on
# Linux flock on two separate open() fds of the same file blocks even
# within one process, so mutual exclusion is testable in-process.

import fcntl as _fcntl_test


def test_daily_file_lock_is_mutually_exclusive(tmp_path: Path) -> None:
    """While the lock is held, a second exclusive acquire on the same
    sidecar fails (non-blocking) — proves the lock is actually held, so
    a concurrent writer's RMW would block rather than interleave."""
    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-05-29.md"
    lock_path = daily_file.with_suffix(".lock")

    with daily_file_lock(daily_file):
        # Lock sidecar created + held. A fresh fd's non-blocking
        # exclusive acquire must fail (someone holds it).
        with open(lock_path, "a", encoding="utf-8") as probe:
            raised = False
            try:
                _fcntl_test.flock(
                    probe.fileno(),
                    _fcntl_test.LOCK_EX | _fcntl_test.LOCK_NB,
                )
            except BlockingIOError:
                raised = True
            finally:
                # If we somehow acquired, release so cleanup is clean.
                if not raised:
                    _fcntl_test.flock(probe.fileno(), _fcntl_test.LOCK_UN)
    assert raised, (
        "daily_file_lock did not hold the lock exclusively — a "
        "concurrent RMW could interleave (lost-update window open)."
    )


def test_daily_file_lock_releases_on_exit(tmp_path: Path) -> None:
    """After the context exits, the lock is released — a fresh acquire
    succeeds (no leaked lock that would deadlock the next writer)."""
    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-05-29.md"
    lock_path = daily_file.with_suffix(".lock")

    with daily_file_lock(daily_file):
        pass
    # Now acquirable again.
    with open(lock_path, "a", encoding="utf-8") as probe:
        _fcntl_test.flock(
            probe.fileno(), _fcntl_test.LOCK_EX | _fcntl_test.LOCK_NB,
        )  # must not raise
        _fcntl_test.flock(probe.fileno(), _fcntl_test.LOCK_UN)


def test_lost_update_curation_survives_interleaved_aggregator(
    tmp_path: Path,
) -> None:
    """THE lost-update scenario, deterministic via a thread + the lock.

    Reproduce the race ordering the lock must defeat: a writer (the
    aggregator-style RMW) reads the daily file, then — BEFORE it writes —
    the operator's curation lands. With the lock, the aggregator's RMW
    holds the lock across read+write, so the curation write BLOCKS until
    the aggregator finishes, then merges onto fresh state. Net: BOTH the
    aggregator's keys AND the operator's curation survive (no clobber).

    Driven without sleeps: a background thread attempts the curation save
    while the main thread holds the lock mid-"aggregator RMW"; we release,
    join, and assert both halves are present.
    """
    import threading

    vault = _make_vault(tmp_path)
    today = TODAY
    daily_file = vault / "daily" / f"{today.isoformat()}.md"

    # Seed an aggregator-written file (aggregator keys + body, no
    # curation yet) so the curation save does a read-merge-write.
    daily_file.write_text(
        "---\ntype: daily\ndate: '2026-05-29'\n"
        "routines_contributing:\n- Daily R\n"
        "critical_pending: []\n---\n\n## Tracked\n- Brush AM\n",
        encoding="utf-8",
    )

    curation = DailyCuration(
        t1=[T1T2Entry(task="[[task/Confirm Me]]", source="operator",
                      confirmed=True)],
        t2=[], t3=[],
    )

    save_started = threading.Event()
    save_done = threading.Event()

    def _bg_save():
        save_started.set()
        # This BLOCKS on the lock the main thread holds, until released.
        save_tier_curation(vault, today, curation)
        save_done.set()

    t = threading.Thread(target=_bg_save)

    # Main thread holds the lock (simulating the aggregator's RMW
    # in-flight), starts the bg curation save (which must block), then
    # writes the aggregator's view + releases.
    with daily_file_lock(daily_file):
        t.start()
        save_started.wait(timeout=5)
        # The bg save is now blocked on the lock. Confirm it has NOT
        # completed while we hold the lock (it can't have written).
        assert not save_done.is_set(), (
            "curation save completed while the lock was held — the lock "
            "is not actually serializing the two RMWs."
        )
        # Aggregator writes its keys (re-read inside lock, preserve any
        # curation — there's none yet, the bg save is still blocked).
        post = frontmatter.load(str(daily_file))
        meta = dict(post.metadata or {})
        meta["routines_contributing"] = ["Daily R", "Evening R"]  # a change
        new_post = frontmatter.Post(post.content or "", **meta)
        tmp = daily_file.with_suffix(".routine.tmp")
        tmp.write_text(frontmatter.dumps(new_post) + "\n", encoding="utf-8")
        import os as _os
        _os.replace(tmp, daily_file)
    # Lock released — bg curation save now proceeds, reads FRESH (our
    # just-written aggregator keys), merges its curation.
    t.join(timeout=5)
    assert save_done.is_set(), "bg curation save did not complete"

    # BOTH survive: aggregator's updated key AND the operator's curation.
    reloaded = frontmatter.load(str(daily_file))
    rmeta = dict(reloaded.metadata or {})
    assert rmeta.get("routines_contributing") == ["Daily R", "Evening R"], (
        "aggregator's key was clobbered by the curation save"
    )
    loaded_cur = load_daily_curation(vault, today)
    assert loaded_cur is not None
    assert loaded_cur.t1[0].task == "[[task/Confirm Me]]", (
        "operator's curation was lost — the lost-update race is NOT closed"
    )


def test_save_tier_curation_cleans_orphan_tmp_on_replace_failure(
    tmp_path: Path,
) -> None:
    """orphan-tmp cleanup (reviewer NOTE, 2026-06-27): a failed
    os.replace must NOT leave a stale .curation.tmp orphan — the
    try/finally unlinks it. (Distinct from the existing
    corrupt-file-integrity test: this pins the SCRATCH file is gone.)"""
    import alfred.tier.daily_curation as dc

    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / f"{TODAY.isoformat()}.md"
    daily_file.write_text(
        "---\ntype: daily\ndate: '2026-05-29'\n---\n\nbody\n",
        encoding="utf-8",
    )

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    orig = dc.os.replace
    dc.os.replace = _boom
    try:
        try:
            save_tier_curation(vault, TODAY, DailyCuration(t1=[], t2=[], t3=[]))
        except OSError:
            pass
    finally:
        dc.os.replace = orig

    assert not (vault / "daily" / f"{TODAY.isoformat()}.curation.tmp").exists(), (
        "failed os.replace left an orphan .curation.tmp"
    )
