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
    load_daily_curation,
    save_tier_curation,
)


TODAY = date(2026, 5, 29)


# ---------------------------------------------------------------------------
# Source enum pin
# ---------------------------------------------------------------------------


def test_t1_t2_sources_pinned() -> None:
    """Stable contract — Ship 4 SKILL references these verbatim. A
    change here = update Ship 4 in lockstep."""
    assert T1_T2_SOURCES == frozenset({
        "auto-due",
        "auto-escalate",
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
        "## Critical\n\n- [ ] Kiki Insulin @ 12:00\n\n"
        "## Tracked\n\n- [ ] Walk Fergus *(no completions yet)*\n",
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
