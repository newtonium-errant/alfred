"""``alfred routine item`` CLI handler tests (Phase 2B B3, 2026-05-30).

Covers ``cmd_item_add`` / ``cmd_item_remove`` / ``cmd_item_edit`` —
the three new subverbs for item-level CRUD on existing routine
records.

Test surface:
  * Happy-path per action (add / remove / edit)
  * Atomic-mutation contract: other items on the record unchanged
  * completion_log migration on rename (Edit text NEW)
  * completion_log strip on remove
  * Cadence-conflict rejection (no --clear-X flag) + acceptance
    (with --clear-X flag)
  * Fuzzy match (vault-wide + record-scoped)
  * Disambiguation canary on 2+ matches
  * Duplicate-item on add
  * Invalid-field-value validation (negative cadence, bad priority,
    malformed due_pattern JSON)
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import pytest
import yaml

from alfred.routine.cli import (
    ITEM_KIND_ADDED,
    ITEM_KIND_AMBIGUOUS_ITEM,
    ITEM_KIND_CADENCE_CONFLICT,
    ITEM_KIND_DUPLICATE_ITEM,
    ITEM_KIND_EDITED,
    ITEM_KIND_INVALID_FIELD,
    ITEM_KIND_REMOVED,
    ITEM_KIND_UNKNOWN_ITEM,
    ITEM_KIND_UNKNOWN_RECORD,
    cmd_item_add,
    cmd_item_edit,
    cmd_item_remove,
)
from alfred.routine.config import RoutineConfig
from alfred.vault.scope import ScopeError


# ---------------------------------------------------------------------------
# Helpers (mirror test_cli.py)
# ---------------------------------------------------------------------------


def _config(
    vault_path: Path, tmp_path: Path, *, instance: str = "salem",
) -> RoutineConfig:
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


def _read_fm(vault_path: Path, name: str) -> dict:
    post = frontmatter.load(str(vault_path / "routine" / f"{name}.md"))
    return dict(post.metadata)


# ===========================================================================
# cmd_item_add
# ===========================================================================


def test_item_add_happy_path_appends_to_items_list(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config,
        record_name="Daily",
        item_text="New Item",
        priority="aspirational",
    )
    assert code == 0

    fm = _read_fm(vault, "Daily")
    items = fm["items"]
    assert len(items) == 2
    # Original item unchanged.
    assert items[0]["text"] == "Brush Teeth"
    assert items[0]["priority"] == "tracked"
    # New item appended at end with operator-supplied priority.
    assert items[1]["text"] == "New Item"
    assert items[1]["priority"] == "aspirational"


def test_item_add_defaults_priority_to_tracked(tmp_path: Path) -> None:
    """When operator omits --priority, default to 'tracked' (matches
    the aggregator's existing fallback for raw_items)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(config, record_name="Daily", item_text="X")
    assert code == 0
    assert _read_fm(vault, "Daily")["items"][0]["priority"] == "tracked"


def test_item_add_with_soft_cadence(tmp_path: Path) -> None:
    """target_cadence_days threads through to the new item."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config,
        record_name="Self Care",
        item_text="Walk dog",
        priority="aspirational",
        target_cadence_days=3,
    )
    assert code == 0
    item = _read_fm(vault, "Self Care")["items"][0]
    assert item["target_cadence_days"] == 3
    # Hard cadence fields NOT present (only target_cadence_days).
    assert "due_pattern" not in item
    assert "escalate_at_days" not in item


def test_item_add_with_hard_cadence_due_pattern_dict(tmp_path: Path) -> None:
    """due_pattern accepts a dict directly (talker passes parsed
    dicts; CLI accepts JSON-string-or-dict)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Bills", {
        "type": "routine", "name": "Bills",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config,
        record_name="Bills",
        item_text="Pay rent",
        priority="critical",
        due_pattern={"type": "monthly", "day": 1},
        escalate_at_days=0,
        surface_at_days=5,
    )
    assert code == 0
    item = _read_fm(vault, "Bills")["items"][0]
    assert item["due_pattern"]["type"] == "monthly"
    assert item["due_pattern"]["day"] == 1
    assert item["escalate_at_days"] == 0
    assert item["surface_at_days"] == 5


def test_item_add_with_hard_cadence_due_pattern_json_string(
    tmp_path: Path,
) -> None:
    """CLI also accepts due_pattern as JSON string (operator typed
    --due-pattern '{"type":"weekly","day":"thu"}' at the shell)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Weekly", {
        "type": "routine", "name": "Weekly",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config,
        record_name="Weekly",
        item_text="Garbage",
        priority="critical",
        due_pattern='{"type": "weekly", "day": "thu"}',
    )
    assert code == 0
    item = _read_fm(vault, "Weekly")["items"][0]
    assert item["due_pattern"]["type"] == "weekly"
    assert item["due_pattern"]["day"] == "thu"


def test_item_add_does_not_touch_completion_log(tmp_path: Path) -> None:
    """New item has no history; completion_log dict is preserved
    (other items' entries untouched, new item NOT added to the dict
    since it has no completions yet)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "completion_log": {
            "Brush Teeth": ["2026-05-28", "2026-05-29"],
        },
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(config, record_name="Daily", item_text="New Item")
    assert code == 0

    log = _read_fm(vault, "Daily")["completion_log"]
    # Existing entry untouched.
    assert log["Brush Teeth"] == ["2026-05-28", "2026-05-29"]
    # New item NOT in log (no completions yet).
    assert "New Item" not in log


def test_item_add_duplicate_text_returns_canary(
    tmp_path: Path, capsys,
) -> None:
    """Adding an item with text matching an existing one → reject
    with ITEM_KIND_DUPLICATE_ITEM. File unchanged."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="Walk dog",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_DUPLICATE_ITEM
    # File still has just the one item.
    assert len(_read_fm(vault, "Daily")["items"]) == 1


def test_atomic_item_mutate_refusal_does_not_touch_file(
    tmp_path: Path, capsys,
) -> None:
    """Regression pin (WARN-1, code-reviewer 2026-05-30) — refusal
    paths through ``_atomic_item_mutate`` MUST NOT touch the file
    on disk. Pre-fix, the primitive unconditionally called
    ``_write_record_state``, which round-trips the YAML — even
    identical content bumps mtime + can drift YAML formatting
    (number normalisation, multiline-style flattening, list-of-
    dicts reflow). The ``aborted=True`` signal on ``_MutationResult``
    now gates the write; this test pins the no-write invariant for
    all three refusal sites.

    Covers all three refusal paths:
      1. ``cmd_item_add`` duplicate-item: text matches existing →
         ``aborted=True`` → file untouched.
      2. ``cmd_item_edit`` cadence-conflict: existing item has
         due_pattern, edit sets target_cadence_days without
         --clear-due-pattern → ``aborted=True`` → file untouched.
      3. ``cmd_item_edit`` TOCTOU-disappeared: hard to provoke from
         a test (mutator sees the item disappear between load +
         iteration), exercised by hand-deleting the items list
         between load + mutate. Tested via the _atomic_item_mutate
         primitive directly with a forced aborted return.

    Pinning bytes (not just mtime) is the load-bearing invariant
    — mtime alone could theoretically pass if write+read happen
    within the same OS time granule. Byte-identity is the
    semantic operator promise: "refused → file unchanged."
    """
    import os
    import time

    from alfred.routine.cli_items import (
        _MutationResult,
        _atomic_item_mutate,
    )

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)
    record_path = vault / "routine" / "Daily.md"

    # Capture baseline bytes + mtime BEFORE any refused call.
    bytes_before = record_path.read_bytes()
    mtime_before = record_path.stat().st_mtime_ns

    # Small sleep so any erroneous write would produce a
    # detectably-different mtime (avoids the same-OS-tick collision
    # that could mask the bug on a fast filesystem).
    time.sleep(0.01)

    # --- Refusal path 1: duplicate-item on add --------------------
    code = cmd_item_add(
        config, record_name="Daily", item_text="Walk dog",
        wants_json=True,
    )
    capsys.readouterr()  # drain canary output
    assert code == 1, "duplicate-item path must exit 1"
    assert record_path.read_bytes() == bytes_before, (
        "duplicate-item refusal MUST NOT touch the file bytes "
        "(YAML round-trip drift breaks the operator-promise "
        '"refused → file unchanged")'
    )
    assert record_path.stat().st_mtime_ns == mtime_before, (
        "duplicate-item refusal MUST NOT bump mtime"
    )

    # --- Refusal path 2: cadence-conflict on edit -----------------
    # Re-seed the record with an item carrying due_pattern so the
    # cadence-conflict path fires.
    _write_routine(vault, "Bills", {
        "type": "routine", "name": "Bills",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Pay rent", "priority": "critical",
            "due_pattern": {"type": "monthly", "day": 1},
            "escalate_at_days": 0,
        }],
    })
    bills_path = vault / "routine" / "Bills.md"
    bills_bytes_before = bills_path.read_bytes()
    bills_mtime_before = bills_path.stat().st_mtime_ns
    time.sleep(0.01)

    code = cmd_item_edit(
        config, record_name="Bills", item_text="Pay rent",
        target_cadence_days=30,
        # --clear-due-pattern intentionally NOT supplied → conflict.
        wants_json=True,
    )
    capsys.readouterr()  # drain canary
    assert code == 1, "cadence-conflict path must exit 1"
    assert bills_path.read_bytes() == bills_bytes_before, (
        "cadence-conflict refusal MUST NOT touch the file bytes"
    )
    assert bills_path.stat().st_mtime_ns == bills_mtime_before, (
        "cadence-conflict refusal MUST NOT bump mtime"
    )

    # --- Refusal path 3: forced aborted=True via the primitive ----
    # Direct exercise of _atomic_item_mutate's gate. A mutator that
    # returns aborted=True must NOT trigger a write. This covers
    # the TOCTOU-disappeared refusal path in cmd_item_edit without
    # needing to provoke the race condition itself.
    _write_routine(vault, "Test", {
        "type": "routine", "name": "Test",
        "cadence": {"type": "daily"},
        "items": [{"text": "X", "priority": "tracked"}],
    })
    test_path = vault / "routine" / "Test.md"
    test_bytes_before = test_path.read_bytes()
    test_mtime_before = test_path.stat().st_mtime_ns
    time.sleep(0.01)

    def _refuse_mutator(items, completion_log):
        # Return ANY state with aborted=True — primitive must skip
        # the write regardless of what items/completion_log contain.
        return _MutationResult(
            items=[{"text": "totally different", "priority": "critical"}],
            completion_log={"different": ["2026-05-30"]},
            payload_extras={},
            aborted=True,
        )

    result = _atomic_item_mutate(test_path, _refuse_mutator)
    assert result.aborted is True
    assert test_path.read_bytes() == test_bytes_before, (
        "aborted=True MUST NOT touch the file bytes even when the "
        "mutator returns wildly different items/completion_log"
    )
    assert test_path.stat().st_mtime_ns == test_mtime_before, (
        "aborted=True MUST NOT bump mtime"
    )


def test_atomic_item_mutate_success_does_touch_file(
    tmp_path: Path,
) -> None:
    """Companion regression pin to the refusal test above: success
    path (aborted=False, the default) MUST write the file.

    Sanity check that the gate isn't accidentally suppressing
    legitimate writes — the bytes+mtime invariant only applies to
    refusals."""
    import time

    from alfred.routine.cli_items import (
        _MutationResult,
        _atomic_item_mutate,
    )

    vault = tmp_path / "vault"
    _write_routine(vault, "Test", {
        "type": "routine", "name": "Test",
        "cadence": {"type": "daily"},
        "items": [{"text": "X", "priority": "tracked"}],
    })
    test_path = vault / "routine" / "Test.md"
    bytes_before = test_path.read_bytes()
    mtime_before = test_path.stat().st_mtime_ns
    time.sleep(0.01)

    def _success_mutator(items, completion_log):
        # Mutate items (append a new one) + return without aborted
        # flag. Primitive MUST write.
        items.append({"text": "Y", "priority": "tracked"})
        return _MutationResult(
            items=items,
            completion_log=completion_log,
            payload_extras={},
        )

    result = _atomic_item_mutate(test_path, _success_mutator)
    assert result.aborted is False  # default
    # File DID change.
    assert test_path.read_bytes() != bytes_before, (
        "Success path (aborted=False) MUST write the file"
    )
    assert test_path.stat().st_mtime_ns > mtime_before, (
        "Success path MUST bump mtime"
    )
    # New item landed on disk.
    fm = _read_fm(vault, "Test")
    assert len(fm["items"]) == 2
    assert fm["items"][1]["text"] == "Y"


def test_item_add_unknown_record_returns_canary(
    tmp_path: Path, capsys,
) -> None:
    """Add against non-existent record → ITEM_KIND_UNKNOWN_RECORD."""
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True)
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Nonexistent", item_text="X",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_UNKNOWN_RECORD


def test_item_add_empty_record_returns_canary(
    tmp_path: Path, capsys,
) -> None:
    """Add with empty record_name → ITEM_KIND_UNKNOWN_RECORD (vault-
    wide fuzzy doesn't apply for add; SKILL asks back)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="", item_text="X",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_UNKNOWN_RECORD


def test_item_add_empty_item_text_returns_invalid_field(
    tmp_path: Path, capsys,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="   ",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_INVALID_FIELD


def test_item_add_invalid_priority_returns_invalid_field(
    tmp_path: Path, capsys,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="X",
        priority="urgent",  # not in {critical, tracked, aspirational}
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_INVALID_FIELD
    assert "priority" in payload["error"].lower()


def test_item_add_negative_cadence_returns_invalid_field(
    tmp_path: Path, capsys,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="X",
        target_cadence_days=-3,
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_INVALID_FIELD


def test_item_add_malformed_due_pattern_json_returns_invalid_field(
    tmp_path: Path, capsys,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="X",
        due_pattern="not-valid-json{{",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_INVALID_FIELD
    assert "due_pattern" in payload["error"].lower()


def test_item_add_unknown_due_pattern_type_returns_invalid_field(
    tmp_path: Path, capsys,
) -> None:
    """due_pattern.type not in DUE_PATTERN_TYPES → INVALID_FIELD."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="X",
        due_pattern={"type": "annually"},  # not a real type
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_INVALID_FIELD


def test_item_add_both_cadence_modes_returns_cadence_conflict(
    tmp_path: Path, capsys,
) -> None:
    """Operator-supplied target_cadence_days AND due_pattern in same
    call → ITEM_KIND_CADENCE_CONFLICT (mutually exclusive)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="X",
        target_cadence_days=3,
        due_pattern={"type": "weekly", "day": "thu"},
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_CADENCE_CONFLICT


def test_item_add_atomic_other_items_unchanged(tmp_path: Path) -> None:
    """Adding doesn't perturb other items' frontmatter — exact-byte
    pin would be brittle (YAML serialisation may reorder keys
    within an item dict), but the items list count + per-item text/
    priority should be preserved."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Multi", {
        "type": "routine", "name": "Multi",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "A", "priority": "critical"},
            {"text": "B", "priority": "tracked",
             "target_cadence_days": 7},
            {"text": "C", "priority": "aspirational"},
        ],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Multi", item_text="D",
        priority="tracked",
    )
    assert code == 0

    items = _read_fm(vault, "Multi")["items"]
    assert len(items) == 4
    # A B C unchanged in same order.
    assert [it["text"] for it in items] == ["A", "B", "C", "D"]
    assert items[0]["priority"] == "critical"
    assert items[1]["target_cadence_days"] == 7
    assert items[2]["priority"] == "aspirational"


def test_item_add_canary_success_kind_in_json_output(
    tmp_path: Path, capsys,
) -> None:
    """JSON mode emits ITEM_KIND_ADDED + payload (single-line per
    feedback_cli_json_mode_single_line)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Daily", item_text="X",
        wants_json=True,
    )
    assert code == 0
    out = capsys.readouterr().out
    # Single-line JSON (no embedded newlines in the payload object).
    assert "\n" not in out.strip()
    payload = json.loads(out)
    assert payload["kind"] == ITEM_KIND_ADDED
    assert payload["ok"] is True
    assert payload["record"] == "Daily"
    assert payload["item"] == "X"


# ===========================================================================
# cmd_item_remove
# ===========================================================================


def test_item_remove_happy_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "A", "priority": "tracked"},
            {"text": "B", "priority": "tracked"},
        ],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_remove(config, record_name="Daily", item_text="A")
    assert code == 0
    items = _read_fm(vault, "Daily")["items"]
    assert len(items) == 1
    assert items[0]["text"] == "B"


def test_item_remove_strips_completion_log(tmp_path: Path) -> None:
    """Remove also strips ``completion_log[<item_text>]`` atomically."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "completion_log": {
            "Walk dog": ["2026-05-28", "2026-05-29"],
            "Brush teeth": ["2026-05-29"],
        },
        "items": [
            {"text": "Walk dog", "priority": "tracked"},
            {"text": "Brush teeth", "priority": "tracked"},
        ],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_remove(
        config, record_name="Daily", item_text="Walk dog",
    )
    assert code == 0

    log = _read_fm(vault, "Daily")["completion_log"]
    assert "Walk dog" not in log
    # Other item's log entry preserved.
    assert log["Brush teeth"] == ["2026-05-29"]


def test_item_remove_no_completion_log_entry_still_succeeds(
    tmp_path: Path,
) -> None:
    """Item without prior completion_log entry can still be removed
    (no-op on completion_log side)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Walk dog", "priority": "tracked"},
        ],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_remove(
        config, record_name="Daily", item_text="Walk dog",
    )
    assert code == 0
    assert _read_fm(vault, "Daily")["items"] == []


def test_item_remove_vault_wide_fuzzy(tmp_path: Path) -> None:
    """Empty record_name → vault-wide fuzzy match. 'walking' stems
    to 'walk' → matches 'Walk dog' on Self Care."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "status": "active", "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "aspirational"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_remove(
        config, record_name="", item_text="walking",
    )
    assert code == 0
    assert _read_fm(vault, "Self Care")["items"] == []


def test_item_remove_unknown_item_returns_canary(
    tmp_path: Path, capsys,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Real Item", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_remove(
        config, record_name="Daily", item_text="xyzzy nonexistent",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_UNKNOWN_ITEM


def test_item_remove_ambiguous_vault_wide(
    tmp_path: Path, capsys,
) -> None:
    """Vault-wide fuzzy returning 2+ matches → AMBIGUOUS_ITEM."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "status": "active", "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "aspirational"}],
    })
    _write_routine(vault, "Outdoor", {
        "type": "routine", "status": "active", "name": "Outdoor",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk to coffee shop", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_remove(
        config, record_name="", item_text="walked",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_AMBIGUOUS_ITEM
    assert len(payload["candidates"]) == 2


def test_item_remove_atomic_other_items_unchanged(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Multi", {
        "type": "routine", "name": "Multi",
        "cadence": {"type": "daily"},
        "completion_log": {
            "A": ["2026-05-28"], "B": ["2026-05-28"], "C": ["2026-05-28"],
        },
        "items": [
            {"text": "A", "priority": "critical"},
            {"text": "B", "priority": "tracked"},
            {"text": "C", "priority": "aspirational"},
        ],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_remove(config, record_name="Multi", item_text="B")
    assert code == 0

    fm = _read_fm(vault, "Multi")
    assert [it["text"] for it in fm["items"]] == ["A", "C"]
    # A and C completion_log entries preserved.
    assert fm["completion_log"] == {
        "A": ["2026-05-28"], "C": ["2026-05-28"],
    }


# ===========================================================================
# cmd_item_edit
# ===========================================================================


def test_item_edit_priority_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Daily", item_text="Walk dog",
        priority="critical",
    )
    assert code == 0
    assert _read_fm(vault, "Daily")["items"][0]["priority"] == "critical"


def test_item_edit_soft_to_soft_cadence_change(tmp_path: Path) -> None:
    """target_cadence_days 3 → 2 (same mode, no conflict)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Walk dog", "priority": "aspirational",
            "target_cadence_days": 3,
        }],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Self Care", item_text="Walk dog",
        target_cadence_days=2,
    )
    assert code == 0
    assert _read_fm(vault, "Self Care")["items"][0][
        "target_cadence_days"
    ] == 2


def test_item_edit_soft_to_hard_without_clear_flag_returns_conflict(
    tmp_path: Path, capsys,
) -> None:
    """Setting due_pattern on an item with existing target_cadence_days
    WITHOUT --clear-target-cadence-days → CADENCE_CONFLICT."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Walk dog", "priority": "aspirational",
            "target_cadence_days": 3,
        }],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Self Care", item_text="Walk dog",
        due_pattern={"type": "weekly", "day": "thu"},
        # clear_target_cadence_days NOT supplied.
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_CADENCE_CONFLICT
    # File unchanged.
    item = _read_fm(vault, "Self Care")["items"][0]
    assert item["target_cadence_days"] == 3
    assert "due_pattern" not in item


def test_item_edit_soft_to_hard_with_clear_flag_succeeds(
    tmp_path: Path,
) -> None:
    """With --clear-target-cadence-days, the swap completes:
    target_cadence_days stripped + due_pattern set."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "name": "Self Care",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Walk dog", "priority": "aspirational",
            "target_cadence_days": 3,
        }],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Self Care", item_text="Walk dog",
        due_pattern={"type": "weekly", "day": "thu"},
        clear_target_cadence_days=True,
    )
    assert code == 0
    item = _read_fm(vault, "Self Care")["items"][0]
    assert "target_cadence_days" not in item
    assert item["due_pattern"]["type"] == "weekly"
    assert item["due_pattern"]["day"] == "thu"


def test_item_edit_hard_to_soft_without_clear_flag_returns_conflict(
    tmp_path: Path, capsys,
) -> None:
    """Reverse direction: setting target_cadence_days on an item with
    existing due_pattern WITHOUT --clear-due-pattern → conflict."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Bills", {
        "type": "routine", "name": "Bills",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Pay rent", "priority": "critical",
            "due_pattern": {"type": "monthly", "day": 1},
            "escalate_at_days": 0,
        }],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Bills", item_text="Pay rent",
        target_cadence_days=30,
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_CADENCE_CONFLICT


def test_item_edit_hard_to_soft_with_clear_flag_strips_escalation_too(
    tmp_path: Path,
) -> None:
    """clear_due_pattern strips due_pattern + escalate_at_days +
    surface_at_days (the latter two only make sense with due_pattern;
    leaving them orphaned would be operator confusion)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Bills", {
        "type": "routine", "name": "Bills",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Pay rent", "priority": "critical",
            "due_pattern": {"type": "monthly", "day": 1},
            "escalate_at_days": 0,
            "surface_at_days": 5,
        }],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Bills", item_text="Pay rent",
        target_cadence_days=30,
        clear_due_pattern=True,
    )
    assert code == 0
    item = _read_fm(vault, "Bills")["items"][0]
    assert item["target_cadence_days"] == 30
    # All three hard-cadence companions cleared.
    assert "due_pattern" not in item
    assert "escalate_at_days" not in item
    assert "surface_at_days" not in item


def test_item_edit_both_cadence_modes_in_same_call_rejects(
    tmp_path: Path, capsys,
) -> None:
    """Even with both clear flags, supplying BOTH new
    target_cadence_days AND new due_pattern in the same call rejects
    (mutually exclusive even when clearing)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "X", {
        "type": "routine", "name": "X",
        "cadence": {"type": "daily"},
        "items": [{"text": "Item", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="X", item_text="Item",
        target_cadence_days=3,
        due_pattern={"type": "weekly", "day": "thu"},
        clear_due_pattern=True,
        clear_target_cadence_days=True,
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_CADENCE_CONFLICT


def test_item_edit_text_migrates_completion_log(tmp_path: Path) -> None:
    """Renaming via --text NEW migrates
    completion_log[old_text] → completion_log[new_text] atomically.
    Historical completion data MUST be preserved under the new key."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "completion_log": {
            "Walk dog": ["2026-05-28", "2026-05-29", "2026-05-30"],
            "Brush teeth": ["2026-05-29"],
        },
        "items": [
            {"text": "Walk dog", "priority": "tracked"},
            {"text": "Brush teeth", "priority": "tracked"},
        ],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Daily", item_text="Walk dog",
        new_text="dog walk",
    )
    assert code == 0

    fm = _read_fm(vault, "Daily")
    # Item text renamed in items list.
    walk_items = [it for it in fm["items"] if it["text"] == "dog walk"]
    assert len(walk_items) == 1
    # No item with the OLD text.
    assert not any(it["text"] == "Walk dog" for it in fm["items"])

    # Completion log migrated.
    log = fm["completion_log"]
    assert "Walk dog" not in log
    assert log["dog walk"] == ["2026-05-28", "2026-05-29", "2026-05-30"]
    # Other item's log preserved.
    assert log["Brush teeth"] == ["2026-05-29"]


def test_item_edit_text_no_completion_log_entry_still_renames(
    tmp_path: Path,
) -> None:
    """Item without prior completion_log entry can still be renamed
    (no-op on completion_log; items list still updates)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Old name", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Daily", item_text="Old name",
        new_text="New name",
    )
    assert code == 0
    items = _read_fm(vault, "Daily")["items"]
    assert items[0]["text"] == "New name"


def test_item_edit_text_same_as_existing_no_op(tmp_path: Path) -> None:
    """Renaming to the SAME text is a no-op (no completion_log
    migration, no items change). Mirror of B1's idempotent shape."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "completion_log": {"Walk dog": ["2026-05-28"]},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Daily", item_text="Walk dog",
        new_text="Walk dog",
    )
    assert code == 0
    # Completion log unchanged (no migration on no-op rename).
    log = _read_fm(vault, "Daily")["completion_log"]
    assert log == {"Walk dog": ["2026-05-28"]}


def test_item_edit_atomic_other_items_unchanged(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Multi", {
        "type": "routine", "name": "Multi",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "A", "priority": "critical"},
            {"text": "B", "priority": "tracked",
             "target_cadence_days": 7},
            {"text": "C", "priority": "aspirational"},
        ],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Multi", item_text="B",
        priority="critical",
    )
    assert code == 0

    items = _read_fm(vault, "Multi")["items"]
    # A unchanged.
    assert items[0]["text"] == "A" and items[0]["priority"] == "critical"
    # B mutated.
    assert items[1]["text"] == "B" and items[1]["priority"] == "critical"
    # B's target_cadence_days preserved (not mutated by this edit).
    assert items[1]["target_cadence_days"] == 7
    # C unchanged.
    assert (
        items[2]["text"] == "C"
        and items[2]["priority"] == "aspirational"
    )


def test_item_edit_unknown_item_returns_canary(
    tmp_path: Path, capsys,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Real", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Daily", item_text="xyzzy",
        priority="critical",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_UNKNOWN_ITEM


def test_item_edit_invalid_new_text_returns_invalid_field(
    tmp_path: Path, capsys,
) -> None:
    """--text '' is invalid."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Daily", item_text="Walk dog",
        new_text="   ",
        wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_INVALID_FIELD


def test_item_edit_clear_due_pattern_without_replacement_succeeds(
    tmp_path: Path,
) -> None:
    """Operator clears due_pattern without supplying a replacement
    (item becomes cadence-less; falls back to tracked-gap annotation).
    The clear flag's purpose isn't ONLY for swaps — also for full
    removal."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Bills", {
        "type": "routine", "name": "Bills",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Pay rent", "priority": "tracked",
            "due_pattern": {"type": "monthly", "day": 1},
            "escalate_at_days": 0,
        }],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Bills", item_text="Pay rent",
        clear_due_pattern=True,
    )
    assert code == 0
    item = _read_fm(vault, "Bills")["items"][0]
    assert "due_pattern" not in item
    assert "escalate_at_days" not in item
    # Item itself still exists.
    assert item["text"] == "Pay rent"
    assert item["priority"] == "tracked"


# ===========================================================================
# Salem-only enforcement (mirror of B1)
# ===========================================================================


def test_item_add_non_salem_instance_raises_scope_error(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "X", {
        "type": "routine", "name": "X",
        "cadence": {"type": "daily"},
        "items": [],
    })
    config = _config(vault, tmp_path, instance="hypatia")

    with pytest.raises(ScopeError, match="Salem-only"):
        cmd_item_add(config, record_name="X", item_text="Y")


def test_item_remove_non_salem_instance_raises_scope_error(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "X", {
        "type": "routine", "name": "X",
        "cadence": {"type": "daily"},
        "items": [{"text": "Y", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path, instance="kalle")

    with pytest.raises(ScopeError, match="Salem-only"):
        cmd_item_remove(config, record_name="X", item_text="Y")


def test_item_edit_non_salem_instance_raises_scope_error(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "X", {
        "type": "routine", "name": "X",
        "cadence": {"type": "daily"},
        "items": [{"text": "Y", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path, instance="hypatia")

    with pytest.raises(ScopeError, match="Salem-only"):
        cmd_item_edit(
            config, record_name="X", item_text="Y",
            priority="critical",
        )


# ===========================================================================
# Canary constant pinning (cross-agent contract)
# ===========================================================================


def test_item_kind_constants_pinned() -> None:
    """Pin the literal string values — SKILL.md quotes these
    verbatim; rename here = update SKILL.md + the talker dispatcher
    lazy import in lockstep."""
    assert ITEM_KIND_ADDED == "added"
    assert ITEM_KIND_REMOVED == "removed"
    assert ITEM_KIND_EDITED == "edited"
    assert ITEM_KIND_UNKNOWN_RECORD == "unknown_record"
    assert ITEM_KIND_UNKNOWN_ITEM == "unknown_item"
    assert ITEM_KIND_AMBIGUOUS_ITEM == "ambiguous_item"
    assert ITEM_KIND_CADENCE_CONFLICT == "cadence_conflict"
    assert ITEM_KIND_DUPLICATE_ITEM == "duplicate_item"
    assert ITEM_KIND_INVALID_FIELD == "invalid_field"


def test_all_item_kind_constants_exported_via_cli_all() -> None:
    """The routine.cli module's __all__ must list every ITEM_KIND_*
    constant — talker dispatcher imports them by name; missing entry
    would surface as ImportError at first dispatch."""
    from alfred.routine import cli as rcli

    expected_kinds = {
        "ITEM_KIND_ADDED", "ITEM_KIND_REMOVED", "ITEM_KIND_EDITED",
        "ITEM_KIND_UNKNOWN_RECORD", "ITEM_KIND_UNKNOWN_ITEM",
        "ITEM_KIND_AMBIGUOUS_ITEM", "ITEM_KIND_CADENCE_CONFLICT",
        "ITEM_KIND_DUPLICATE_ITEM", "ITEM_KIND_INVALID_FIELD",
    }
    missing = expected_kinds - set(rcli.__all__)
    assert not missing, (
        f"routine.cli __all__ missing ITEM_KIND_* constants: "
        f"{sorted(missing)!r}"
    )


# ===========================================================================
# self_care SET-path (06-27 gap) — SET↔READ round-trip with the Q2 read side
# ===========================================================================


def test_item_add_self_care_round_trips(tmp_path: Path) -> None:
    """``self_care=True`` is written into the new item (the hardcoded
    add-mutator allowlist previously DROPPED it silently)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "name": "Self Care",
        "cadence": {"type": "daily"}, "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(
        config, record_name="Self Care", item_text="Meditate",
        priority="aspirational", self_care=True,
    )
    assert code == 0
    item = _read_fm(vault, "Self Care")["items"][0]
    assert item["self_care"] is True


def test_item_add_without_self_care_omits_field(tmp_path: Path) -> None:
    """Absent self_care → the field is NOT written (behavior-preserving;
    reads as not-self-care)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"}, "items": [],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_add(config, record_name="Daily", item_text="Brush AM")
    assert code == 0
    assert "self_care" not in _read_fm(vault, "Daily")["items"][0]


def test_item_edit_sets_and_unsets_self_care(tmp_path: Path) -> None:
    """edit can both set (self_care=True) and unset (self_care=False)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Stretch", "priority": "aspirational"}],
    })
    config = _config(vault, tmp_path)

    assert cmd_item_edit(
        config, record_name="Daily", item_text="Stretch", self_care=True,
    ) == 0
    assert _read_fm(vault, "Daily")["items"][0]["self_care"] is True

    assert cmd_item_edit(
        config, record_name="Daily", item_text="Stretch", self_care=False,
    ) == 0
    assert _read_fm(vault, "Daily")["items"][0]["self_care"] is False


def test_self_care_set_then_read_routes_to_t3(tmp_path: Path) -> None:
    """SET↔READ end-to-end: a self_care item set via the CLI is parsed by
    the Q2 read side (Item.from_dict) and routed to the T3 lane by
    classify_routine_item — proving the SET path reaches the existing
    compute path with the SAME field name/semantics."""
    from datetime import date

    from alfred.routine.config import Item
    from alfred.tier.compute import classify_routine_item

    vault = tmp_path / "vault"
    _write_routine(vault, "Self Care", {
        "type": "routine", "name": "Self Care",
        "cadence": {"type": "daily"}, "items": [],
    })
    config = _config(vault, tmp_path)
    assert cmd_item_add(
        config, record_name="Self Care", item_text="Walk Fergus",
        priority="aspirational", self_care=True,
    ) == 0

    item = Item.from_dict(_read_fm(vault, "Self Care")["items"][0])
    assert item is not None and item.self_care is True

    result = classify_routine_item(
        priority=None, due_pattern=None, surface_at_days=None,
        escalate_at_days=None, target_cadence_days=None,
        completion_log={}, item_text=item.text, today=date(2026, 7, 1),
        self_care=item.self_care,
        default_escalate_at_days=None, default_surface_at_days=None,
    )
    assert result.tier == 3  # self-care → T3 lane


def test_non_self_care_item_not_routed_to_t3(tmp_path: Path) -> None:
    """A plain item (no self_care, no due_pattern, no target_cadence) →
    classify returns tier None (NOT T3)."""
    from datetime import date

    from alfred.routine.config import Item
    from alfred.tier.compute import classify_routine_item

    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"}, "items": [],
    })
    config = _config(vault, tmp_path)
    assert cmd_item_add(
        config, record_name="Daily", item_text="Brush AM",
    ) == 0

    item = Item.from_dict(_read_fm(vault, "Daily")["items"][0])
    assert item is not None and item.self_care is False

    result = classify_routine_item(
        priority=None, due_pattern=None, surface_at_days=None,
        escalate_at_days=None, target_cadence_days=None,
        completion_log={}, item_text=item.text, today=date(2026, 7, 1),
        self_care=item.self_care,
        default_escalate_at_days=None, default_surface_at_days=None,
    )
    assert result.tier is None
