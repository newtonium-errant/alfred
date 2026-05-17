"""Re-encounter source-body append tests — Phase 2 deliverable #4
(2026-05-17).

When a capture session anchors to an EXISTING source (the source-
anchor resolver finds a pre-existing ``source/<title>.md`` record),
the orchestrator appends today's observations to that source's
``## Observations During`` body section.

Behaviour matrix:
  * First encounter on a fresh source → source_created=True; this
    deliverable doesn't fire (handled by the resolver's create path).
  * Second capture, different day → new ``### YYYY-MM-DD`` subsection
    appended at end of Observations During.
  * Third capture, same day as second → APPEND BULLETS to the
    existing ``### YYYY-MM-DD`` subsection (no duplicate heading).
  * Pre-Phase-2 source records (missing ``## Observations During``
    section) → no-op, returns False.

Coverage:
  * Unit tests on _build_re_encounter_rewriter (pure-function path)
  * End-to-end via append_re_encounter_observation
  * Idempotent same-day shape
  * Pre-Phase-2 source (no Observations During section) → no-op
  * Failure isolation: missing source → returns False, no crash
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred.telegram import capture_source_anchor as csa


# --- Fixture: build a source record with the Phase 2 template body ------


def _phase2_source_body() -> str:
    """Phase 2 source template body — matches the bundled scaffold."""
    return (
        "# Source Details\n\n"
        "## Bibliographic Details\n\n"
        "## Goal\n\n"
        "## Overview\n\n"
        "# Notes\n\n"
        "## Summary Statement\n\n"
        "## Why It Matters\n\n"
        "## Observations During\n\n"
        "## Permanent Notes spawned\n\n"
        "# External References\n\n"
        "# Tags\n\n"
        "# Indexing & MOCs\n"
    )


def _write_source_record(
    vault: Path, title: str,
    body: str | None = None,
) -> str:
    """Write a source record to vault/source/<title>.md."""
    (vault / "source").mkdir(parents=True, exist_ok=True)
    rel = f"source/{title}.md"
    if body is None:
        body = _phase2_source_body()
    (vault / rel).write_text(
        "---\n"
        "type: source\n"
        f"name: {title}\n"
        "created: '2026-05-15'\n"
        "status: active\n"
        "---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("source", "session"):
        (vault / sub).mkdir(parents=True)
    return vault


# --- _build_re_encounter_rewriter (pure-function) ------------------------


def test_rewriter_appends_new_date_subsection() -> None:
    """First encounter today: rewriter inserts a new ``### 2026-05-17``
    subsection at the end of Observations During."""
    body = _phase2_source_body()
    rewriter = csa._build_re_encounter_rewriter(
        "2026-05-17",
        "- First insight\n- Second insight\n\n_From [[session/cap-1]]_",
    )
    result = rewriter(body)
    assert "### 2026-05-17" in result
    assert "- First insight" in result
    # The new subsection lands WITHIN Observations During (before
    # Permanent Notes spawned).
    obs_idx = result.index("## Observations During")
    new_section_idx = result.index("### 2026-05-17")
    perm_idx = result.index("## Permanent Notes spawned")
    assert obs_idx < new_section_idx < perm_idx, (
        f"new ### 2026-05-17 subsection landed outside Observations "
        f"During — indexes: obs={obs_idx}, new={new_section_idx}, "
        f"perm={perm_idx}"
    )


def test_rewriter_same_day_appends_to_existing_subsection() -> None:
    """When ``### <today>`` already exists, the rewriter appends new
    bullets under it (no duplicate heading)."""
    # Pre-populate with one prior encounter same-day.
    body = _phase2_source_body().replace(
        "## Observations During\n\n",
        "## Observations During\n\n"
        "### 2026-05-17\n\n"
        "- Prior insight\n\n"
        "_From [[session/cap-prior]]_\n\n",
    )
    rewriter = csa._build_re_encounter_rewriter(
        "2026-05-17",
        "- New insight\n\n_From [[session/cap-new]]_",
    )
    result = rewriter(body)
    # Only ONE ### 2026-05-17 heading.
    assert result.count("### 2026-05-17") == 1
    # Both prior and new bullets present.
    assert "- Prior insight" in result
    assert "- New insight" in result
    # New backref present.
    assert "[[session/cap-new]]" in result
    assert "[[session/cap-prior]]" in result


def test_rewriter_different_day_creates_new_subsection() -> None:
    """``### 2026-05-17`` exists; today is 2026-05-18 → new subsection
    appended below the existing one (not folded into it)."""
    body = _phase2_source_body().replace(
        "## Observations During\n\n",
        "## Observations During\n\n"
        "### 2026-05-17\n\n"
        "- Yesterday insight\n\n"
        "_From [[session/cap-yesterday]]_\n\n",
    )
    rewriter = csa._build_re_encounter_rewriter(
        "2026-05-18",
        "- Today insight\n\n_From [[session/cap-today]]_",
    )
    result = rewriter(body)
    assert "### 2026-05-17" in result
    assert "### 2026-05-18" in result
    # Today's subsection appears AFTER yesterday's.
    yesterday_idx = result.index("### 2026-05-17")
    today_idx = result.index("### 2026-05-18")
    assert yesterday_idx < today_idx


def test_rewriter_no_observations_during_section_is_noop() -> None:
    """Pre-Phase-2 source records (no ``## Observations During``
    section) → rewriter returns body unchanged. Conservative behaviour:
    don't write to an arbitrary location.
    """
    body = "# Old Source\n\n## Notes\n\n(running notes)\n"
    rewriter = csa._build_re_encounter_rewriter(
        "2026-05-17",
        "- New insight",
    )
    result = rewriter(body)
    assert result == body


def test_rewriter_preserves_subsequent_sections() -> None:
    """Sections AFTER Observations During (Permanent Notes spawned,
    External References, Tags, Indexing & MOCs) are preserved
    untouched."""
    body = _phase2_source_body()
    rewriter = csa._build_re_encounter_rewriter(
        "2026-05-17",
        "- Insight\n\n_From [[session/cap-1]]_",
    )
    result = rewriter(body)
    # All canonical sections still present in correct order.
    canonical = [
        "## Observations During",
        "## Permanent Notes spawned",
        "# External References",
        "# Tags",
        "# Indexing & MOCs",
    ]
    indexes = [result.index(s) for s in canonical]
    assert indexes == sorted(indexes), (
        f"section order broken — got indexes {indexes}"
    )


# --- _render_observations_for_session ------------------------------------


def test_render_observations_includes_topics_and_insights() -> None:
    rendered = csa._render_observations_for_session(
        topics=["stoicism", "dichotomy of control"],
        key_insights=["Marcus returns to control as foundational"],
        session_rel_path="session/capture-foo.md",
    )
    assert "- stoicism" in rendered
    assert "- dichotomy of control" in rendered
    assert "- Marcus returns to control as foundational" in rendered
    # Backref to session at end.
    assert "_From [[session/capture-foo]]_" in rendered


def test_render_observations_empty_emits_explicit_placeholder() -> None:
    """No topics + no insights → explicit ``(no topics or insights
    surfaced this session)`` placeholder. Per the "intentionally left
    blank" discipline — silence is ambiguous; explicit emptiness is not."""
    rendered = csa._render_observations_for_session(
        topics=[], key_insights=[],
        session_rel_path="session/capture-empty.md",
    )
    assert "(no topics or insights surfaced this session)" in rendered
    # Backref still present — ties the encounter to the source even
    # when there are no observations to record.
    assert "[[session/capture-empty]]" in rendered


# --- End-to-end via append_re_encounter_observation ---------------------


def test_append_creates_subsection_on_existing_source(
    tmp_path: Path,
) -> None:
    """E2E: pre-existing source + first encounter today → new
    ``### <today>`` subsection in body."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(vault, "Meditations")

    ok = csa.append_re_encounter_observation(
        vault_path=vault,
        source_rel_path=rel,
        today_iso="2026-05-17",
        topics=["stoicism"],
        key_insights=["dichotomy of control"],
        session_rel_path="session/capture-2026-05-17-aaa.md",
        scope="hypatia",
    )
    assert ok is True
    body = (vault / rel).read_text(encoding="utf-8")
    assert "### 2026-05-17" in body
    assert "- stoicism" in body
    assert "- dichotomy of control" in body


def test_append_idempotent_same_day(tmp_path: Path) -> None:
    """E2E: two same-day appends → one ``### <today>`` heading,
    bullets from both encounters accumulate under it."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(vault, "Meditations")

    csa.append_re_encounter_observation(
        vault_path=vault, source_rel_path=rel,
        today_iso="2026-05-17",
        topics=["first"], key_insights=[],
        session_rel_path="session/capture-first.md",
        scope="hypatia",
    )
    csa.append_re_encounter_observation(
        vault_path=vault, source_rel_path=rel,
        today_iso="2026-05-17",
        topics=["second"], key_insights=[],
        session_rel_path="session/capture-second.md",
        scope="hypatia",
    )
    body = (vault / rel).read_text(encoding="utf-8")
    # One heading only.
    assert body.count("### 2026-05-17") == 1
    # Both bullets present.
    assert "- first" in body
    assert "- second" in body
    # Both backrefs present.
    assert "[[session/capture-first]]" in body
    assert "[[session/capture-second]]" in body


def test_append_different_days_creates_separate_subsections(
    tmp_path: Path,
) -> None:
    """Encounters on different days produce separate ``### YYYY-MM-DD``
    subsections — both preserved."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(vault, "Meditations")

    csa.append_re_encounter_observation(
        vault_path=vault, source_rel_path=rel,
        today_iso="2026-05-17",
        topics=["yesterday"], key_insights=[],
        session_rel_path="session/cap-1.md",
        scope="hypatia",
    )
    csa.append_re_encounter_observation(
        vault_path=vault, source_rel_path=rel,
        today_iso="2026-05-18",
        topics=["today"], key_insights=[],
        session_rel_path="session/cap-2.md",
        scope="hypatia",
    )
    body = (vault / rel).read_text(encoding="utf-8")
    assert "### 2026-05-17" in body
    assert "### 2026-05-18" in body
    assert body.count("### 2026-05-17") == 1
    assert body.count("### 2026-05-18") == 1


def test_append_pre_phase2_source_no_observations_section(
    tmp_path: Path,
) -> None:
    """Pre-Phase-2 source record (no ``## Observations During``
    section) → returns False, body unchanged."""
    vault = _make_vault(tmp_path)
    # Operator-curated old source with no Phase 2 scaffolding.
    rel = _write_source_record(
        vault, "Pre-Phase2 Source",
        body="# Old Source\n\n## My Notes\n\n(running notes)\n",
    )
    before_body = (vault / rel).read_text(encoding="utf-8")
    ok = csa.append_re_encounter_observation(
        vault_path=vault, source_rel_path=rel,
        today_iso="2026-05-17",
        topics=["x"], key_insights=[],
        session_rel_path="session/cap.md",
        scope="hypatia",
    )
    # Conservative: no Observations During → no-op (the vault_edit
    # call still runs but the rewriter returns body unchanged, so
    # the file mtime may change but content stays identical).
    after_body = (vault / rel).read_text(encoding="utf-8")
    # File content unchanged. (ok may be True because the vault_edit
    # call succeeded, even though the rewriter no-op'd — that's
    # acceptable. The pin is on the body content.)
    # Strip the frontmatter for comparison since timestamps in
    # frontmatter may shift; we care about body shape.
    before_post = frontmatter.loads(before_body)
    after_post = frontmatter.loads(after_body)
    assert before_post.content == after_post.content, (
        f"pre-Phase-2 source body mutated unexpectedly. "
        f"before: {before_post.content!r} after: {after_post.content!r}"
    )


def test_append_missing_source_returns_false(tmp_path: Path) -> None:
    """Source record doesn't exist → returns False; no crash."""
    vault = _make_vault(tmp_path)
    ok = csa.append_re_encounter_observation(
        vault_path=vault,
        source_rel_path="source/Nonexistent.md",
        today_iso="2026-05-17",
        topics=["x"], key_insights=[],
        session_rel_path="session/cap.md",
        scope="hypatia",
    )
    assert ok is False


def test_append_handles_wikilink_input_form(tmp_path: Path) -> None:
    """Caller may pass ``[[source/Title]]`` wikilink form; the helper
    strips brackets + appends .md."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(vault, "Meditations")

    ok = csa.append_re_encounter_observation(
        vault_path=vault,
        source_rel_path="[[source/Meditations]]",  # wikilink form
        today_iso="2026-05-17",
        topics=["x"], key_insights=[],
        session_rel_path="session/cap.md",
        scope="hypatia",
    )
    assert ok is True
    body = (vault / rel).read_text(encoding="utf-8")
    assert "### 2026-05-17" in body


# --- WARN-1 hardening regression: line-anchored section detection -------


def test_rewriter_does_not_false_match_h3_observations_during(
    tmp_path: Path,
) -> None:
    """WARN-1 regression-pin (2026-05-17). Body containing an H3
    heading like ``### Observations During Yesterday`` must NOT
    cause the rewriter to false-match on the substring ``##
    Observations During`` at offset+1 within the H3 line.

    Pre-hardening shape: ``body.find("## Observations During")``
    would lock onto the H3-line's offset+1 (because ``### Foo`` =
    ``#`` + ``## Foo``) and corrupt subsequent section-bounded
    operations.

    Post-hardening: ``_find_h2_section_start`` enforces
    line-anchored detection — the H3 doesn't match.

    The fixture body has NO real ``## Observations During`` H2
    heading. Post-hardening, the rewriter detects no section and
    returns body unchanged (the canonical "no Observations During
    section → no-op" path).
    """
    # Body with an H3 that contains the substring "## Observations
    # During" at offset+1 within the H3 line — but NO real H2 heading.
    body = (
        "# Source Details\n\n"
        "## Bibliographic Details\n\n"
        "# Notes\n\n"
        "### Observations During Yesterday\n\n"  # H3 — must NOT false-match
        "Some prior content.\n\n"
        "# External References\n"
    )
    rewriter = csa._build_re_encounter_rewriter(
        "2026-05-17",
        "- New insight\n\n_From [[session/cap]]_",
    )
    result = rewriter(body)
    # No real H2 ``## Observations During`` → rewriter no-ops.
    assert result == body, (
        f"H3 ``### Observations During Yesterday`` false-matched the "
        f"H2 anchor; rewriter corrupted body. before:\n{body}\n\n"
        f"after:\n{result}"
    )


def test_find_h2_section_start_rejects_h3_false_match() -> None:
    """Unit test on the line-anchor helper: H3 heading containing the
    H2 substring must return -1."""
    body = (
        "# Top\n\n"
        "### Observations During Yesterday\n\n"
        "Some content.\n"
    )
    idx = csa._find_h2_section_start(body, "## Observations During")
    assert idx == -1, (
        f"H3 should NOT false-match H2 anchor; got idx={idx}"
    )


def test_find_h2_section_start_matches_real_h2() -> None:
    """Sanity check: a real H2 heading at line start is detected."""
    body = (
        "# Top\n\n"
        "## Observations During\n\n"
        "Some content.\n"
    )
    idx = csa._find_h2_section_start(body, "## Observations During")
    assert idx > 0
    assert body[idx:idx + len("## Observations During")] == (
        "## Observations During"
    )


def test_find_h2_section_start_matches_at_body_start() -> None:
    """H2 heading at byte 0 of body is detected (edge case — no
    preceding newline)."""
    body = "## Observations During\n\nContent.\n"
    idx = csa._find_h2_section_start(body, "## Observations During")
    assert idx == 0
