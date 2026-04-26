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
