"""Arc #20 (2026-07-22) — free-text T3 ad-hoc done-state (P1 schema + writer).

Covers the ``done_at`` field on :class:`alfred.tier.daily_curation.T3Entry`
plus the deterministic mutators :func:`mark_t3_done` / :func:`mark_t3_undone`:

  * Schema — ``to_dict`` drops ``done_at`` when None (byte-stability for
    the common unmarked case), emits it when set; ``from_dict`` reads it
    schema-tolerantly; save → load round-trip preserves it.
  * Aggregator-preserve — the routine aggregator's read-preserve path
    (``_load_existing_tier_curation``) carries a nested ``done_at``
    verbatim across its 05:59 pass (no aggregator change needed).
  * Mutator ``kind`` matrix — success / idempotent_noop / unknown_item /
    ambiguous_item / future_date_rejected + back-date lands in the
    correct day file; undo unmarked / not_marked. The honest #19
    dead-end (``unknown_item``) is preserved for the truly-untracked item.
  * RMW correctness — marking one T3 item done preserves sibling entries
    and all other frontmatter keys + body (the locked read-modify-write).
  * Log emission — the observability events are pinned via
    ``structlog.testing.capture_logs`` per
    ``feedback_log_emission_test_pattern`` (a done/undo flip is
    operator-grep-load-bearing per ``feedback_intentionally_left_blank``).

Contract-first (per ``feedback_regression_pin_unconditional`` +
``feedback_worked_example_accuracy``): walked against the real T3 shape
(``item:`` + ``source:`` + optional ``done_at:``), no dep-gated skips.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import frontmatter
import structlog

from alfred.routine.aggregator import _load_existing_tier_curation
from alfred.tier.daily_curation import (
    DailyCuration,
    T3Entry,
    TIER_DONE_KIND_AMBIGUOUS_ITEM,
    TIER_DONE_KIND_FUTURE_DATE_REJECTED,
    TIER_DONE_KIND_IDEMPOTENT_NOOP,
    TIER_DONE_KIND_SUCCESS,
    TIER_DONE_KIND_UNKNOWN_ITEM,
    TIER_UNDONE_KIND_NOT_MARKED,
    TIER_UNDONE_KIND_UNMARKED,
    TierDoneResult,
    load_daily_curation,
    mark_t3_done,
    mark_t3_undone,
    save_tier_curation,
)


TODAY = date(2026, 7, 22)
YESTERDAY = date(2026, 7, 21)
FUTURE = date(2026, 7, 25)


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    return vault


# ---------------------------------------------------------------------------
# Kind constant pins — cross-agent contract (talker dispatcher + SKILL)
# ---------------------------------------------------------------------------


def test_tier_done_kind_constants_pinned() -> None:
    """The ``kind`` discriminator strings are a cross-agent contract the
    talker dispatcher + vault-talker SKILL route on verbatim. A rename =
    update both in lockstep. Mirrors the routine ``DONE_KIND_*`` values."""
    assert TIER_DONE_KIND_SUCCESS == "success"
    assert TIER_DONE_KIND_IDEMPOTENT_NOOP == "idempotent_noop"
    assert TIER_DONE_KIND_AMBIGUOUS_ITEM == "ambiguous_item"
    assert TIER_DONE_KIND_UNKNOWN_ITEM == "unknown_item"
    assert TIER_DONE_KIND_FUTURE_DATE_REJECTED == "future_date_rejected"
    assert TIER_UNDONE_KIND_UNMARKED == "unmarked"
    assert TIER_UNDONE_KIND_NOT_MARKED == "not_marked"


# ---------------------------------------------------------------------------
# Schema — to_dict / from_dict / round-trip
# ---------------------------------------------------------------------------


def test_to_dict_drops_done_at_when_none() -> None:
    """An open item stays a clean two-key ``{item, source}`` — byte
    stability for every never-marked-done item (the common case)."""
    e = T3Entry(item="Rake leaves", source="operator-adhoc")
    assert e.to_dict() == {"item": "Rake leaves", "source": "operator-adhoc"}


def test_to_dict_emits_done_at_when_set() -> None:
    e = T3Entry(item="Rake leaves", source="operator-adhoc", done_at="2026-07-22")
    assert e.to_dict() == {
        "item": "Rake leaves",
        "source": "operator-adhoc",
        "done_at": "2026-07-22",
    }


def test_from_dict_reads_done_at() -> None:
    e = T3Entry.from_dict(
        {"item": "X", "source": "operator", "done_at": "2026-07-01"}
    )
    assert e.done_at == "2026-07-01"


def test_from_dict_tolerates_absent_done_at() -> None:
    """Additive field — a pre-Arc-#20 entry (no ``done_at`` key) loads
    with ``done_at is None``."""
    e = T3Entry.from_dict({"item": "X", "source": "operator"})
    assert e.done_at is None


def test_from_dict_null_done_at_is_none() -> None:
    e = T3Entry.from_dict({"item": "X", "source": "operator", "done_at": None})
    assert e.done_at is None


def test_round_trip_preserves_done_at(tmp_path: Path) -> None:
    """save → load round-trip preserves ``done_at`` on the T3 entry."""
    vault = _make_vault(tmp_path)
    cur = DailyCuration(t3=[
        T3Entry(item="Rake leaves", source="operator-adhoc", done_at="2026-07-22"),
        T3Entry(item="Read for an hour", source="aspirational"),
    ])
    save_tier_curation(vault, TODAY, cur)
    loaded = load_daily_curation(vault, TODAY)
    assert loaded is not None
    by = {e.item: e.done_at for e in loaded.t3}
    assert by == {"Rake leaves": "2026-07-22", "Read for an hour": None}


def test_open_item_byte_stable_on_disk(tmp_path: Path) -> None:
    """An unmarked T3 item writes NO ``done_at`` key to disk — byte
    stability so existing daily files don't churn on an unrelated save."""
    vault = _make_vault(tmp_path)
    cur = DailyCuration(t3=[T3Entry(item="Read for an hour", source="aspirational")])
    save_tier_curation(vault, TODAY, cur)
    raw = (vault / "daily" / "2026-07-22.md").read_text(encoding="utf-8")
    assert "done_at" not in raw


# ---------------------------------------------------------------------------
# Aggregator preserve — nested done_at survives the 05:59 read-preserve pass
# ---------------------------------------------------------------------------


def test_aggregator_preserves_nested_done_at_verbatim(tmp_path: Path) -> None:
    """The routine aggregator's read-preserve helper returns the
    ``tier_curation`` dict verbatim — including a nested ``done_at`` — so
    the aggregator's daily fire never clobbers a talker-set done-state.
    No aggregator change needed (design §4 point 3)."""
    vault = _make_vault(tmp_path)
    cur = DailyCuration(t3=[
        T3Entry(item="Rake leaves", source="operator-adhoc", done_at="2026-07-22"),
    ])
    save_tier_curation(vault, TODAY, cur)
    daily_file = vault / "daily" / "2026-07-22.md"

    preserved = _load_existing_tier_curation(daily_file)
    assert preserved is not None
    assert preserved["t3"][0]["done_at"] == "2026-07-22"


# ---------------------------------------------------------------------------
# mark_t3_done — the kind matrix
# ---------------------------------------------------------------------------


def _seed(vault: Path, day: date, items: list[tuple[str, str]]) -> None:
    """Seed ``day``'s daily file with the given (item, source) T3 list."""
    cur = DailyCuration(t3=[T3Entry(item=i, source=s) for i, s in items])
    save_tier_curation(vault, day, cur)


def test_mark_t3_done_success(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    res = mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_SUCCESS
    assert res.item == "Rake leaves"  # canonical form, not the fuzzy query
    assert res.done_at == "2026-07-22"
    assert res.date == "2026-07-22"
    # Persisted.
    loaded = load_daily_curation(vault, TODAY)
    assert loaded.t3[0].done_at == "2026-07-22"


def test_mark_t3_done_idempotent_noop(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    res = mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_IDEMPOTENT_NOOP
    assert res.item == "Rake leaves"


def test_mark_t3_done_unknown_item_preserves_honest_deadend(tmp_path: Path) -> None:
    """No T3 item matches → unknown_item + the day's full T3 list as
    candidates (the honest #19 'I checked the tier list too' close)."""
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc"), ("Read", "aspirational")])
    res = mark_t3_done(vault, "wash the car", completed_at=TODAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_UNKNOWN_ITEM
    assert set(res.candidates) == {"Rake leaves", "Read"}
    # Nothing was mutated.
    loaded = load_daily_curation(vault, TODAY)
    assert all(e.done_at is None for e in loaded.t3)


def test_mark_t3_done_no_curation_is_unknown(tmp_path: Path) -> None:
    """No daily file / no curation for the date → unknown_item (the
    truly-untracked item; #19's honest dead-end stays intact)."""
    vault = _make_vault(tmp_path)
    res = mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_UNKNOWN_ITEM


def test_mark_t3_done_ambiguous_item(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [
        ("Read for an hour", "operator-adhoc"),
        ("Read the news", "operator-adhoc"),
    ])
    res = mark_t3_done(vault, "read", completed_at=TODAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_AMBIGUOUS_ITEM
    assert set(res.candidates) == {"Read for an hour", "Read the news"}
    # Ambiguity does NOT mutate — the operator gets asked back.
    loaded = load_daily_curation(vault, TODAY)
    assert all(e.done_at is None for e in loaded.t3)


def test_mark_t3_done_future_date_rejected(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, FUTURE, [("Rake leaves", "operator-adhoc")])
    res = mark_t3_done(vault, "rake leaves", completed_at=FUTURE, today=TODAY)
    assert res.kind == TIER_DONE_KIND_FUTURE_DATE_REJECTED
    assert res.date == "2026-07-25"


def test_mark_t3_done_backdate_lands_in_correct_day_file(tmp_path: Path) -> None:
    """A back-date resolves the item on the day it was curated (yesterday's
    file), stamping done_at there — NOT on today's file."""
    vault = _make_vault(tmp_path)
    _seed(vault, YESTERDAY, [("Rake leaves", "operator-adhoc")])
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])  # a fresh copy today
    res = mark_t3_done(vault, "rake leaves", completed_at=YESTERDAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_SUCCESS
    assert res.done_at == "2026-07-21"
    # Yesterday's file marked; today's file untouched.
    assert load_daily_curation(vault, YESTERDAY).t3[0].done_at == "2026-07-21"
    assert load_daily_curation(vault, TODAY).t3[0].done_at is None


def test_mark_t3_done_empty_query(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    res = mark_t3_done(vault, "   ", completed_at=TODAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_UNKNOWN_ITEM


def test_mark_t3_done_fuzzy_stem_tolerant(tmp_path: Path) -> None:
    """Reuses the routine matcher — past-tense phrasing stems to the item
    ('raked the leaves' → 'rake leaves')."""
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    res = mark_t3_done(vault, "I raked the leaves", completed_at=TODAY, today=TODAY)
    assert res.kind == TIER_DONE_KIND_SUCCESS
    assert res.item == "Rake leaves"


# ---------------------------------------------------------------------------
# RMW correctness — siblings + other frontmatter survive the flip
# ---------------------------------------------------------------------------


def test_mark_t3_done_preserves_sibling_entries(tmp_path: Path) -> None:
    """Marking one T3 item done leaves every other T3 entry intact — the
    locked read-modify-write can't drop or mangle siblings."""
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [
        ("Rake leaves", "operator-adhoc"),
        ("Read for an hour", "aspirational"),
        ("Call mum", "operator"),
    ])
    mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    loaded = load_daily_curation(vault, TODAY)
    by = {e.item: e.done_at for e in loaded.t3}
    assert by == {
        "Rake leaves": "2026-07-22",
        "Read for an hour": None,
        "Call mum": None,
    }
    # Order preserved.
    assert [e.item for e in loaded.t3] == ["Rake leaves", "Read for an hour", "Call mum"]


def test_mark_t3_done_preserves_other_frontmatter_and_body(tmp_path: Path) -> None:
    """The aggregator-owned frontmatter keys + body survive the flip
    (the RMW only touches the nested tier_curation ``done_at``)."""
    vault = _make_vault(tmp_path)
    daily_file = vault / "daily" / "2026-07-22.md"
    daily_file.write_text(
        "---\n"
        "type: daily\n"
        "date: 2026-07-22\n"
        "routines_contributing: [For Self Health]\n"
        "critical_pending: 2\n"
        "tier_curation:\n"
        "  t3:\n"
        "    - item: Rake leaves\n"
        "      source: operator-adhoc\n"
        "---\n\n## Routines\n- something the aggregator wrote\n",
        encoding="utf-8",
    )
    mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    post = frontmatter.load(str(daily_file))
    assert post.metadata["type"] == "daily"
    assert post.metadata["routines_contributing"] == ["For Self Health"]
    assert post.metadata["critical_pending"] == 2
    assert "something the aggregator wrote" in post.content
    assert post.metadata["tier_curation"]["t3"][0]["done_at"] == "2026-07-22"


# ---------------------------------------------------------------------------
# mark_t3_undone — the inverse
# ---------------------------------------------------------------------------


def test_mark_t3_undone_unmarked(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    res = mark_t3_undone(vault, "rake leaves", on_date=TODAY)
    assert res.kind == TIER_UNDONE_KIND_UNMARKED
    assert res.item == "Rake leaves"
    assert load_daily_curation(vault, TODAY).t3[0].done_at is None


def test_mark_t3_undone_not_marked_is_not_error(tmp_path: Path) -> None:
    """Un-checking an already-open item → not_marked (idempotent, NOT an
    error) — distinct from done's idempotent_noop so the talker can voice
    'that wasn't checked off, nothing to undo'."""
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    res = mark_t3_undone(vault, "rake leaves", on_date=TODAY)
    assert res.kind == TIER_UNDONE_KIND_NOT_MARKED
    assert res.item == "Rake leaves"


def test_mark_t3_undone_unknown_item(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    res = mark_t3_undone(vault, "wash the car", on_date=TODAY)
    assert res.kind == TIER_DONE_KIND_UNKNOWN_ITEM


def test_mark_t3_undone_ambiguous_item(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [
        ("Read for an hour", "operator-adhoc"),
        ("Read the news", "operator-adhoc"),
    ])
    res = mark_t3_undone(vault, "read", on_date=TODAY)
    assert res.kind == TIER_DONE_KIND_AMBIGUOUS_ITEM


# ---------------------------------------------------------------------------
# Log-emission pins — observability is load-bearing (ILB principle)
# ---------------------------------------------------------------------------


def test_mark_t3_done_success_emits_log(tmp_path: Path) -> None:
    """Per ``feedback_log_emission_test_pattern`` — drive the production
    path + assert the named event fires with key fields, so a future
    refactor that drops the line fails loud (operator grep depends on it)."""
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    with structlog.testing.capture_logs() as captured:
        mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    matches = [c for c in captured if c.get("event") == "tier.mark_t3_done.success"]
    assert len(matches) == 1
    assert matches[0]["item"] == "Rake leaves"
    assert matches[0]["date"] == "2026-07-22"


def test_mark_t3_done_unknown_item_emits_log(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    with structlog.testing.capture_logs() as captured:
        mark_t3_done(vault, "wash the car", completed_at=TODAY, today=TODAY)
    matches = [
        c for c in captured if c.get("event") == "tier.mark_t3_done.unknown_item"
    ]
    assert len(matches) == 1
    assert matches[0]["t3_count"] == 1


def test_mark_t3_done_future_rejected_emits_log(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    with structlog.testing.capture_logs() as captured:
        mark_t3_done(vault, "rake leaves", completed_at=FUTURE, today=TODAY)
    matches = [
        c for c in captured
        if c.get("event") == "tier.mark_t3_done.future_date_rejected"
    ]
    assert len(matches) == 1
    assert matches[0]["date"] == "2026-07-25"
    assert matches[0]["today"] == "2026-07-22"


def test_mark_t3_undone_unmarked_emits_log(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _seed(vault, TODAY, [("Rake leaves", "operator-adhoc")])
    mark_t3_done(vault, "rake leaves", completed_at=TODAY, today=TODAY)
    with structlog.testing.capture_logs() as captured:
        mark_t3_undone(vault, "rake leaves", on_date=TODAY)
    matches = [
        c for c in captured if c.get("event") == "tier.mark_t3_undone.unmarked"
    ]
    assert len(matches) == 1
    assert matches[0]["item"] == "Rake leaves"
    assert matches[0]["was_done_at"] == "2026-07-22"


# ---------------------------------------------------------------------------
# Result dataclass shape
# ---------------------------------------------------------------------------


def test_tier_done_result_defaults() -> None:
    """Empty-candidate default is a list, not None (safe to iterate)."""
    r = TierDoneResult(kind=TIER_DONE_KIND_SUCCESS)
    assert r.candidates == []
    assert r.item is None and r.date is None and r.done_at is None
