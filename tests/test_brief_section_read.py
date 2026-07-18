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
