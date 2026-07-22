"""Smoke tests for the vault ops layer.

Bootstrap-scope: prove ``vault_create``/``vault_read``/``vault_search`` work
end-to-end against a temp vault. Per-field validation, near-match dedup,
and template handling are out of scope here — those get their own tests as
the behaviour is touched.
"""

from __future__ import annotations

from pathlib import Path

from alfred.vault.ops import vault_create, vault_read, vault_search


def test_vault_create_then_read_round_trip(tmp_vault: Path):
    result = vault_create(
        tmp_vault,
        "task",
        "Bootstrap Smoke Task",
        set_fields={"status": "todo"},
    )
    assert result["path"] == "task/Bootstrap Smoke Task.md"

    read_back = vault_read(tmp_vault, result["path"])
    fm = read_back["frontmatter"]
    assert fm["type"] == "task"
    assert fm["name"] == "Bootstrap Smoke Task"
    assert fm["status"] == "todo"
    # ``created`` is auto-populated to today's ISO date.
    assert isinstance(fm.get("created"), str) or fm.get("created") is not None


def test_vault_search_finds_known_glob(tmp_vault: Path):
    # The conftest fixture seeds person/Sample Person.md — a glob over
    # person/*.md must surface it.
    hits = vault_search(tmp_vault, glob_pattern="person/*.md")
    paths = {h["path"] for h in hits}
    assert "person/Sample Person.md" in paths

    # And the parsed metadata should round-trip the type/name from frontmatter.
    sample = next(h for h in hits if h["path"] == "person/Sample Person.md")
    assert sample["type"] == "person"
    assert sample["name"] == "Sample Person"


def test_vault_search_glob_is_case_insensitive(tmp_vault: Path):
    """P4 / Surface (a) — 2026-06-07: glob case-folds by default.

    The 2026-06-06 Tilray conversation friction: Salem called
    ``vault_search glob="task/FMM Review video*.md"`` against the
    live file ``task/FMM Review Video.md`` and got no match
    (lowercase ``video`` vs capital ``V``). Operator-spoken phrasing
    rarely matches filesystem capitalization, and the
    ``vault_create`` near-match guard already prevents case-only-
    distinguishable filenames from coexisting in a well-formed vault.

    The fix is :meth:`pathlib.Path.glob`'s ``case_sensitive=False``
    kwarg (Python 3.12+). This test pins the contract: a lowercased
    glob pattern matches a capitalized filename.
    """
    # Seed a task with capital-V "Video" in the name to reproduce the
    # 2026-06-06 shape.
    (tmp_vault / "task").mkdir(exist_ok=True)
    (tmp_vault / "task" / "FMM Review Video.md").write_text(
        "---\ntype: task\nname: FMM Review Video\nstatus: todo\n---\n",
    )

    # Lowercase "video" in the glob — pre-P4 this would have missed.
    hits = vault_search(tmp_vault, glob_pattern="task/FMM Review video*.md")
    paths = {h["path"] for h in hits}
    assert "task/FMM Review Video.md" in paths, (
        f"Case-fold glob should find capitalized file; got paths={paths!r}"
    )


def test_vault_search_glob_case_fold_works_in_both_directions(tmp_vault: Path):
    """Capitalized glob pattern also matches lowercased file (symmetry pin).

    The case-fold is symmetric — if operators sometimes uppercase
    things in habits and the filename happens to be lowercase, the
    match still fires.
    """
    (tmp_vault / "task").mkdir(exist_ok=True)
    (tmp_vault / "task" / "lowercase-task.md").write_text(
        "---\ntype: task\nname: lowercase-task\nstatus: todo\n---\n",
    )

    # Capital-cased glob against lowercase file.
    hits = vault_search(tmp_vault, glob_pattern="task/LOWERCASE-TASK.md")
    paths = {h["path"] for h in hits}
    assert "task/lowercase-task.md" in paths


def test_vault_create_note_with_living_status(tmp_vault: Path):
    # Hypatia QA 2026-04-28: status='living' on a note record (e.g. a
    # permanent task list) was rejected by validation, forcing
    # status='active' which is semantically wrong for reference
    # material that never finishes. ``living`` is now a valid status
    # for ``note`` records — this test guards against the regression.
    result = vault_create(
        tmp_vault,
        "note",
        "VAC Form Unit Economics Model",
        set_fields={"status": "living"},
    )
    assert result["path"] == "note/VAC Form Unit Economics Model.md"

    read_back = vault_read(tmp_vault, result["path"])
    fm = read_back["frontmatter"]
    assert fm["type"] == "note"
    assert fm["status"] == "living"


# ---------------------------------------------------------------------------
# completion_log schema relaxation (2026-05-28)
# ---------------------------------------------------------------------------
#
# The 2026-05-28 tier Phase 1 migration surfaced a validator/runtime
# disagreement on ``completion_log``:
#   - Original schema (2026-05-26): ``completion_log`` in LIST_FIELDS
#     → validator demanded list shape on create.
#   - Runtime (routine/aggregator.py:201, routine/cli.py:132): always
#     coerced as ``dict[str, list[str]]`` (item text → ISO date list).
#   - Existing fixtures (vault/routine/Core Daily.md, For Self Health.md):
#     ship with ``completion_log: {}`` (empty dict).
#
# Net: existing fixtures were schema-tolerant on read but couldn't be
# re-created via the create path. Schema relaxation removes
# ``completion_log`` from LIST_FIELDS; both dict and list shapes are
# now valid at create time. The runtime aggregator stays the source
# of truth (dict-of-lists is canonical; coerces other shapes
# defensively).
#
# These tests pin the relaxation contract — any future re-tightening
# of the LIST_FIELDS membership would break the create flow for the
# existing routine fixtures' canonical shape.


def test_routine_create_with_completion_log_empty_dict_succeeds(
    tmp_vault: Path,
) -> None:
    """``completion_log: {}`` (empty dict — canonical empty for the
    runtime's dict-of-lists shape) MUST succeed on create. This is
    the shape every existing routine fixture on disk uses; the
    validator must accept it post-relaxation."""
    result = vault_create(
        tmp_vault,
        "routine",
        "Schema Relax Empty Dict",
        set_fields={
            "status": "active",
            "cadence": {"type": "daily"},
            "items": [{"text": "x", "priority": "tracked"}],
            "completion_log": {},
        },
    )
    assert result["path"] == "routine/Schema Relax Empty Dict.md"

    read_back = vault_read(tmp_vault, result["path"])
    fm = read_back["frontmatter"]
    # The dict round-trips — frontmatter still carries the empty dict
    # the operator/migration script supplied. No silent list-coercion.
    assert fm["completion_log"] == {}


def test_routine_create_with_completion_log_empty_list_succeeds(
    tmp_vault: Path,
) -> None:
    """``completion_log: []`` (empty list — what the tier Phase 1
    migration script writes) MUST succeed on create. Pre-relaxation
    this was the ONLY valid shape; post-relaxation it remains valid
    alongside dict. The migration script's choice is preserved for
    forensic clarity (per the script's comment block) and this pin
    keeps that pathway operational."""
    result = vault_create(
        tmp_vault,
        "routine",
        "Schema Relax Empty List",
        set_fields={
            "status": "active",
            "cadence": {"type": "daily"},
            "items": [{"text": "x", "priority": "tracked"}],
            "completion_log": [],
        },
    )
    assert result["path"] == "routine/Schema Relax Empty List.md"

    read_back = vault_read(tmp_vault, result["path"])
    fm = read_back["frontmatter"]
    assert fm["completion_log"] == []


def test_routine_create_with_completion_log_populated_dict_succeeds(
    tmp_vault: Path,
) -> None:
    """``completion_log: {"Reading": ["2026-05-28"]}`` — the populated
    runtime shape after the operator has actually completed an item —
    MUST succeed on create. Documents that the validator doesn't
    silently coerce the nested dict-of-lists to anything else; the
    aggregator's source-of-truth shape round-trips cleanly through
    create."""
    populated = {
        "Reading": ["2026-05-28"],
        "Writing": ["2026-05-26", "2026-05-27", "2026-05-28"],
    }
    result = vault_create(
        tmp_vault,
        "routine",
        "Schema Relax Populated",
        set_fields={
            "status": "active",
            "cadence": {"type": "daily"},
            "items": [
                {"text": "Reading", "priority": "aspirational"},
                {"text": "Writing", "priority": "aspirational"},
            ],
            "completion_log": populated,
        },
    )
    assert result["path"] == "routine/Schema Relax Populated.md"

    read_back = vault_read(tmp_vault, result["path"])
    fm = read_back["frontmatter"]
    # Full nested structure round-trips.
    assert fm["completion_log"] == populated
    assert fm["completion_log"]["Reading"] == ["2026-05-28"]


def test_completion_log_not_in_list_fields_registry() -> None:
    """Direct registry-membership pin: ``completion_log`` MUST NOT be
    in ``LIST_FIELDS`` (schema.py). The relaxation 2026-05-28 dropped
    it specifically because the validator's strict-list demand
    conflicted with the runtime's dict-of-lists canonical shape.

    Pin so a future refactor that re-adds it (e.g. an over-zealous
    'add all routine fields' grep) surfaces immediately rather than
    re-breaking the create flow for the canonical fixture shape."""
    from alfred.vault.schema import LIST_FIELDS
    assert "completion_log" not in LIST_FIELDS, (
        "completion_log was removed from LIST_FIELDS on 2026-05-28 "
        "because the validator's strict-list demand conflicted with "
        "the runtime's dict-of-lists canonical shape. See the comment "
        "block in schema.py near the LIST_FIELDS definition for the "
        "full rationale. Re-adding this entry will break the routine "
        "create flow for the canonical fixture shape."
    )
    # Sanity: ``items`` is still in LIST_FIELDS (the relaxation was
    # surgical — only completion_log was affected).
    assert "items" in LIST_FIELDS


# ---------------------------------------------------------------------------
# Secure delete (s.49 destruction hardening) — overwrite-before-unlink +
# force-permanent (bypass Obsidian trash). See scribe.retention destroy path.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402
from alfred.vault import ops as _ops  # noqa: E402
from alfred.vault.ops import secure_unlink, vault_delete  # noqa: E402


def test_secure_unlink_overwrites_bytes_BEFORE_unlink(tmp_path, monkeypatch):
    p = tmp_path / "phi.txt"
    secret = b"PATIENT SECRET SOAP NOTE - PHI"
    p.write_bytes(secret)
    seen = {}
    orig = _ops.Path.unlink

    def spy_unlink(self, *a, **k):
        # capture what's ON DISK at the moment unlink is called → proves overwrite happened FIRST
        if self == p:
            seen["at_unlink"] = self.read_bytes()
        return orig(self, *a, **k)

    monkeypatch.setattr(_ops.Path, "unlink", spy_unlink)
    assert secure_unlink(p) is True
    assert seen["at_unlink"] == b"\x00" * len(secret)      # OVERWRITTEN before the unlink (not the PHI)
    assert not p.exists()


def test_secure_unlink_missing_file_returns_true(tmp_path):
    assert secure_unlink(tmp_path / "gone.txt") is True     # idempotent (already-gone is success)


def test_secure_unlink_real_unlink_failure_returns_false(tmp_path, monkeypatch):
    p = tmp_path / "phi.txt"
    p.write_bytes(b"data")

    def boom(self, *a, **k):
        raise OSError("EPERM")

    monkeypatch.setattr(_ops.Path, "unlink", boom)
    assert secure_unlink(p) is False                        # a REAL failure is counted, not swallowed


def test_vault_delete_secure_bypasses_obsidian_and_overwrites(tmp_path, monkeypatch):
    """secure=True: ALWAYS permanent (never Obsidian-trashed) even when Obsidian IS available, and the
    plaintext is overwritten before the unlink."""
    vault = tmp_path / "vault"
    (vault / "clinical_note").mkdir(parents=True)
    note = vault / "clinical_note" / "R.md"
    secret = b"---\ntype: clinical_note\n---\nPHI SOAP body\n"
    note.write_bytes(secret)
    # Obsidian IS available — a non-secure delete would route to its trash. A spy proves secure skips it.
    trash_calls = []
    monkeypatch.setattr(_ops.obsidian, "is_available", lambda: True)
    monkeypatch.setattr(_ops.obsidian, "delete_file", lambda name: trash_calls.append(name) or True)
    seen = {}
    orig = _ops.Path.unlink

    def spy_unlink(self, *a, **k):
        if self == note:
            seen["at_unlink"] = self.read_bytes()
        return orig(self, *a, **k)

    monkeypatch.setattr(_ops.Path, "unlink", spy_unlink)
    vault_delete(vault, "clinical_note/R.md", scope="stayc_clinical_destroy", secure=True)
    assert trash_calls == []                                # Obsidian trash NEVER invoked (forced permanent)
    assert not note.exists()                                # gone from disk, not trashed
    assert seen["at_unlink"] == b"\x00" * len(secret)       # overwritten before the unlink


def test_vault_delete_non_secure_still_uses_obsidian_when_available(tmp_path, monkeypatch):
    """Regression: the DEFAULT (secure=False) delete still respects the Obsidian trash routing when
    Obsidian is available — the secure path is opt-in, not a global behavior change."""
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    (vault / "task" / "T.md").write_text("---\ntype: task\n---\nb\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(_ops.obsidian, "is_available", lambda: True)
    monkeypatch.setattr(_ops.obsidian, "delete_file", lambda name: calls.append(name) or True)
    vault_delete(vault, "task/T.md")                        # no secure, no scope
    assert calls == ["task/T"]                              # routed to Obsidian (trash-respecting) as before
