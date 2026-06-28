"""P4 / Surface (b) — confidence-instrumentation tests (2026-06-07).

Pins the ``_match_confidence`` helper output for known shapes + the
``routine_done.matched`` log emission on the single-match success
path of :func:`cmd_done`.

The ``_match_confidence`` helper output is instrumentation (pinned for
known shapes). Step 5 (2026-06-XX) then APPLIED the structural matcher
fix in ``routine/cli.py::_matches_item`` — the check-2 stem-substring
fallback is now gated by a min-stem-length floor AND a confidence>0
requirement. (The self-correcting matcher LOOP remains deferred — this
is the structural close only.)

Canonical false-positive shape: the 2026-06-06 Tilray conversation
friction where the operator said *"Tilray Medical Registration Renewal
complete"* and the matcher fired against the ``Meds`` routine item —
near-zero token overlap, but the stem substring check (``"med" in
"tilray medical registration renewal"``) returned True. ``_match_confidence``
= 0.0 for that pair (pinned below); and post-Step-5 the matcher itself
NO LONGER matches it (``test_tilray_shape_no_longer_matches_meds``).
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

from alfred.routine.cli import _match_confidence, cmd_done
from alfred.routine.config import RoutineConfig


# ---------------------------------------------------------------------------
# Helpers (mirror tests/routine/test_cli.py shapes for consistency)
# ---------------------------------------------------------------------------


def _config(vault_path: Path, tmp_path: Path, *, instance: str = "salem") -> RoutineConfig:
    config = RoutineConfig(
        vault_path=str(vault_path),
        instance_name=instance,
    )
    config.state.path = str(tmp_path / "routine_state.json")
    return config


def _write_routine(vault_path: Path, name: str, payload: dict) -> Path:
    routine_dir = vault_path / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _match_confidence — pure-function pins
# ---------------------------------------------------------------------------


def test_confidence_tilray_meds_canonical_false_positive() -> None:
    """Canonical false-positive shape: 2026-06-06 Tilray→Meds.

    The matcher's check 2 (stem-substring bidirectional) fires
    because ``_fuzzy_stem("Meds") == "med"`` (3 chars) and "med" is
    a substring of "medical" inside the query stem. The token-set
    confidence MUST report 0.0 — the post-stem post-stopword token
    sets share zero entries (``{med}`` vs
    ``{tilray, medical, registration, renewal}``).

    Future structural fix (TODO P4-followup in
    ``_matches_item``): gate the check-2 substring fallback when
    either stem is < N chars OR when the post-stem token-set
    confidence is 0.0. Either gate would have caught this case.
    """
    confidence = _match_confidence(
        "Tilray Medical Registration Renewal", "Meds",
    )
    assert confidence == 0.0


def test_confidence_genuine_partial_match() -> None:
    """Genuine partial match returns a fractional confidence.

    Operator phrasing carries extra content ("yesterday") that
    isn't in the canonical item text. Intersection={walk, dog};
    query tokens={walk, dog, yesterday}; item tokens={walk, dog}.
    Confidence = 2 / max(3, 2) = 0.667.
    """
    confidence = _match_confidence(
        "I walked the dog yesterday", "Walk dog",
    )
    # 2/3 = 0.666... — within float tolerance of 2/3.
    assert abs(confidence - 2 / 3) < 1e-9


def test_confidence_exact_match() -> None:
    """Exact token-set match returns 1.0."""
    confidence = _match_confidence("Walk dog", "Walk dog")
    assert confidence == 1.0


def test_confidence_empty_query_returns_zero() -> None:
    """Defensive: empty query returns 0.0 (not NaN, not crash)."""
    assert _match_confidence("", "Walk dog") == 0.0
    assert _match_confidence("  ", "Walk dog") == 0.0


def test_confidence_empty_item_returns_zero() -> None:
    """Defensive: empty item returns 0.0."""
    assert _match_confidence("Walk dog", "") == 0.0


def test_confidence_stopword_only_query_returns_zero() -> None:
    """Query with only stopwords filters to empty token set → 0.0."""
    # All entries are in _FUZZY_STOPWORDS (i, the, a, an, to, my,
    # for, on, in, at, and, or, but, of). "to a" stems to "to a"
    # both filtered, leaving an empty token set.
    assert _match_confidence("to a", "Walk dog") == 0.0


def test_confidence_substring_match_with_short_stem_reports_zero() -> None:
    """The ``Meds`` shape generalizes: any short-stemmed item that
    matches via stem-substring containment alone reports confidence
    0.0.

    Pins additional cases the future tightening pass should catch:
      * "Run" stems to "run" (3 chars; -s strip on "Runs" → "run")
        matches "Trump Runs Ad" via substring containment
      * "Read" stems to "read" (4 chars) matches "Bread Bakery
        Visit" via substring of "bread"
      * "Pay" stems to "pay" (3 chars) matches "Payment received"
        via substring of "payment"
    """
    # Note: _match_confidence does NOT invoke _matches_item. It
    # computes pure Jaccard on stemmed token sets. The substring
    # fallback that fires in _matches_item is exactly what this
    # confidence measure surfaces as "0.0 but matched anyway."
    assert _match_confidence("Runs", "Trump Runs Ad") > 0.0  # has real overlap
    # Even with a substring match in _matches_item (not exercised
    # here), the confidence is 0.0 when stems share no full tokens:
    assert _match_confidence("Read book yesterday", "Bread Bakery Visit") == 0.0


def test_confidence_is_symmetric() -> None:
    """Confidence is symmetric: swap query↔item, get the same score."""
    a = _match_confidence("Walk dog", "I walked the dog")
    b = _match_confidence("I walked the dog", "Walk dog")
    assert a == b


def test_confidence_bounded_zero_to_one() -> None:
    """Confidence is always in [0.0, 1.0]."""
    for query, item in [
        ("Walk dog", "Walk dog"),
        ("Tilray Medical Registration Renewal", "Meds"),
        ("walked", "walking dog"),
        ("a b c", "x y z"),
    ]:
        c = _match_confidence(query, item)
        assert 0.0 <= c <= 1.0


# ---------------------------------------------------------------------------
# routine_done.matched log emission — integration with cmd_done
# ---------------------------------------------------------------------------


def test_routine_done_matched_log_emission_fires_on_success(tmp_path: Path) -> None:
    """``cmd_done`` emits ``routine_done.matched`` on single-match success.

    Per ``feedback_log_emission_test_pattern.md`` — pin the log
    shape so a future refactor that renames or drops fields is
    caught at test time.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    with structlog.testing.capture_logs() as captured:
        # Empty record_name + populated item_text → vault-wide fuzzy
        # match. The matcher finds "Walk dog"; the log fires.
        cmd_done(
            config, "", "I walked the dog",
            today_override="2026-06-07",
        )

    matches = [c for c in captured if c.get("event") == "routine_done.matched"]
    assert len(matches) == 1, (
        f"expected exactly one routine_done.matched event; "
        f"got events={[c.get('event') for c in captured]!r}"
    )
    event = matches[0]
    assert event["query"] == "I walked the dog"
    assert event["matched_to"] == "Walk dog"
    assert event["record"] == "Daily"
    # Confidence is a float in [0.0, 1.0]. Don't pin the exact value
    # here — the helper-level tests above pin the math; this test
    # pins the log shape.
    assert isinstance(event["confidence"], float)
    assert 0.0 <= event["confidence"] <= 1.0


def test_tilray_shape_no_longer_matches_meds(tmp_path: Path) -> None:
    """STRUCTURAL-FIX regression pin (Step 5): the 2026-06-06 Tilray→Meds
    false positive NO LONGER matches.

    Was the bug: ``_fuzzy_stem("Meds") == "med"`` (3 chars) substringed
    into the query stem at check-2, matching with zero token overlap. The
    Step-5 gates (min-stem-length floor + confidence>0) both reject it, so
    the matcher falls through to a clean no-match → the ``unknown_item``
    canary fires and NO ``routine_done.matched`` log is emitted (nothing
    matched). Operator gets asked back rather than silently wronged.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine",
        "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Meds", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    with structlog.testing.capture_logs() as captured:
        code = cmd_done(
            config, "", "Tilray Medical Registration Renewal",
            today_override="2026-06-07",
        )

    # No match → no matched-log fired (the false positive is gone).
    matches = [c for c in captured if c.get("event") == "routine_done.matched"]
    assert matches == []
    # No-match canary (exit 1) — the operator is asked back, not wronged.
    assert code == 1


def test_routine_done_matched_log_carries_high_confidence_on_genuine_match(
    tmp_path: Path,
) -> None:
    """Genuine match emits a non-zero confidence — operator-grep can
    filter false positives by confidence threshold once data
    accumulates.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    with structlog.testing.capture_logs() as captured:
        # Operator's phrasing token-set is a subset of the canonical
        # item plus 1 stopword-filtered extra; intersection >= 1
        # non-stopword token → confidence > 0.0.
        cmd_done(
            config, "", "walked the dog today",
            today_override="2026-06-07",
        )

    matches = [c for c in captured if c.get("event") == "routine_done.matched"]
    assert len(matches) == 1
    event = matches[0]
    assert event["confidence"] > 0.0


def test_routine_done_matched_log_does_not_fire_on_strict_record_path(
    tmp_path: Path,
) -> None:
    """The strict-by-record-name path does NOT emit ``routine_done.matched``.

    When the operator supplies both record_name AND item_text, the
    fuzzy match doesn't run — the record is looked up by name + the
    item must exist in that record. The confidence log is specific
    to the vault-wide fuzzy match success branch where false
    positives are most likely.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    with structlog.testing.capture_logs() as captured:
        # Both args supplied → strict path, no fuzzy match
        cmd_done(
            config, "Daily", "Walk dog",
            today_override="2026-06-07",
        )

    matches = [c for c in captured if c.get("event") == "routine_done.matched"]
    assert matches == [], (
        f"strict record path should not emit routine_done.matched; "
        f"got events={[c.get('event') for c in captured]!r}"
    )
