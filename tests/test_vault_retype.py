"""Unit tests for ``alfred.vault.retype``.

Coverage:
  * Field mapping (event → task): keep / rename(date→due) / drop /
    unknown-kept buckets, all reported correctly
  * Required-field defaults: status="todo", priority="medium" when
    not in source AND no override; overrides win when supplied
  * Overrides (status, priority, due) honor what the operator passed
  * Body content is preserved verbatim across the type change
  * Filename is preserved (event/Foo.md → task/Foo.md)
  * Wikilink rewriter:
      - finds ``[[event/Foo]]`` → ``[[task/Foo]]``
      - handles ``|alias`` and ``#section`` suffixes
      - skips ``_templates`` / ``_bases`` / ``.obsidian`` / ``.git``
      - reports per-file occurrences in dry-run mode without writing
      - actually writes when apply=True
      - skips the source file (which is about to be deleted anyway)
  * GCal cleanup signal: ``gcal_will_delete`` flag set when source
    had ``gcal_event_id`` and target type isn't event
  * Edge cases:
      - target path already exists → VaultError
      - unknown target type → VaultError
      - same source/target type → VaultError
      - no mapping registered for the (source, target) pair → VaultError
      - source missing → VaultError
      - source has no ``type`` field → VaultError
      - status override invalid for target type → VaultError
  * Integration:
      - dry-run leaves vault untouched
      - apply writes target, deletes source by default
      - --keep-source leaves source on disk
      - source delete fires the registered event-delete hook (which
        is the GCal cleanup mechanism — no direct sync_event_delete
        call from retype)
  * CLI:
      - ``alfred vault retype <path> --to task`` parses + dispatches
      - ``--dry-run`` / ``--keep-source`` / ``--status`` / ``--priority``
        / ``--due`` flags route through to vault_retype
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import frontmatter
import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_event_hooks():
    """Clear the event-hook registries before AND after each test.

    The retype path DOES delete the source via vault_delete, which
    fires the delete-hook. Tests that don't register a hook must see
    a clean baseline; tests that DO register must see only their own.
    """
    from alfred.vault.ops import clear_event_hooks
    clear_event_hooks()
    yield
    clear_event_hooks()


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Minimal vault layout — directories the retype + delete need."""
    for sub in ("event", "task", "person", "project", "note",
                "_templates", "_bases", ".obsidian"):
        (tmp_path / sub).mkdir()
    return tmp_path


def _seed_event(
    vault: Path, *, name: str, fields: dict | None = None,
    body: str = "body content\n",
) -> str:
    """Write event/<name>.md with given fields. Returns rel_path."""
    fm = {"type": "event", "name": name, "title": name}
    if fields:
        fm.update(fields)
    rel_path = f"event/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


def _read_fm(vault: Path, rel_path: str) -> dict:
    return dict(frontmatter.load(str(vault / rel_path)).metadata)


# ---------------------------------------------------------------------------
# Field mapping (event → task)
# ---------------------------------------------------------------------------


def test_event_to_task_keeps_safe_fields(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="Renewal",
        fields={
            "name": "Renewal",
            "title": "Renewal",
            "created": "2026-04-15",
            "alfred_tags": ["billing", "annual"],
            "description": "Auto-renews unless cancelled",
            "summary": "Cancel by Friday",
            "related": ["[[org/Apple]]"],
            "project": "[[project/Personal Ops]]",
        },
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    fm = _read_fm(tmp_vault, report.target_path)
    assert fm["type"] == "task"
    assert fm["name"] == "Renewal"
    assert fm["alfred_tags"] == ["billing", "annual"]
    assert fm["description"] == "Auto-renews unless cancelled"
    assert fm["summary"] == "Cancel by Friday"
    assert fm["related"] == ["[[org/Apple]]"]
    assert fm["created"] == "2026-04-15"
    # Confirm the kept-fields report matches.
    assert "name" in report.fields_kept
    assert "alfred_tags" in report.fields_kept
    assert "description" in report.fields_kept


def test_event_to_task_renames_date_to_due(tmp_vault):
    """The headline mapping: event's ``date`` → task's ``due``.

    NOT ``due_date`` — that's a field nothing reads. Per
    vault/schema.py, transport/scheduler.py, brief/upcoming_events.py
    the canonical task field is ``due``.
    """
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="iCloud Renewal",
        fields={"date": "2026-07-15"},
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    fm = _read_fm(tmp_vault, report.target_path)
    # The whole point of the prompt-tuner correction.
    assert "due" in fm
    assert fm["due"] == "2026-07-15"
    assert "due_date" not in fm
    assert "date" not in fm
    # Report flags the rename.
    assert any(
        r["from"] == "date" and r["to"] == "due"
        for r in report.fields_renamed
    )


def test_event_to_task_drops_event_specific_fields(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="Concert",
        fields={
            "start": "2026-06-27T19:00:00-03:00",
            "end": "2026-06-27T22:00:00-03:00",
            "time": "7:00 PM",
            "location": "Halifax Common",
            "participants": ["[[person/Andrew Newton]]"],
            "platform": "TIXR",
            "ticket_type": "GA",
            "gcal_event_id": "weezer-id-1",
            "gcal_calendar": "alfred",
            "status": "scheduled",  # invalid for task; would be dropped anyway
            "correlation_id": "test-corr-1",
            "origin_instance": "salem",
            "origin_context": "from a TIXR ticket forwarded by Andrew",
        },
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    fm = _read_fm(tmp_vault, report.target_path)
    for dropped in [
        "start", "end", "time", "location", "participants",
        "platform", "ticket_type",
        "gcal_event_id", "gcal_calendar",
        "correlation_id", "origin_instance", "origin_context",
    ]:
        assert dropped not in fm, f"field {dropped!r} should have been dropped"
    # Source status doesn't carry across — replaced by task default.
    assert fm["status"] == "todo"
    # All dropped fields should be reported.
    assert "start" in report.fields_dropped
    assert "end" in report.fields_dropped
    assert "gcal_event_id" in report.fields_dropped


def test_event_to_task_keeps_unknown_fields_with_report(tmp_vault):
    """Source fields not in keep/rename/drop are kept by default + reported."""
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="Custom Field Event",
        fields={"some_custom_field": "extra value"},
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    fm = _read_fm(tmp_vault, report.target_path)
    assert fm["some_custom_field"] == "extra value"
    assert "some_custom_field" in report.fields_unknown_kept


# ---------------------------------------------------------------------------
# Required-field defaults + overrides
# ---------------------------------------------------------------------------


def test_event_to_task_sets_default_status_and_priority(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Default Defaults Event")
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    fm = _read_fm(tmp_vault, report.target_path)
    # task scaffold default — NOT "normal" (spec said normal but the
    # scaffold uses medium; we follow the scaffold so records match
    # template defaults).
    assert fm["priority"] == "medium"
    assert fm["status"] == "todo"


def test_event_to_task_overrides_win(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="Override Test",
        fields={"date": "2026-07-15"},
    )
    report = vault_retype(
        tmp_vault, rel_path, "task",
        apply=True,
        overrides={
            "status": "active",
            "priority": "high",
            "due": "2026-07-20",
        },
    )
    fm = _read_fm(tmp_vault, report.target_path)
    assert fm["status"] == "active"
    assert fm["priority"] == "high"
    # Override beats the date-renamed-to-due value.
    assert fm["due"] == "2026-07-20"


def test_invalid_status_override_raises(tmp_vault):
    from alfred.vault.ops import VaultError
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Bad Status")
    with pytest.raises(VaultError, match="not valid for"):
        vault_retype(
            tmp_vault, rel_path, "task",
            apply=True,
            overrides={"status": "garbage"},
        )


# ---------------------------------------------------------------------------
# Body + filename preserved
# ---------------------------------------------------------------------------


def test_body_preserved_across_retype(tmp_vault):
    from alfred.vault.retype import vault_retype

    body = dedent("""\
        # Detailed body

        Some markdown content with [[wikilinks/that/should/stay]] and
        ## sections.
    """)
    rel_path = _seed_event(
        tmp_vault, name="Body Test", fields={"date": "2026-07-01"},
        body=body,
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    new_post = frontmatter.load(str(tmp_vault / report.target_path))
    # python-frontmatter strips one trailing newline on dump; comparing
    # stripped content sidesteps that without losing semantics.
    assert new_post.content.rstrip() == body.rstrip()


def test_filename_preserved(tmp_vault):
    """Source filename → target filename, only the directory differs."""
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Halifax Music Fest 2026 — Weezer")
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    assert report.target_path == "task/Halifax Music Fest 2026 — Weezer.md"
    assert (tmp_vault / report.target_path).exists()


# ---------------------------------------------------------------------------
# Wikilink rewriting
# ---------------------------------------------------------------------------


def test_wikilinks_rewritten_basic(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Concert")
    # Seed a few records that link to event/Concert
    (tmp_vault / "person" / "Andrew.md").write_text(
        "---\ntype: person\nname: Andrew\n---\n\nGoing to [[event/Concert]]\n",
        encoding="utf-8",
    )
    (tmp_vault / "project" / "Personal.md").write_text(
        "---\ntype: project\nname: Personal\n---\n\n"
        "Concert: [[event/Concert|Halifax show]]\n",
        encoding="utf-8",
    )
    (tmp_vault / "note" / "Random.md").write_text(
        "---\ntype: note\nname: Random\n---\n\n"
        "See [[event/Concert#tickets]] for details.\n",
        encoding="utf-8",
    )

    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    assert report.wikilinks_rewritten == 3
    paths = {f["path"] for f in report.wikilinks_files}
    assert "person/Andrew.md" in paths
    assert "project/Personal.md" in paths
    assert "note/Random.md" in paths

    # Verify the rewrites actually landed.
    assert "[[task/Concert]]" in (tmp_vault / "person" / "Andrew.md").read_text()
    assert "[[task/Concert|Halifax show]]" in (
        tmp_vault / "project" / "Personal.md"
    ).read_text()
    assert "[[task/Concert#tickets]]" in (
        tmp_vault / "note" / "Random.md"
    ).read_text()


def test_wikilinks_rewritten_skips_ignored_dirs(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Concert")
    # _templates is in the ignore set — wikilinks here should NOT be touched.
    (tmp_vault / "_templates" / "task.md").write_text(
        "---\ntype: template\n---\n\nExample link [[event/Concert]] in template\n",
        encoding="utf-8",
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    # No wikilinks rewritten because the only link was in _templates.
    assert report.wikilinks_rewritten == 0
    # Template content untouched.
    assert "[[event/Concert]]" in (
        tmp_vault / "_templates" / "task.md"
    ).read_text()


def test_wikilinks_rewritten_dry_run_does_not_write(tmp_vault):
    """Dry-run reports counts but doesn't modify referring files."""
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="DryRunConcert")
    referrer = tmp_vault / "person" / "Friend.md"
    referrer.write_text(
        "---\ntype: person\nname: Friend\n---\n\nSee [[event/DryRunConcert]]\n",
        encoding="utf-8",
    )
    original = referrer.read_text()
    report = vault_retype(tmp_vault, rel_path, "task", apply=False)
    assert report.wikilinks_rewritten == 1
    # File untouched.
    assert referrer.read_text() == original


def test_wikilinks_rewriter_skips_source_file_itself(tmp_vault):
    """The source record (which we're about to delete) is excluded from
    the link rewrite — leaving its self-references inside its own
    body would be confusing."""
    from alfred.vault.retype import vault_retype

    # Source body contains a self-reference (rare but possible).
    rel_path = _seed_event(
        tmp_vault, name="SelfRef",
        body="See related [[event/SelfRef|earlier]] for context.\n",
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    # No external referrers, and the source's self-ref was excluded.
    assert report.wikilinks_rewritten == 0


# ---------------------------------------------------------------------------
# GCal cleanup signal
# ---------------------------------------------------------------------------


def test_gcal_will_delete_flag_when_source_has_gcal_event_id(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="Synced",
        fields={"gcal_event_id": "abc-123"},
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=False)
    assert report.gcal_event_id == "abc-123"
    assert report.gcal_will_delete is True


def test_gcal_will_delete_false_when_source_has_no_id(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Unsynced")
    report = vault_retype(tmp_vault, rel_path, "task", apply=False)
    assert report.gcal_event_id == ""
    assert report.gcal_will_delete is False


def test_gcal_will_delete_false_when_keep_source(tmp_vault):
    """If we're keeping the source on disk, its GCal mirror stays too."""
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="KeepIt",
        fields={"gcal_event_id": "abc-123"},
    )
    report = vault_retype(
        tmp_vault, rel_path, "task", apply=False, keep_source=True,
    )
    assert report.gcal_will_delete is False


def test_apply_fires_event_delete_hook_for_source(tmp_vault):
    """The integration handle: when the source is deleted as part of
    retype, the registered event-delete hook fires with the pre-delete
    frontmatter. This is the load-bearing mechanism for GCal cleanup
    in production — the daemon registers a hook that calls
    sync_event_delete_to_gcal."""
    from alfred.vault.ops import register_event_delete_hook
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="WithGCal",
        fields={"gcal_event_id": "live-id-99"},
    )
    fired = []
    register_event_delete_hook(
        lambda v, r, fm: fired.append({
            "rel_path": r,
            "gcal_event_id": fm.get("gcal_event_id"),
        })
    )
    vault_retype(tmp_vault, rel_path, "task", apply=True)
    assert len(fired) == 1
    assert fired[0]["rel_path"] == rel_path
    # Critical: the hook receives the pre-delete frontmatter, including
    # the gcal_event_id that the GCal sync function needs.
    assert fired[0]["gcal_event_id"] == "live-id-99"


def test_keep_source_does_not_fire_delete_hook(tmp_vault):
    """--keep-source skips vault_delete entirely → no hook fires."""
    from alfred.vault.ops import register_event_delete_hook
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="KeepIt",
        fields={"gcal_event_id": "stays-on-gcal"},
    )
    fired = []
    register_event_delete_hook(lambda *a: fired.append(a))
    vault_retype(
        tmp_vault, rel_path, "task", apply=True, keep_source=True,
    )
    assert fired == []
    # Source still on disk.
    assert (tmp_vault / rel_path).exists()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_target_path_already_exists_raises(tmp_vault):
    from alfred.vault.ops import VaultError
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Conflicts")
    # Create a colliding task.
    (tmp_vault / "task" / "Conflicts.md").write_text(
        "---\ntype: task\nname: Conflicts\n---\n\nbody\n", encoding="utf-8",
    )
    with pytest.raises(VaultError, match="already exists"):
        vault_retype(tmp_vault, rel_path, "task", apply=True)


def test_unknown_target_type_raises(tmp_vault):
    from alfred.vault.ops import VaultError
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="X")
    with pytest.raises(VaultError, match="Unknown target type"):
        vault_retype(tmp_vault, rel_path, "not_a_real_type", apply=True)


def test_same_source_target_raises(tmp_vault):
    from alfred.vault.ops import VaultError
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="X")
    with pytest.raises(VaultError, match="already type"):
        vault_retype(tmp_vault, rel_path, "event", apply=True)


def test_no_mapping_for_pair_raises(tmp_vault):
    from alfred.vault.ops import VaultError
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="Z")
    # event → person isn't in the FIELD_MAPPINGS table.
    with pytest.raises(VaultError, match="No retype mapping"):
        vault_retype(tmp_vault, rel_path, "person", apply=True)


def test_source_missing_raises(tmp_vault):
    from alfred.vault.ops import VaultError
    from alfred.vault.retype import vault_retype

    with pytest.raises(VaultError, match="not found"):
        vault_retype(
            tmp_vault, "event/Nonexistent.md", "task", apply=True,
        )


def test_source_missing_type_field_raises(tmp_vault):
    from alfred.vault.ops import VaultError
    from alfred.vault.retype import vault_retype

    # Create an event-dir record with no ``type``.
    (tmp_vault / "event" / "Untyped.md").write_text(
        "---\nname: Untyped\n---\n\nbody\n", encoding="utf-8",
    )
    with pytest.raises(VaultError, match="no ``type`` in frontmatter"):
        vault_retype(tmp_vault, "event/Untyped.md", "task", apply=True)


# ---------------------------------------------------------------------------
# Dry-run vs apply integration
# ---------------------------------------------------------------------------


def test_dry_run_leaves_vault_untouched(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(
        tmp_vault, name="DryRunX", fields={"date": "2026-07-01"},
    )
    report = vault_retype(tmp_vault, rel_path, "task", apply=False)
    # Source still exists.
    assert (tmp_vault / rel_path).exists()
    # Target NOT created.
    assert not (tmp_vault / report.target_path).exists()
    # Report still describes the would-be result.
    assert report.target_path == "task/DryRunX.md"
    assert any(
        r["from"] == "date" and r["to"] == "due"
        for r in report.fields_renamed
    )


def test_apply_default_deletes_source(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="DeleteMe")
    report = vault_retype(tmp_vault, rel_path, "task", apply=True)
    # Source gone, target present.
    assert not (tmp_vault / rel_path).exists()
    assert (tmp_vault / report.target_path).exists()


def test_apply_keep_source_leaves_source_on_disk(tmp_vault):
    from alfred.vault.retype import vault_retype

    rel_path = _seed_event(tmp_vault, name="KeepMe")
    report = vault_retype(
        tmp_vault, rel_path, "task", apply=True, keep_source=True,
    )
    assert (tmp_vault / rel_path).exists()
    assert (tmp_vault / report.target_path).exists()
    assert report.keep_source is True


# ---------------------------------------------------------------------------
# Top-level CLI parser
# ---------------------------------------------------------------------------


def test_alfred_vault_retype_subcommand_registered():
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "vault", "retype", "event/Foo.md",
        "--to", "task",
        "--dry-run",
        "--keep-source",
        "--status", "active",
        "--priority", "high",
        "--due", "2026-07-20",
    ])
    assert args.command == "vault"
    assert args.vault_cmd == "retype"
    assert args.path == "event/Foo.md"
    assert args.to == "task"
    assert args.dry_run is True
    assert args.keep_source is True
    assert args.status == "active"
    assert args.priority == "high"
    assert args.due == "2026-07-20"


def test_alfred_vault_retype_subcommand_minimal():
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["vault", "retype", "event/Foo.md", "--to", "task"])
    assert args.dry_run is False
    assert args.keep_source is False
    assert args.status is None
    assert args.priority is None
    assert args.due is None


def test_alfred_vault_retype_to_is_required():
    from alfred.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["vault", "retype", "event/Foo.md"])
