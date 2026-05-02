"""Tests for the KAL-LE distiller-radar Phase 2 synthesis ranker.

Verifies the deterministic mechanical ranking covers:
- Each score term contributes correctly in isolation
- Default weights produce the expected ordering on a fixture vault
- Operator weight overrides reshape the ranking
- Recency cliff (records outside window) zero only the recency term
- Empty / partial / malformed inputs degrade gracefully
- Wikilink dedup, alias-stripping, type discovery from directory
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.distiller.synthesis_ranker import (
    RankedRecord,
    ScoreBreakdown,
    rank_synthesis_records,
    summary_from_record,
)


NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _record(
    *,
    name: str,
    record_type: str,
    created: str | None = "2026-04-30",
    claim: str = "Default claim text.",
    source_links: list[str] | None = None,
    entity_links: list[str] | None = None,
    body: str = "",
    extra_fm: dict[str, str] | None = None,
) -> str:
    """Build a synthetic learn-record markdown string."""
    fm_lines = [f"name: {name}", f"type: {record_type}", f"claim: {claim}"]
    if created is not None:
        fm_lines.append(f"created: '{created}'")
    if source_links:
        fm_lines.append("source_links:")
        for link in source_links:
            fm_lines.append(f"  - '{link}'")
    if entity_links:
        fm_lines.append("entity_links:")
        for link in entity_links:
            fm_lines.append(f"  - '{link}'")
    if extra_fm:
        for k, v in extra_fm.items():
            fm_lines.append(f"{k}: {v}")
    head = "---\n" + "\n".join(fm_lines) + "\n---\n"
    return head + "\n" + body


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    for d in ("synthesis", "decision", "contradiction"):
        (tmp_path / d).mkdir()
    return tmp_path


def _write(vault: Path, record_type: str, name: str, content: str) -> Path:
    path = vault / record_type / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Empty + missing
# ---------------------------------------------------------------------------


def test_empty_vault_returns_empty_list(empty_vault: Path) -> None:
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert out == []


def test_missing_dirs_skipped(tmp_path: Path) -> None:
    # No synthesis/decision/contradiction subdirectories at all.
    assert rank_synthesis_records(tmp_path, now=NOW) == []


def test_gitkeep_not_counted(empty_vault: Path) -> None:
    (empty_vault / "synthesis" / ".gitkeep").write_text("", encoding="utf-8")
    assert rank_synthesis_records(empty_vault, now=NOW) == []


# ---------------------------------------------------------------------------
# Score formula — per-term isolation
# ---------------------------------------------------------------------------


def test_cross_source_term_weighted_3x(empty_vault: Path) -> None:
    """Two source_links → cross_source = 2 * 3.0 = 6.0."""
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        source_links=["[[session/A]]", "[[session/B]]"],
        entity_links=[],
        created=NOW.date().isoformat(),
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert len(out) == 1
    assert out[0].source_count == 2
    assert out[0].breakdown.cross_source == pytest.approx(6.0)


def test_entity_diversity_term_weighted_2x(empty_vault: Path) -> None:
    """Three distinct entity_links → entity_diversity = 3 * 2.0 = 6.0."""
    _write(empty_vault, "decision", "rec", _record(
        name="rec",
        record_type="decision",
        source_links=[],
        entity_links=["[[project/Alfred]]", "[[project/RRTS]]", "[[person/X]]"],
        created=NOW.date().isoformat(),
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert out[0].entity_count == 3
    assert out[0].breakdown.entity_diversity == pytest.approx(6.0)


def test_entity_diversity_dedups_aliases(empty_vault: Path) -> None:
    """``[[project/Alfred|x]]`` and ``[[project/Alfred]]`` count once."""
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        entity_links=["[[project/Alfred]]", "[[project/Alfred|alias]]"],
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert out[0].entity_count == 1


def test_recency_full_credit_today(empty_vault: Path) -> None:
    """Created same instant as now → recency = 1.0 * weight (= 1.0 default).

    Bare-date ``created`` gets parsed as 00:00 UTC, so to test the
    "exactly now" case we pass a full ISO timestamp matching NOW.
    """
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        created=NOW.isoformat(),
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    # 0.5^(0/7) = 1.0; default weight 1.0 → 1.0
    assert out[0].breakdown.recency == pytest.approx(1.0, abs=0.001)


def test_recency_today_bare_date_close_to_one(empty_vault: Path) -> None:
    """Bare date created today (00:00 UTC) with NOW at 12:00 → ~0.95.

    Verifies the half-life slope: 0.5 days old → 0.5^(0.5/7) ≈ 0.95.
    """
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        created=NOW.date().isoformat(),
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert 0.94 <= out[0].breakdown.recency <= 0.97


def test_recency_half_at_seven_days(empty_vault: Path) -> None:
    """7-day half-life: created exactly 7 days ago → recency = 0.5."""
    seven_days_ago = (NOW - timedelta(days=7)).date().isoformat()
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        created=seven_days_ago,
    ))
    # window_days=10 so the record is still IN window (recency != 0)
    out = rank_synthesis_records(empty_vault, window_days=10, now=NOW)
    # 0.5^(7/7) = 0.5 — but the record was created at 00:00 UTC and NOW
    # is at 12:00, so age_days is 7.5. 0.5^(7.5/7) ≈ 0.476.
    assert 0.45 <= out[0].breakdown.recency <= 0.51


def test_recency_zeroed_outside_window(empty_vault: Path) -> None:
    """Older than window_days → recency=0 but other terms still scored."""
    _write(empty_vault, "synthesis", "old", _record(
        name="old",
        record_type="synthesis",
        created="2020-01-01",
        source_links=["[[session/x]]"],
    ))
    out = rank_synthesis_records(empty_vault, window_days=7, now=NOW)
    assert out[0].breakdown.recency == 0.0
    # Cross-source still earned: 1 * 3.0 = 3.0
    assert out[0].breakdown.cross_source == pytest.approx(3.0)
    # And type still earned: synthesis = 3.0 * 1.0 = 3.0
    assert out[0].breakdown.type_weight == pytest.approx(3.0)


def test_recency_missing_created_field_zero(empty_vault: Path) -> None:
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        created=None,
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert out[0].breakdown.recency == 0.0


def test_type_weight_synthesis_highest(empty_vault: Path) -> None:
    """synthesis=3, contradiction=2, decision=1, all multiplied by w_type=1.0."""
    for rt in ("synthesis", "decision", "contradiction"):
        _write(empty_vault, rt, "rec", _record(
            name="rec",
            record_type=rt,
            created=NOW.date().isoformat(),
        ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    by_type = {r.record_type: r.breakdown.type_weight for r in out}
    assert by_type["synthesis"] == 3.0
    assert by_type["contradiction"] == 2.0
    assert by_type["decision"] == 1.0


# ---------------------------------------------------------------------------
# Ordering — deterministic
# ---------------------------------------------------------------------------


def test_ordering_is_score_desc_then_created_desc(empty_vault: Path) -> None:
    """Higher score first; equal scores break by newer-created first."""
    # Big score: 4 sources, 3 entities, recent
    _write(empty_vault, "synthesis", "big", _record(
        name="big",
        record_type="synthesis",
        source_links=["[[s/1]]", "[[s/2]]", "[[s/3]]", "[[s/4]]"],
        entity_links=["[[e/1]]", "[[e/2]]", "[[e/3]]"],
        created=NOW.date().isoformat(),
    ))
    # Small score: 1 source, 1 entity
    _write(empty_vault, "decision", "small", _record(
        name="small",
        record_type="decision",
        source_links=["[[s/x]]"],
        entity_links=["[[e/x]]"],
        created=NOW.date().isoformat(),
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert [r.path.stem for r in out] == ["big", "small"]


def test_top_n_caps_results(empty_vault: Path) -> None:
    for i in range(5):
        _write(empty_vault, "synthesis", f"rec{i}", _record(
            name=f"rec{i}",
            record_type="synthesis",
            source_links=[f"[[s/{j}]]" for j in range(i + 1)],
            created=NOW.date().isoformat(),
        ))
    out = rank_synthesis_records(empty_vault, top_n=3, now=NOW)
    assert len(out) == 3
    # Highest source_count first.
    assert [r.path.stem for r in out] == ["rec4", "rec3", "rec2"]


# ---------------------------------------------------------------------------
# Operator weight overrides
# ---------------------------------------------------------------------------


def test_weights_override_changes_ranking(empty_vault: Path) -> None:
    """Setting cross_source weight to 0 should flip ordering when only
    cross-source separates two records."""
    _write(empty_vault, "synthesis", "many_sources", _record(
        name="many_sources",
        record_type="synthesis",
        source_links=["[[s/1]]", "[[s/2]]", "[[s/3]]"],
        entity_links=["[[e/1]]"],
        created=NOW.date().isoformat(),
    ))
    _write(empty_vault, "synthesis", "many_entities", _record(
        name="many_entities",
        record_type="synthesis",
        source_links=["[[s/1]]"],
        entity_links=["[[e/1]]", "[[e/2]]", "[[e/3]]"],
        created=NOW.date().isoformat(),
    ))
    # Default weights: many_sources wins (3*3 + 1*2 = 11) vs (1*3 + 3*2 = 9)
    default = rank_synthesis_records(empty_vault, now=NOW)
    assert default[0].path.stem == "many_sources"
    # Zero out cross_source: many_entities wins (0 + 6 = 6) vs (0 + 2 = 2)
    flipped = rank_synthesis_records(
        empty_vault, weights={"cross_source": 0}, now=NOW,
    )
    assert flipped[0].path.stem == "many_entities"


def test_invalid_weight_falls_back_to_default(empty_vault: Path) -> None:
    """A non-numeric weight value silently uses the default (logged)."""
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        source_links=["[[s/1]]", "[[s/2]]"],
    ))
    out = rank_synthesis_records(
        empty_vault, weights={"cross_source": "garbage"}, now=NOW,
    )
    # Default weight 3.0 still applied: 2 * 3.0 = 6.0
    assert out[0].breakdown.cross_source == pytest.approx(6.0)


def test_unknown_weight_keys_ignored(empty_vault: Path) -> None:
    _write(empty_vault, "synthesis", "rec", _record(
        name="rec",
        record_type="synthesis",
        source_links=["[[s/1]]"],
    ))
    out = rank_synthesis_records(
        empty_vault, weights={"unknown_term": 100.0}, now=NOW,
    )
    # Default weights all in effect — 1*3 + 0*2 + recency + 3*1
    assert out[0].breakdown.cross_source == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Robustness — malformed input
# ---------------------------------------------------------------------------


def test_corrupt_record_skipped_not_crashed(empty_vault: Path) -> None:
    """A file with no frontmatter and one with valid frontmatter — only
    the valid one comes back."""
    (empty_vault / "synthesis" / "broken.md").write_text(
        "garbage with no frontmatter at all\n",
        encoding="utf-8",
    )
    _write(empty_vault, "synthesis", "good", _record(
        name="good",
        record_type="synthesis",
        source_links=["[[s/1]]"],
    ))
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert len(out) == 2  # frontmatter parses both — broken just has empty fm
    # The good one outranks (broken record has 0 source_links / entity_links)
    assert out[0].path.stem == "good"


def test_subdirectories_walked(empty_vault: Path) -> None:
    """Records in a nested subdir under synthesis/ are still found."""
    nested = empty_vault / "synthesis" / "by-date" / "2026-04"
    nested.mkdir(parents=True)
    (nested / "deep.md").write_text(_record(
        name="deep",
        record_type="synthesis",
        source_links=["[[s/1]]"],
    ), encoding="utf-8")
    out = rank_synthesis_records(empty_vault, now=NOW)
    assert len(out) == 1
    assert out[0].path.stem == "deep"


# ---------------------------------------------------------------------------
# summary_from_record helper
# ---------------------------------------------------------------------------


def test_summary_prefers_claim_field() -> None:
    fm = {"claim": "  Cached signal.  ", "summary": "alt"}
    assert summary_from_record(fm, "body text") == "Cached signal."


def test_summary_falls_back_to_summary_field() -> None:
    fm = {"summary": "summary text"}
    assert summary_from_record(fm, "body") == "summary text"


def test_summary_falls_back_to_first_paragraph() -> None:
    body = dedent("""\
        <!-- comment -->
        # Heading
        First real paragraph here.
        Second line of same paragraph.

        Second paragraph not included.
    """)
    out = summary_from_record({}, body)
    assert "First real paragraph here." in out
    assert "Second line of same paragraph." in out
    assert "Second paragraph" not in out
    assert "Heading" not in out


def test_summary_empty_when_no_signal() -> None:
    assert summary_from_record({}, "") == ""


# ---------------------------------------------------------------------------
# ScoreBreakdown.total
# ---------------------------------------------------------------------------


def test_score_breakdown_total_sums() -> None:
    sb = ScoreBreakdown(
        cross_source=1.0, entity_diversity=2.0, recency=3.0, type_weight=4.0,
    )
    assert sb.total() == 10.0


def test_negative_top_n_returns_empty_not_mangled(empty_vault: Path) -> None:
    """Negative ``top_n`` must clamp to 0, not return ``candidates[:-N]``.

    Without the ``max(0, top_n)`` guard, ``rank_synthesis_records(..., top_n=-5)``
    would return all-but-last-5 records (silent corruption of the digest).
    """
    for i in range(5):
        _write(empty_vault, "synthesis", f"rec{i}", _record(
            name=f"rec{i}",
            record_type="synthesis",
            source_links=[f"[[s/{j}]]" for j in range(i + 1)],
            created=NOW.date().isoformat(),
        ))
    out = rank_synthesis_records(empty_vault, top_n=-5, now=NOW)
    assert out == []
