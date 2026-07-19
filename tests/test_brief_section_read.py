"""Tests for the shared defensive section-file read helper (task #25).

``safe_read_section_file`` centralizes the FileNotFoundError + OSError +
UnicodeDecodeError catch that recurred three times in brief/ section readers
(watches, tier_section, stayc_relay). The load-bearing property: a section
renderer is called BARE by the daemon, so a read that raises (esp.
UnicodeDecodeError, which subclasses ValueError NOT OSError) would kill the
whole brief. The helper degrades every read failure to a discriminated
result instead.

Pins here:
  * the helper catches each of the 3 exception classes → the right status,
    text None (per exception type);
  * the 3 migrated call sites preserve their exact degrade behavior on a
    non-UTF-8 / missing file (the regression the refactor must not break).
"""

from __future__ import annotations

from pathlib import Path

import structlog

from alfred.brief.utils import (
    SectionRead,
    SectionReadStatus,
    safe_read_section_file,
)


# --- the helper itself -----------------------------------------------------


def test_ok_returns_text(tmp_path) -> None:
    p = tmp_path / "f.md"
    p.write_text("hello world\n", encoding="utf-8")
    read = safe_read_section_file(p)
    assert read.status is SectionReadStatus.OK
    assert read.text == "hello world\n"
    assert read.detail == ""
    assert read.error_type == ""


def test_not_found(tmp_path) -> None:
    read = safe_read_section_file(tmp_path / "missing.md")
    assert read.status is SectionReadStatus.NOT_FOUND
    assert read.text is None
    assert read.error_type == "FileNotFoundError"


def test_os_error_on_directory(tmp_path) -> None:
    """Reading a directory raises IsADirectoryError (an OSError, NOT
    FileNotFoundError) → OS_ERROR."""
    d = tmp_path / "adir"
    d.mkdir()
    read = safe_read_section_file(d)
    assert read.status is SectionReadStatus.OS_ERROR
    assert read.text is None
    assert read.error_type == "IsADirectoryError"


def test_decode_error_on_non_utf8(tmp_path) -> None:
    """A non-UTF-8 file raises UnicodeDecodeError (ValueError subclass, NOT
    OSError — the escaping-catch bug this helper closes) → DECODE_ERROR.
    Mutation: drop the UnicodeDecodeError branch → this raises."""
    p = tmp_path / "bad.md"
    p.write_bytes(b"valid ascii then \xff\xfe raw bytes\n")
    read = safe_read_section_file(p)
    assert read.status is SectionReadStatus.DECODE_ERROR
    assert read.text is None
    assert read.error_type == "UnicodeDecodeError"


def test_result_is_namedtuple_shape() -> None:
    """Guards the field order callers unpack (status, text, detail, error_type)."""
    r = SectionRead(SectionReadStatus.OK, "x", "", "")
    assert r.status is SectionReadStatus.OK
    assert r.text == "x"


# --- migrated site: watches.load_watch_state preserves behavior ------------


def test_watches_non_utf8_state_degrades_to_fresh(tmp_path) -> None:
    """A binary-corrupted watch-state file → fresh baseline ({}) + a warning
    (the a3 regression: UnicodeDecodeError must not escape). error_type is
    still surfaced as UnicodeDecodeError via the helper."""
    from alfred.brief.watches import load_watch_state

    p = tmp_path / "s.json"
    p.write_bytes(b"\xff\xfe not utf-8 at all")
    with structlog.testing.capture_logs() as cap:
        states = load_watch_state(p)
    assert states == {}
    warns = [c for c in cap if c.get("event") == "brief.watches_state_load_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "UnicodeDecodeError"


def test_watches_bad_json_still_degrades(tmp_path) -> None:
    """A valid-UTF-8 but non-JSON state file → {} + warning with
    JSONDecodeError (the json.loads catch, unchanged by the migration)."""
    from alfred.brief.watches import load_watch_state

    p = tmp_path / "s.json"
    p.write_text("{ not json", encoding="utf-8")
    with structlog.testing.capture_logs() as cap:
        states = load_watch_state(p)
    assert states == {}
    warns = [c for c in cap if c.get("event") == "brief.watches_state_load_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "JSONDecodeError"


def test_watches_missing_state_is_silent(tmp_path) -> None:
    """A missing state file is normal (first run) → {} with NO warning
    (the pre-check short-circuits before the helper). Migration must not
    start warning on absent state."""
    from alfred.brief.watches import load_watch_state

    with structlog.testing.capture_logs() as cap:
        states = load_watch_state(tmp_path / "never_written.json")
    assert states == {}
    assert not [c for c in cap if c.get("event") == "brief.watches_state_load_failed"]


# --- migrated site: tier_section frontmatter pre-validate preserves msgs ----


def test_tier_validate_non_utf8_message(tmp_path) -> None:
    from alfred.brief.tier_section import _validate_frontmatter_yaml

    p = tmp_path / "rec.md"
    p.write_bytes(b"---\ntitle: x\xff\xfe\n---\n")
    assert (_validate_frontmatter_yaml(p) or "").startswith("not utf-8:")


def test_tier_validate_missing_file_message(tmp_path) -> None:
    """Missing file → 'read failed:' (the prior ``except OSError`` included
    FileNotFoundError, so NOT_FOUND maps to the same message)."""
    from alfred.brief.tier_section import _validate_frontmatter_yaml

    assert (_validate_frontmatter_yaml(tmp_path / "gone.md") or "").startswith(
        "read failed:"
    )


# --- migrated 4th site: health_section (bare-called render path) -----------


def test_health_non_utf8_bit_record_does_not_crash(tmp_path) -> None:
    """A non-UTF-8 BIT record must NOT crash the brief. ``_parse_frontmatter``
    used a bare ``except OSError`` that missed UnicodeDecodeError; the render
    is called BARE by the daemon, so it escaped and killed the whole brief.
    Now it degrades to the no-record sentinel. Mutation: revert
    _parse_frontmatter to ``except OSError`` → this raises.

    Also pins the degrade-path log (ILB): a corrupt record must not silently
    masquerade as "BIT hasn't run yet"."""
    from alfred.brief.health_section import render_health_section

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "Alfred BIT 2026-07-19.md").write_bytes(
        b"---\noverall_status: ok\xff\xfe\n---\nbody\n",
    )
    with structlog.testing.capture_logs() as cap:
        out = render_health_section(tmp_path, state_path=None, today="2026-07-19")
    assert "No BIT run recorded yet" in out
    failed = [
        c for c in cap if c.get("event") == "brief.health_record_load_failed"
    ]
    assert len(failed) == 1
    assert failed[0]["stage"] == "read"
    assert failed[0]["error_type"] == "UnicodeDecodeError"


def test_health_non_utf8_state_file_does_not_crash(tmp_path) -> None:
    """A non-UTF-8 BIT STATE file must NOT crash the brief either.
    ``_read_state_latest`` caught ``(OSError, json.JSONDecodeError)`` —
    UnicodeDecodeError is a SIBLING of JSONDecodeError under ValueError, not a
    subclass, so it escaped. Now it degrades. Mutation: revert the state
    read's catch → this raises.

    Also pins the degrade-path log (ILB)."""
    from alfred.brief.health_section import render_health_section

    state = tmp_path / "bit_state.json"
    state.write_bytes(b"\xff\xfe not utf-8")
    # No vault records → falls through to the state file → decode fail → sentinel.
    with structlog.testing.capture_logs() as cap:
        out = render_health_section(tmp_path, state_path=state, today="2026-07-19")
    assert "No BIT run recorded yet" in out
    failed = [
        c for c in cap if c.get("event") == "brief.health_state_load_failed"
    ]
    assert len(failed) == 1
    assert failed[0]["stage"] == "read"
    assert failed[0]["error_type"] == "UnicodeDecodeError"


def test_health_body_read_failure_degrades_to_frontmatter(tmp_path, monkeypatch) -> None:
    """If the second (body) read fails after the frontmatter read succeeded
    (a race / transient I/O error), the section still renders from the parsed
    frontmatter instead of crashing. Forces the 2nd helper call to fail.

    The pin binds the DEGRADE path specifically. The fixture body carries a
    real ``## Summary`` block with parseable per-tool lines, so a SUCCESSFUL
    body read renders those (``- curator  ok``) and NEVER emits the
    ``tool summary:`` marker. That marker appears ONLY on the tool_counts
    fallback, which is only reached when the body is empty — i.e. the degrade
    path. Asserting the marker (not just ``curator``, which appears on both
    paths) is what makes reverting the body read to a raw ``read_text`` FAIL
    this test: with a working read the ``## Summary`` block parses and the
    marker is absent. (Bind-check verified: revert body read → this FAILS.)
    The ``## Summary`` block is load-bearing — don't strip it.
    """
    from alfred.brief import health_section as hs
    from alfred.brief.utils import SectionRead, SectionReadStatus

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "Alfred BIT 2026-07-19.md").write_text(
        "---\n"
        "overall_status: ok\n"
        "mode: quick\n"
        "created: 2026-07-19\n"
        "started: 2026-07-19T05:55:00\n"
        "name: Alfred BIT 2026-07-19\n"
        "tool_counts:\n"
        "  curator: 1\n"
        "---\n"
        "## Summary\n"
        "\n"
        "[OK] curator\n"
        "[OK] janitor\n",
        encoding="utf-8",
    )
    real = hs.safe_read_section_file
    state = {"n": 0}

    def flaky(path):
        state["n"] += 1
        if state["n"] == 2:  # the body read (2nd call for this record)
            return SectionRead(SectionReadStatus.OS_ERROR, None, "boom", "OSError")
        return real(path)

    monkeypatch.setattr(hs, "safe_read_section_file", flaky)
    with structlog.testing.capture_logs() as cap:
        out = hs.render_health_section(tmp_path, state_path=None, today="2026-07-19")
    assert "Overall:" in out          # rendered from frontmatter, no crash
    # Degrade-only marker: the empty-body tool_counts fallback. A working body
    # read would render the ``## Summary`` per-tool lines instead → no marker.
    assert "tool summary:" in out
    assert "curator" in out           # tool_counts fallback content
    # Partial-degrade is signalled, not silent (ILB).
    body_fail = [
        c for c in cap if c.get("event") == "brief.health_body_read_failed"
    ]
    assert len(body_fail) == 1
    assert body_fail[0]["error_type"] == "OSError"


# --- migrated site: operations (bare-called via format_operations_section) --


def test_operations_count_audit_non_utf8_degrades(tmp_path) -> None:
    """A non-UTF-8 audit log → count-0 (empty counts) + a warning, not a
    crash. The old bare ``except OSError`` missed UnicodeDecodeError; the
    Operations section is called BARE by the daemon (daemon.py), so it killed
    the whole brief. Mutation: revert to ``read_text`` inside ``except
    OSError`` → this raises."""
    from alfred.brief.operations import _count_audit_log

    audit = tmp_path / "vault_audit.log"
    audit.write_bytes(b"\xff\xfe not utf-8 audit line\n")
    with structlog.testing.capture_logs() as cap:
        counts = _count_audit_log(audit, since="2026-01-01")
    assert counts == {}
    warns = [c for c in cap if c.get("event") == "operations.audit_read_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "UnicodeDecodeError"


def test_operations_count_audit_happy_path(tmp_path) -> None:
    """Sanity: a clean audit log still counts by tool/op since the given
    date prefix (the migration must not change the happy path)."""
    from alfred.brief.operations import _count_audit_log

    audit = tmp_path / "vault_audit.log"
    audit.write_text(
        '{"ts": "2026-07-19T06:00:00", "tool": "curator", "op": "create"}\n'
        '{"ts": "2026-07-19T06:01:00", "tool": "curator", "op": "create"}\n'
        '{"ts": "2025-01-01T00:00:00", "tool": "janitor", "op": "edit"}\n',
        encoding="utf-8",
    )
    counts = _count_audit_log(audit, since="2026-07-19")
    assert counts["curator"]["create"] == 2
    assert "janitor" not in counts  # before ``since`` → excluded


def test_operations_read_json_non_utf8_degrades(tmp_path) -> None:
    """A non-UTF-8 state file → {} (empty), not a crash. The old
    ``(json.JSONDecodeError, OSError)`` catch missed UnicodeDecodeError (a
    SIBLING of JSONDecodeError under ValueError, not a subclass). Mutation:
    revert to that two-tuple catch → this raises."""
    from alfred.brief.operations import _read_json

    p = tmp_path / "curator_state.json"
    p.write_bytes(b"\xff\xfe not utf-8")
    assert _read_json(p) == {}


def test_operations_read_json_bad_json_degrades(tmp_path) -> None:
    """A clean-read but non-JSON file → {} (the json.loads catch preserved
    exactly from the pre-migration behavior)."""
    from alfred.brief.operations import _read_json

    p = tmp_path / "curator_state.json"
    p.write_text("{ not json", encoding="utf-8")
    assert _read_json(p) == {}


def test_operations_section_survives_non_utf8_audit_and_state(tmp_path) -> None:
    """End-to-end: a corrupt (non-UTF-8) audit log AND a corrupt state file
    must not crash the whole Operations section — it's rendered BARE by the
    daemon (daemon.py:167). It renders with degraded content instead."""
    from alfred.brief.operations import format_operations_section

    data = tmp_path / "data"
    data.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (data / "vault_audit.log").write_bytes(b"\xff\xfe corrupt")
    (data / "curator_state.json").write_bytes(b"\xff\xfe corrupt")
    out = format_operations_section(str(data), str(vault), since="2026-07-19")
    assert isinstance(out, str)
    assert "Curator" in out  # tool-activity table still renders
