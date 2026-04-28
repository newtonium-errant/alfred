"""Distiller body-content-hash gate.

The skip-distill gate hashes the body only (frontmatter stripped, trailing
whitespace normalized) so cosmetic frontmatter rewrites by janitor's
``deep_sweep_fix`` mode and surveyor's ``alfred_tags`` writer don't
re-trigger LLM extraction. Body changes still re-trigger correctly —
LINK001 wikilink repair and STUB001 enrichment legitimately shift the
source's claim wording, so re-extraction is desired in those cases.

Migration: legacy state with empty ``body_hash`` re-extracts once on
first encounter to populate the field, then hash-gates from then on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.distiller.candidates import scan_candidates
from alfred.distiller.state import DistillerState, FileState
from alfred.distiller.utils import compute_body_hash


# --- compute_body_hash ----------------------------------------------------


def test_body_hash_unchanged_when_only_frontmatter_changes() -> None:
    """Frontmatter-only edits (alfred_tags, attribution_audit) preserve hash.

    The whole point of the body-hash gate: surveyor / janitor cosmetic
    writes that touch only frontmatter must NOT cause re-extraction.
    """
    body = "## Outcome\n\nDecided to use sha256 for body hashing.\n"
    text_a = f"---\ntype: session\ntitle: Foo\n---\n{body}"
    text_b = f"---\ntype: session\ntitle: Foo\nalfred_tags: [autotagged]\n---\n{body}"

    assert compute_body_hash(text_a) == compute_body_hash(text_b)


def test_body_hash_changes_when_body_changes() -> None:
    """STUB001 enrichment / LINK001 wikilink repair shifts body bytes."""
    fm = "---\ntype: session\ntitle: Foo\n---\n"
    text_a = fm + "## Outcome\n\nDecided to use sha256.\n"
    text_b = fm + "## Outcome\n\nDecided to use sha256 with [[person/Andrew]] approval.\n"

    assert compute_body_hash(text_a) != compute_body_hash(text_b)


def test_body_hash_normalizes_trailing_whitespace() -> None:
    """A trailing newline or whitespace difference alone won't re-trigger."""
    fm = "---\ntype: session\n---\n"
    text_a = fm + "Body content."
    text_b = fm + "Body content.\n"
    text_c = fm + "Body content.   \n\n"

    h = compute_body_hash(text_a)
    assert compute_body_hash(text_b) == h
    assert compute_body_hash(text_c) == h


def test_body_hash_handles_no_frontmatter() -> None:
    """Documents without frontmatter are hashed in full."""
    text = "Just a plain markdown body, no frontmatter.\n"
    # Should not raise; should produce a stable hex digest of the rstripped body.
    h = compute_body_hash(text)
    assert len(h) == 64  # sha256 hex digest


def test_body_hash_handles_unclosed_frontmatter() -> None:
    """Malformed frontmatter (no closing ``---``) → treat whole text as body.

    Skip the gate on a malformed file rather than crashing or silently
    skipping extraction. The full text becomes the body hash; a later
    repair will change it and re-trigger extraction.
    """
    text = "---\ntype: session\nbody starts immediately, no closing fence\n"
    h = compute_body_hash(text)
    assert len(h) == 64


# --- scan_candidates body-hash gate ---------------------------------------


def _write_session(vault: Path, name: str, fm_extra: str, body: str) -> None:
    """Helper: write a session record with a stable scoring footprint.

    Body must include enough decision/outcome content to clear the default
    candidate threshold (0.4) — otherwise the scanner skips it on score
    rather than on the gate, which doesn't exercise what we're testing.
    """
    sessions = vault / "session"
    sessions.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        "type: session\n"
        f"title: {name}\n"
        f"{fm_extra}"
        "---\n"
    )
    (sessions / f"{name}.md").write_text(fm + body, encoding="utf-8")


_RICH_BODY = (
    "## Context\n\n"
    "We were comparing options.\n\n"
    "## Outcome\n\n"
    "Decided to ship sha256 for body hashing. "
    "We agreed it was the right call. The team approved the change "
    "after we confirmed the constraint on hash collisions.\n"
)


def test_scan_skips_when_body_hash_unchanged(tmp_path: Path) -> None:
    """Stored body_hash matches current body → candidate is skipped."""
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)

    full_text = (vault / "session" / "Foo.md").read_text(encoding="utf-8")
    body_hash = compute_body_hash(full_text)

    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash=body_hash, last_distilled="2026-04-25T00:00:00Z",
    )

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
    )

    assert candidates == []


def test_scan_skips_when_only_frontmatter_changed(tmp_path: Path) -> None:
    """Stored body_hash from before a frontmatter-only rewrite still matches."""
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)

    # Capture body hash BEFORE the cosmetic frontmatter write.
    full_before = (vault / "session" / "Foo.md").read_text(encoding="utf-8")
    body_hash_before = compute_body_hash(full_before)

    # Simulate janitor / surveyor: append alfred_tags + attribution_audit
    # into frontmatter without touching the body.
    _write_session(
        vault, "Foo",
        fm_extra="alfred_tags: [decided]\nattribution_audit: [janitor:LINK001]\n",
        body=_RICH_BODY,
    )

    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash=body_hash_before,
        last_distilled="2026-04-25T00:00:00Z",
    )

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
    )

    assert candidates == []


def test_scan_re_extracts_when_body_changed(tmp_path: Path) -> None:
    """Body content actually shifted → candidate re-qualifies."""
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)
    body_hash_before = compute_body_hash(
        (vault / "session" / "Foo.md").read_text(encoding="utf-8")
    )

    # STUB001 enrichment: rewrite the body with an added wikilink.
    new_body = _RICH_BODY + "\nLater: [[person/Andrew]] confirmed.\n"
    _write_session(vault, "Foo", fm_extra="", body=new_body)

    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash=body_hash_before,
        last_distilled="2026-04-25T00:00:00Z",
    )

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
    )

    assert len(candidates) == 1
    assert candidates[0].record.rel_path == "session/Foo.md"
    # The fresh hash must be carried on the candidate so the post-extract
    # update_file call records it (otherwise the next scan re-extracts).
    assert candidates[0].body_hash != body_hash_before


def test_scan_re_extracts_legacy_state_with_empty_body_hash(tmp_path: Path) -> None:
    """State pre-dating body_hash field → re-extract once to populate."""
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)

    state = DistillerState(tmp_path / "state.json")
    # Legacy entry: md5 populated but body_hash empty.
    state.files["session/Foo.md"] = FileState(
        md5="legacy-md5", body_hash="",
        last_distilled="2026-04-20T00:00:00Z",
    )

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
    )

    # Empty body_hash treated as unknown → re-extract on first encounter.
    assert len(candidates) == 1
    assert candidates[0].record.rel_path == "session/Foo.md"


# --- DistillerState load tolerates legacy fields --------------------------


def test_state_load_tolerates_unknown_legacy_fields(tmp_path: Path) -> None:
    """An older state file with extra fields (e.g. ``last_scanned``) loads cleanly.

    Forward/backward compat: state.load() filters file entries to known
    dataclass fields so adding/removing schema fields never crashes a
    daemon reading a mismatched-version state file.
    """
    import json
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({
            "version": 1,
            "files": {
                "session/Old.md": {
                    "md5": "abc",
                    "last_distilled": "2026-04-01T00:00:00Z",
                    "learn_records_created": [],
                    "last_scanned": "2026-04-01T00:00:00Z",  # unknown legacy field
                    "frobnicated": True,                     # another unknown
                },
            },
        }),
        encoding="utf-8",
    )

    state = DistillerState(state_path)
    state.load()  # must not raise

    assert "session/Old.md" in state.files
    assert state.files["session/Old.md"].md5 == "abc"
    # body_hash defaults to empty for legacy entries → re-extract on next scan.
    assert state.files["session/Old.md"].body_hash == ""


def test_state_save_load_roundtrip_with_body_hash(tmp_path: Path) -> None:
    """body_hash survives save/load cycle."""
    state_path = tmp_path / "state.json"
    state = DistillerState(state_path)
    state.update_file(
        "session/Foo.md",
        md5="m5",
        learn_records=["decision/Use sha256.md"],
        body_hash="bh-1",
    )
    state.save()

    reloaded = DistillerState(state_path)
    reloaded.load()
    assert reloaded.files["session/Foo.md"].body_hash == "bh-1"


def test_should_distill_uses_body_hash(tmp_path: Path) -> None:
    """Contract check on the helper: stored hash matches → False; differs → True."""
    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash="bh-1",
    )

    assert state.should_distill("session/Foo.md", "bh-1") is False
    assert state.should_distill("session/Foo.md", "bh-2") is True
    # Unknown file → distill (new content)
    assert state.should_distill("session/New.md", "anything") is True


def test_should_distill_legacy_empty_hash_re_extracts(tmp_path: Path) -> None:
    """Empty stored body_hash → return True so we populate on first encounter."""
    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(md5="ignored", body_hash="")
    assert state.should_distill("session/Foo.md", "any-hash") is True


# --- drift_skip observability --------------------------------------------
#
# Per ``project_distiller_drift_mitigation.md``, the body-hash gate must
# emit an explicit "drift skip" log line every time a cosmetic frontmatter
# rewrite (janitor deep_sweep_fix, surveyor alfred_tags) would otherwise
# have triggered re-extraction. The signal feeds the Option 3 escalation
# decision — without it, a silent skip leaves us unable to distinguish
# "drift gate working" from "no drift happening."
#
# These tests use the file-handler logging pattern from
# ``test_surveyor_logging.py``: structlog routes through stdlib logging
# with cache_logger_on_first_use=True, which doesn't reliably propagate
# to pytest's ``caplog`` but does land in a configured FileHandler.


import logging as _logging
import os as _os
import time as _time

import structlog as _structlog

from alfred.distiller.utils import setup_logging as _setup_logging


@pytest.fixture
def drift_log_file(tmp_path: Path):
    """Configure distiller logging to write to a temp file; yield the path.

    Resets structlog + stdlib logging on teardown so subsequent tests
    don't inherit the file handler. Mirrors the ``_reset_logging``
    pattern in ``test_surveyor_logging.py`` (the silent-writer
    regression suite).
    """
    log_path = tmp_path / "distiller.log"
    _setup_logging(level="INFO", log_file=str(log_path), suppress_stdout=True)
    yield log_path
    _structlog.reset_defaults()
    root = _logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def _read_drift_skip_lines(log_path: Path) -> list[str]:
    """Flush handlers and return every line emitting ``candidates.drift_skip``.

    Match on the exact event name to avoid false positives from the
    ``candidates.scanned`` summary line which carries a ``drift_skips=N``
    counter field but is not itself a drift_skip event.
    """
    for h in _logging.getLogger().handlers:
        h.flush()
    if not log_path.exists():
        return []
    return [
        line
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if "candidates.drift_skip " in line or "candidates.drift_skip\t" in line
    ]


def test_drift_skip_logged_when_mtime_bumped_but_body_unchanged(
    tmp_path: Path,
    drift_log_file: Path,
) -> None:
    """Frontmatter-only rewrite bumps mtime → drift_skip log + no candidate.

    The core observability case. Stored last_distilled is older than the
    file's current mtime (simulating a janitor structural fix landing
    after the previous distillation), and the body bytes still match.
    Scanner should skip AND emit ``candidates.drift_skip``.
    """
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)

    full_text = (vault / "session" / "Foo.md").read_text(encoding="utf-8")
    body_hash = compute_body_hash(full_text)

    # Backdate last_distilled to a known-past timestamp so any current
    # filesystem mtime is "after" without test-clock dependence.
    last_distilled = "2026-04-25T00:00:00+00:00"
    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash=body_hash, last_distilled=last_distilled,
    )

    # Force the file's mtime to "now" (well past last_distilled).
    target = vault / "session" / "Foo.md"
    now = _time.time()
    _os.utime(target, (now, now))

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
        distilled_last_distilled=state.get_distilled_last_distilled(),
    )

    assert candidates == []
    drift_lines = _read_drift_skip_lines(drift_log_file)
    assert len(drift_lines) == 1, f"expected one drift_skip line; got: {drift_lines}"
    line = drift_lines[0]
    # Required fields per the ticket: source path, last_distilled, file_mtime,
    # and the body_hash_unchanged marker.
    assert "session/Foo.md" in line
    assert "last_distilled" in line
    assert "file_mtime" in line
    assert "body_hash_unchanged" in line


def test_drift_skip_not_logged_when_body_changed(
    tmp_path: Path,
    drift_log_file: Path,
) -> None:
    """Body actually shifted → no drift_skip (the file goes through to extract).

    Regression guard: the drift_skip log is the body-hash-MATCHED path. A
    body-changed file must reach the candidate path, not produce a
    spurious drift_skip.
    """
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)
    body_hash_before = compute_body_hash(
        (vault / "session" / "Foo.md").read_text(encoding="utf-8")
    )
    new_body = _RICH_BODY + "\nLater: [[person/Andrew]] confirmed.\n"
    _write_session(vault, "Foo", fm_extra="", body=new_body)

    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash=body_hash_before,
        last_distilled="2026-04-25T00:00:00+00:00",
    )

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
        distilled_last_distilled=state.get_distilled_last_distilled(),
    )

    assert len(candidates) == 1
    assert _read_drift_skip_lines(drift_log_file) == []


def test_drift_skip_not_logged_for_legacy_state_without_last_distilled(
    tmp_path: Path,
    drift_log_file: Path,
) -> None:
    """Legacy state with body_hash but no last_distilled → no drift_skip noise.

    ``get_distilled_last_distilled`` filters to entries that have a
    last_distilled timestamp, so legacy hash-only entries simply don't
    appear in the sidecar. The scanner falls through to its silent skip
    on body-hash match — same behavior as before this ticket. Reason:
    we don't want to fire drift_skip on the first scan after migration
    when last_distilled is empty (would be misleading — there's no
    "drift" yet, just absence of bookkeeping).
    """
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)
    body_hash = compute_body_hash(
        (vault / "session" / "Foo.md").read_text(encoding="utf-8")
    )

    state = DistillerState(tmp_path / "state.json")
    # Body hash present, but last_distilled empty (legacy).
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash=body_hash, last_distilled="",
    )

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
        distilled_last_distilled=state.get_distilled_last_distilled(),
    )

    assert candidates == []
    assert _read_drift_skip_lines(drift_log_file) == []


def test_get_distilled_last_distilled_filters_empty_fields(tmp_path: Path) -> None:
    """Sidecar dict only includes entries with both body_hash AND last_distilled.

    Contract for the call sites: pairing the dict with body_hashes ensures
    we never look up a last_distilled for a key that isn't gated.
    """
    state = DistillerState(tmp_path / "state.json")
    state.files["session/A.md"] = FileState(
        md5="m", body_hash="bh-a", last_distilled="2026-04-25T00:00:00+00:00",
    )
    state.files["session/B.md"] = FileState(
        md5="m", body_hash="bh-b", last_distilled="",  # legacy: no timestamp
    )
    state.files["session/C.md"] = FileState(
        md5="m", body_hash="", last_distilled="2026-04-25T00:00:00+00:00",  # legacy: no hash
    )

    sidecar = state.get_distilled_last_distilled()
    assert sidecar == {"session/A.md": "2026-04-25T00:00:00+00:00"}


def test_drift_skip_omitted_when_sidecar_not_supplied(
    tmp_path: Path,
    drift_log_file: Path,
) -> None:
    """Backward-compat: callers that don't pass the sidecar get silent skips.

    Existing tests / callers that only pass ``distilled_files`` continue
    to work exactly as before — no drift_skip log fires (we have nothing
    to compare against), but the body-hash gate still skips correctly.
    """
    vault = tmp_path / "vault"
    _write_session(vault, "Foo", fm_extra="", body=_RICH_BODY)
    body_hash = compute_body_hash(
        (vault / "session" / "Foo.md").read_text(encoding="utf-8")
    )

    state = DistillerState(tmp_path / "state.json")
    state.files["session/Foo.md"] = FileState(
        md5="ignored", body_hash=body_hash,
        last_distilled="2026-04-25T00:00:00+00:00",
    )

    candidates = scan_candidates(
        vault_path=vault,
        ignore_dirs=[],
        ignore_files=[],
        source_types=["session"],
        threshold=0.4,
        distilled_files=state.get_distilled_body_hashes(),
        # distilled_last_distilled NOT passed
    )

    assert candidates == []
    assert _read_drift_skip_lines(drift_log_file) == []
