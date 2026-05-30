"""``alfred routine`` CLI handler tests.

Covers the dispatch ratified `done` verb + supporting verbs (run-now,
status). Salem-only enforcement is pinned independently — a non-Salem
instance config raises ScopeError before any vault mutation occurs.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import pytest
import structlog
import yaml

from alfred.routine.cli import cmd_done, cmd_run_now, cmd_status
from alfred.routine.config import RoutineConfig
from alfred.vault.scope import ScopeError


# ---------------------------------------------------------------------------
# Helpers
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
# done — happy path
# ---------------------------------------------------------------------------


def test_done_appends_today_to_completion_log(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "For Self Health", {
        "type": "routine",
        "name": "For Self Health",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Reading for pleasure", "priority": "aspirational"},
            {"text": "Dog Walk", "priority": "tracked"},
        ],
        "completion_log": {
            "Reading for pleasure": ["2026-05-22", "2026-05-24"],
        },
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "For Self Health", "Reading for pleasure",
        today_override="2026-05-26",
    )
    assert code == 0

    post = frontmatter.load(str(vault / "routine" / "For Self Health.md"))
    log = post.metadata["completion_log"]
    assert log["Reading for pleasure"] == ["2026-05-22", "2026-05-24", "2026-05-26"]


def test_done_idempotent_same_day(tmp_path: Path) -> None:
    """Calling ``done`` twice with the same item on the same day yields
    one log entry (no duplicates within a single day)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code1 = cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    code2 = cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    assert code1 == code2 == 0

    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    log = post.metadata["completion_log"]
    assert log["Brush Teeth"] == ["2026-05-26"]


def test_done_creates_completion_log_when_absent(tmp_path: Path) -> None:
    """First-ever completion on a routine without a completion_log key."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Fresh", {
        "type": "routine",
        "name": "Fresh",
        "cadence": {"type": "daily"},
        "items": [{"text": "New Habit", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(config, "Fresh", "New Habit", today_override="2026-05-26")
    assert code == 0

    post = frontmatter.load(str(vault / "routine" / "Fresh.md"))
    log = post.metadata["completion_log"]
    assert log["New Habit"] == ["2026-05-26"]


def test_done_emits_log_event(tmp_path: Path) -> None:
    """Per intentionally-left-blank + log-emission-tests-must-drive-prod
    discipline: pin the emission."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    with structlog.testing.capture_logs() as captured:
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")

    matches = [c for c in captured if c.get("event") == "routine.cli.done"]
    assert len(matches) == 1
    m = matches[0]
    assert m.get("record") == "Daily"
    assert m.get("item") == "Brush Teeth"
    assert m.get("date") == "2026-05-26"
    assert m.get("appended") is True

    # Second call — appended should be False.
    with structlog.testing.capture_logs() as captured2:
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    matches2 = [c for c in captured2 if c.get("event") == "routine.cli.done"]
    assert len(matches2) == 1
    assert matches2[0].get("appended") is False


# ---------------------------------------------------------------------------
# done — error paths
# ---------------------------------------------------------------------------


def test_done_unknown_item_returns_1(tmp_path: Path, capsys) -> None:
    """Item missing from the record's items list → canary
    ``unknown_item`` (Phase 2B B1) + exit 1 + file unchanged.

    Fuzzy match on the record returns zero candidates because
    'Typo Item' shares no substring or stem with 'Real Item'."""
    import json

    from alfred.routine.cli import DONE_KIND_UNKNOWN_ITEM

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Real Item", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "Daily", "Typo Item",
        today_override="2026-05-26",
        wants_json=True,
    )
    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["kind"] == DONE_KIND_UNKNOWN_ITEM
    # File should be unchanged.
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    assert "completion_log" not in post.metadata or not post.metadata.get("completion_log")


def test_done_unknown_record_emits_canary_kind_unknown_record(
    tmp_path: Path, capsys,
) -> None:
    """Phase 2B B1 (2026-05-30) — contract change: previously
    ``cmd_done`` raised ``FileNotFoundError`` for non-existent records;
    now it emits the ``DONE_KIND_UNKNOWN_RECORD`` canary + returns exit
    1. The change exists so the talker subprocess wrapper can route on
    the canary instead of parsing exception messages.

    Pin both the exit code AND the canary kind in the JSON output."""
    import json

    from alfred.routine.cli import DONE_KIND_UNKNOWN_RECORD

    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True)
    config = _config(vault, tmp_path)
    code = cmd_done(
        config, "Nonexistent", "Anything",
        today_override="2026-05-26",
        wants_json=True,
    )
    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["kind"] == DONE_KIND_UNKNOWN_RECORD
    assert payload["ok"] is False
    assert "Nonexistent" in payload.get("error", "")


# ---------------------------------------------------------------------------
# Salem-only enforcement (CLI guard)
# ---------------------------------------------------------------------------


def test_done_non_salem_instance_raises_scope_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path, instance="hypatia")
    with pytest.raises(ScopeError):
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")


def test_done_empty_instance_raises_scope_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _config(vault, tmp_path, instance="")
    with pytest.raises(ScopeError):
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")


def test_run_now_non_salem_instance_raises(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _config(vault, tmp_path, instance="kalle")
    with pytest.raises(ScopeError):
        cmd_run_now(config, today_override="2026-05-26")


def test_status_non_salem_instance_raises(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _config(vault, tmp_path, instance="hypatia")
    with pytest.raises(ScopeError):
        cmd_status(config)


# ---------------------------------------------------------------------------
# run-now + status smoke tests
# ---------------------------------------------------------------------------


def test_run_now_writes_aggregator_note(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_run_now(config, today_override="2026-05-26")
    assert code == 0
    assert (vault / "daily" / "2026-05-26.md").exists()


def test_status_with_no_runs_prints_never(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True)
    config = _config(vault, tmp_path)

    code = cmd_status(config)
    assert code == 0
    captured = capsys.readouterr()
    # Intentionally-left-blank — visible "never" rather than silence.
    assert "Last run:" in captured.out
    assert "never" in captured.out


# ---------------------------------------------------------------------------
# Frontmatter preservation — done shouldn't reorder or drop other fields
# ---------------------------------------------------------------------------


def test_done_preserves_other_frontmatter_fields(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    payload = {
        "type": "routine",
        "status": "active",
        "name": "Daily",
        "created": "2026-05-01",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
        "tags": ["habits", "morning"],
    }
    _write_routine(vault, "Daily", payload)
    config = _config(vault, tmp_path)

    cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    fm = post.metadata
    assert fm["type"] == "routine"
    assert fm["status"] == "active"
    assert fm["name"] == "Daily"
    assert fm["tags"] == ["habits", "morning"]
    # And completion_log got the new entry.
    assert fm["completion_log"]["Brush Teeth"] == ["2026-05-26"]


# ===========================================================================
# Phase 2B B1 (2026-05-30) — Conversational completion CLI surface
# ===========================================================================
#
# Test surface per dispatch:
#   * --completed-at YYYY-MM-DD back-dating works (yesterday / 3d ago)
#   * --completed-at in the future → rejected with future_date_rejected
#   * canary `kind` discriminator in JSON output for every path:
#     - success / unknown_record / unknown_item / ambiguous_item /
#       idempotent_noop / future_date_rejected
#   * vault-wide fuzzy match: omit record_name + pass item only
#   * stem-tolerant fuzzy ("walking" matches "Walk dog")
#   * ambiguous fuzzy → returns candidate list, exit 1, kind=ambiguous
#   * idempotent on different dates works (back-date + today both in
#     the same completion_log without dup)


def test_done_completed_at_back_date_yesterday(tmp_path: Path) -> None:
    """`--completed-at YYYY-MM-DD` for yesterday → completion logged
    with the back-dated value, NOT today."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    # today_override = 2026-05-30; completed_at = yesterday (05-29).
    code = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        completed_at="2026-05-29",
    )
    assert code == 0

    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    log = post.metadata["completion_log"]
    assert log["Walk dog"] == ["2026-05-29"]  # back-date, NOT today


def test_done_completed_at_future_rejected_with_canary(
    tmp_path: Path, capsys,
) -> None:
    """`--completed-at` in the future → exit 1 + canary
    ``future_date_rejected``. File unchanged."""
    import json

    from alfred.routine.cli import DONE_KIND_FUTURE_DATE_REJECTED

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        completed_at="2027-01-01",  # future
        wants_json=True,
    )
    assert code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["kind"] == DONE_KIND_FUTURE_DATE_REJECTED
    assert payload["ok"] is False
    # File must be unchanged.
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    assert not post.metadata.get("completion_log")


def test_done_completed_at_malformed_rejected(tmp_path: Path, capsys) -> None:
    """`--completed-at` with malformed date string → exit 1 + canary.

    The canary kind is also future_date_rejected (the validator returns
    the same kind for any non-acceptable input — malformed and future
    both belong to the 'invalid date' class). The error message names
    the input so the operator can correct it."""
    import json

    from alfred.routine.cli import DONE_KIND_FUTURE_DATE_REJECTED

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        completed_at="not-a-date",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == DONE_KIND_FUTURE_DATE_REJECTED


def test_done_completed_at_today_is_accepted(tmp_path: Path) -> None:
    """`--completed-at YYYY-MM-DD` where the date equals today is
    accepted (upper bound inclusive)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        completed_at="2026-05-30",
    )
    assert code == 0


def test_done_vault_wide_fuzzy_no_record_supplied(tmp_path: Path) -> None:
    """When record_name is empty, the CLI does a vault-wide fuzzy match
    on the item text. Phase 2B B1 contract."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine",
        "status": "active",
        "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "aspirational"}],
    })
    _write_routine(vault, "Other", {
        "type": "routine",
        "status": "active",
        "name": "Other",
        "cadence": {"type": "daily"},
        "items": [{"text": "Different Item", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    # Empty record_name → vault-wide fuzzy. "walked the dog" stems to
    # "walk dog" → matches the canonical "Walk dog".
    code = cmd_done(
        config, "", "walked the dog",
        today_override="2026-05-30",
    )
    assert code == 0

    post = frontmatter.load(str(vault / "routine" / "Self Care.md"))
    log = post.metadata["completion_log"]
    # The verbatim canonical item text was used as the key.
    assert "Walk dog" in log
    assert log["Walk dog"] == ["2026-05-30"]


def test_done_vault_wide_fuzzy_ambiguous_returns_candidate_list(
    tmp_path: Path, capsys,
) -> None:
    """When vault-wide fuzzy returns 2+ matches → canary
    ``ambiguous_item`` + exit 1 + JSON carries the candidate list."""
    import json

    from alfred.routine.cli import DONE_KIND_AMBIGUOUS_ITEM

    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine",
        "status": "active",
        "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "aspirational"}],
    })
    _write_routine(vault, "Outdoor", {
        "type": "routine",
        "status": "active",
        "name": "Outdoor",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk to coffee shop", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    # "walked" matches both "Walk dog" and "Walk to coffee shop" via
    # substring stem match.
    code = cmd_done(
        config, "", "walked",
        today_override="2026-05-30",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == DONE_KIND_AMBIGUOUS_ITEM
    # Candidate list carries both matches with record + item.
    candidates = payload.get("candidates", [])
    assert len(candidates) == 2
    by_item = {c["item"]: c["record"] for c in candidates}
    assert by_item.get("Walk dog") == "Self Care"
    assert by_item.get("Walk to coffee shop") == "Outdoor"


def test_done_vault_wide_fuzzy_zero_match_returns_unknown_item(
    tmp_path: Path, capsys,
) -> None:
    """Vault-wide fuzzy with zero matches → canary ``unknown_item``
    + the available_items list so the operator can see what WAS
    available."""
    import json

    from alfred.routine.cli import DONE_KIND_UNKNOWN_ITEM

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "status": "active",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "", "xyzzy nonexistent",
        today_override="2026-05-30",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == DONE_KIND_UNKNOWN_ITEM
    # available_items surface for operator visibility.
    available = payload.get("available_items", [])
    assert len(available) == 1
    assert available[0]["item"] == "Brush Teeth"
    assert available[0]["record"] == "Daily"


def test_done_canary_success_kind_in_json_output(
    tmp_path: Path, capsys,
) -> None:
    """Happy path emits canary kind=success + the data payload."""
    import json

    from alfred.routine.cli import DONE_KIND_SUCCESS

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        wants_json=True,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == DONE_KIND_SUCCESS
    assert payload["ok"] is True
    assert payload["record"] == "Daily"
    assert payload["item"] == "Walk dog"
    assert payload["date"] == "2026-05-30"
    assert payload["appended"] is True


def test_done_canary_idempotent_noop_kind_on_double_call(
    tmp_path: Path, capsys,
) -> None:
    """Re-invoking with the same item + date → kind=idempotent_noop
    + exit 0 (it's not an error; it's the expected idempotent shape).
    File written ONCE."""
    import json

    from alfred.routine.cli import (
        DONE_KIND_IDEMPOTENT_NOOP,
        DONE_KIND_SUCCESS,
    )

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    # First call → success.
    code1 = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        wants_json=True,
    )
    assert code1 == 0
    payload1 = json.loads(capsys.readouterr().out)
    assert payload1["kind"] == DONE_KIND_SUCCESS

    # Second call (same item + date) → idempotent_noop, still exit 0.
    code2 = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        wants_json=True,
    )
    assert code2 == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["kind"] == DONE_KIND_IDEMPOTENT_NOOP
    assert payload2["ok"] is True

    # File has ONE entry only.
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    assert post.metadata["completion_log"]["Walk dog"] == ["2026-05-30"]


def test_done_idempotent_different_dates_both_logged(
    tmp_path: Path,
) -> None:
    """Back-date + today → BOTH entries land in completion_log (no
    spurious de-dup across distinct dates)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    # Back-date yesterday.
    code1 = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
        completed_at="2026-05-29",
    )
    # Today.
    code2 = cmd_done(
        config, "Daily", "Walk dog",
        today_override="2026-05-30",
    )
    assert code1 == code2 == 0

    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    log = post.metadata["completion_log"]
    assert sorted(log["Walk dog"]) == ["2026-05-29", "2026-05-30"]


def test_done_on_record_fuzzy_fallback_after_strict_miss(
    tmp_path: Path,
) -> None:
    """When the operator supplies record_name + an item text that
    doesn't strictly match but DOES fuzzy-match an item on that
    record, the fuzzy fallback canonicalises to the verbatim text."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    # "walking the dog" → stem-normalised "walk dog" → matches "Walk dog".
    code = cmd_done(
        config, "Daily", "walking the dog",
        today_override="2026-05-30",
    )
    assert code == 0
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    log = post.metadata["completion_log"]
    # Canonical text used as key, NOT the fuzzy input.
    assert log["Walk dog"] == ["2026-05-30"]
    assert "walking the dog" not in log


def test_done_fuzzy_archived_routines_excluded(tmp_path: Path) -> None:
    """Items on archived routines do NOT participate in the vault-wide
    fuzzy match (mirror of the auto-T1/T2/T3 compute paths)."""
    import json

    from alfred.routine.cli import DONE_KIND_UNKNOWN_ITEM

    vault = tmp_path / "vault"
    _write_routine(vault, "Old", {
        "type": "routine",
        "status": "archived",
        "name": "Old",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "aspirational"}],
    })
    config = _config(vault, tmp_path)

    # Empty record_name + item that only matches the archived routine
    # → no match → unknown_item.
    code = cmd_done(
        config, "", "Walk dog",
        today_override="2026-05-30",
        wants_json=True,
    )
    assert code == 1


# ---------------------------------------------------------------------------
# Fuzzy match unit tests (helpers exposed for direct testing)
# ---------------------------------------------------------------------------


def test_fuzzy_stem_strips_present_participle() -> None:
    from alfred.routine.cli import _fuzzy_stem
    assert _fuzzy_stem("walking") == "walk"
    # Every word is stemmed independently.
    assert _fuzzy_stem("Walking the dog") == "walk the dog"
    assert _fuzzy_stem("dog walking") == "dog walk"


def test_fuzzy_stem_strips_past_tense() -> None:
    from alfred.routine.cli import _fuzzy_stem
    assert _fuzzy_stem("exercised") == "exercise"
    # The trailing 'ed' on a too-short word is left alone (heuristic:
    # require length > suffix_len + 1 to avoid over-stemming).
    assert _fuzzy_stem("red") == "red"


def test_fuzzy_stem_strips_trailing_s() -> None:
    from alfred.routine.cli import _fuzzy_stem
    assert _fuzzy_stem("walks") == "walk"


def test_fuzzy_stem_stems_every_word() -> None:
    """Every word independently stemmed — operator phrasing
    'I walked the dog' should reduce to a form whose content words
    overlap with 'Walk dog'."""
    from alfred.routine.cli import _fuzzy_stem
    # "walked" → "walk"; "the" stays; "dog" stays.
    assert _fuzzy_stem("I walked the dog") == "i walk the dog"


def test_matches_item_substring_case_insensitive() -> None:
    from alfred.routine.cli import _matches_item
    assert _matches_item("walk", "Walk dog")
    assert _matches_item("WALK", "Walk dog")
    assert _matches_item("dog", "Walk dog")


def test_matches_item_stem_tolerant() -> None:
    """'walking' → stem 'walk' → substring of 'walk dog' → match."""
    from alfred.routine.cli import _matches_item
    assert _matches_item("walking", "Walk dog")


def test_matches_item_token_set_with_stop_words() -> None:
    """'I walked the dog' → tokens {walk, dog} (after stop-word
    filter) ⊆ {walk, dog} from 'Walk dog' → match. This is the
    operator-phrasing-with-articles case the SKILL grammar relies on."""
    from alfred.routine.cli import _matches_item
    assert _matches_item("I walked the dog", "Walk dog")
    assert _matches_item("I exercised", "Exercise")
    assert _matches_item("I did my exercise", "Exercise")


def test_matches_item_token_set_rejects_partial_overlap() -> None:
    """Partial overlap (shared word but neither set ⊆ other) → no
    match. 'I walked the cat' shares 'walk' with 'Walk dog' but
    {walk, cat} ⊄ {walk, dog} and vice versa → no match. Avoids
    false positives."""
    from alfred.routine.cli import _matches_item
    assert not _matches_item("I walked the cat", "Walk dog")


def test_matches_item_no_match_returns_false() -> None:
    from alfred.routine.cli import _matches_item
    assert not _matches_item("xyzzy", "Walk dog")
    assert not _matches_item("meditation", "Walk dog")


def test_matches_item_empty_inputs_return_false() -> None:
    from alfred.routine.cli import _matches_item
    assert not _matches_item("", "Walk dog")
    assert not _matches_item("walk", "")
