"""Tests for janitor's SUPERSEDED-marker sweep.

The sweep walks the vault for ``<!-- SUPERSEDES: inf-XXX -->``
references inside correction notes, and back-annotates the
referenced ``BEGIN_INFERRED`` block with a matching SUPERSEDED
marker — but ONLY when the correction note is LLM-attributed.
User-attributed corrections were fixed in-place per the
``feedback_correction_attribution_pattern.md`` rule, so they
don't get marked.

Coverage:
    * Salem-attributed correction → marker added
    * User-attributed correction → no marker added
    * Ambiguous correction (no attribution language) → warning
      logged, no marker added
    * Orphaned reference (inf-XXX missing) → counted, no crash
    * Idempotence — second run is a no-op
    * Multiple corrections for distinct blocks in the same record
    * The ignore-dirs filter is honored
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.superseded_marker import (
    run_superseded_marker_sweep,
)


def _config_for(vault: Path) -> JanitorConfig:
    """Minimal JanitorConfig pointed at ``vault`` for sweep tests."""
    return JanitorConfig(
        vault=VaultConfig(path=str(vault)),
        sweep=SweepConfig(),
        state=StateConfig(path=str(vault.parent / "janitor_state.json")),
    )


def _write(vault: Path, rel: str, content: str) -> Path:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _record_with_inferred_block(
    *, inf_id: str, name: str, correction: str
) -> str:
    """Compose a vault record with a BEGIN_INFERRED block + a trailing
    correction note that may include a SUPERSEDES reference.

    The block is laid out the way ``alfred.vault.attribution`` writes it:
    BEGIN/END comments straddling the inferred prose, and an
    ``attribution_audit`` entry in frontmatter with the matching ID.
    """
    body = dedent(
        f"""\
        ---
        type: person
        name: {name}
        created: '2026-04-26'
        attribution_audit:
        - agent: salem
          confirmed_by_andrew: false
          date: '2026-04-26T08:48:55+00:00'
          marker_id: {inf_id}
          reason: conversation turn
          section_title: Background
        ---

        # {name}

        ## Background

        <!-- BEGIN_INFERRED marker_id="{inf_id}" -->
        Hussein Rafih is the landlord of the Greenwood building, contact for the
        Wayne Fowler property.
        <!-- END_INFERRED marker_id="{inf_id}" -->

        {correction}
        """
    )
    return body


# --- Salem-attributed → marker added --------------------------------------


def test_llm_attributed_correction_adds_marker(tmp_vault: Path):
    inf_id = "inf-20260426-salem-c5f6f8"
    correction = (
        "<!-- correction 2026-04-27: Mis-inference was Salem's. Hussein Rafih is "
        f"the New Minas landlord, separate from Wayne Fowler. <!-- SUPERSEDES: {inf_id} --> -->"
    )
    rec = _write(
        tmp_vault,
        "person/Hussein Rafih.md",
        _record_with_inferred_block(
            inf_id=inf_id,
            name="Hussein Rafih",
            correction=correction,
        ),
    )

    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)

    assert result.marked == 1, result.summary_line()
    assert result.skipped_user_attributed == 0
    assert result.skipped_ambiguous == 0
    assert result.orphaned == 0
    assert result.errors == []

    text = rec.read_text(encoding="utf-8")
    # SUPERSEDED line lands immediately after BEGIN_INFERRED.
    lines = text.splitlines()
    begin_idx = next(
        i for i, line in enumerate(lines)
        if "BEGIN_INFERRED" in line and inf_id in line
    )
    assert begin_idx + 1 < len(lines)
    next_line = lines[begin_idx + 1]
    assert "<!-- SUPERSEDED: see correction-" in next_line
    # The date stamp from the correction line should be in the id.
    assert "correction-2026-04-27-" in next_line


# --- User-attributed → no marker ------------------------------------------


def test_user_attributed_correction_skipped(tmp_vault: Path):
    inf_id = "inf-20260426-salem-aaaaaa"
    correction = (
        "<!-- correction 2026-04-27: The error was Andrew's — Salem recorded "
        f"accurately. Andrew gave wrong info initially. <!-- SUPERSEDES: {inf_id} --> -->"
    )
    rec = _write(
        tmp_vault,
        "person/Test User Attributed.md",
        _record_with_inferred_block(
            inf_id=inf_id,
            name="Test User Attributed",
            correction=correction,
        ),
    )

    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)

    assert result.marked == 0, result.summary_line()
    assert result.skipped_user_attributed == 1
    assert result.orphaned == 0
    assert result.errors == []

    # Verify no SUPERSEDED line was inserted.
    text = rec.read_text(encoding="utf-8")
    assert "<!-- SUPERSEDED:" not in text


# --- Ambiguous (no attribution language) → warn, skip ---------------------


def test_ambiguous_correction_skipped(tmp_vault: Path, caplog):
    inf_id = "inf-20260426-salem-bbbbbb"
    correction = (
        f"<!-- correction 2026-04-27: see updated info below. <!-- SUPERSEDES: {inf_id} --> -->\n"
        "The actual landlord is someone else."
    )
    rec = _write(
        tmp_vault,
        "person/Test Ambiguous.md",
        _record_with_inferred_block(
            inf_id=inf_id,
            name="Test Ambiguous",
            correction=correction,
        ),
    )

    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)

    assert result.marked == 0, result.summary_line()
    assert result.skipped_ambiguous == 1
    assert result.orphaned == 0

    # Verify no SUPERSEDED line was inserted.
    text = rec.read_text(encoding="utf-8")
    assert "<!-- SUPERSEDED:" not in text


# --- Orphaned reference (BEGIN_INFERRED missing) --------------------------


def test_orphaned_reference_counted(tmp_vault: Path):
    # SUPERSEDES references an inf-XXX that doesn't exist anywhere in
    # this record. We expect the sweep to count it under ``orphaned``
    # and continue without crashing.
    body = dedent(
        """\
        ---
        type: note
        name: Orphan Test
        created: '2026-04-27'
        ---

        # Orphan Test

        <!-- correction 2026-04-27: Mis-inference was Salem's.
        <!-- SUPERSEDES: inf-20260426-salem-deadbe --> -->
        """
    )
    rec = _write(tmp_vault, "note/Orphan Test.md", body)

    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)

    assert result.marked == 0, result.summary_line()
    assert result.orphaned == 1
    assert result.errors == []

    # File should be unchanged.
    assert rec.read_text(encoding="utf-8") == body


# --- Idempotence ----------------------------------------------------------


def test_idempotence_second_run_is_noop(tmp_vault: Path):
    inf_id = "inf-20260426-salem-cccccc"
    correction = (
        "<!-- correction 2026-04-27: Mis-inference was Salem's. "
        f"<!-- SUPERSEDES: {inf_id} --> -->"
    )
    rec = _write(
        tmp_vault,
        "person/Idempotent.md",
        _record_with_inferred_block(
            inf_id=inf_id,
            name="Idempotent",
            correction=correction,
        ),
    )

    first = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)
    assert first.marked == 1

    after_first = rec.read_text(encoding="utf-8")
    assert after_first.count("<!-- SUPERSEDED: see correction-") == 1

    second = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)
    assert second.marked == 0, second.summary_line()
    assert second.skipped_already_marked >= 1

    after_second = rec.read_text(encoding="utf-8")
    # Content stable across runs.
    assert after_second == after_first
    # Still exactly one SUPERSEDED line.
    assert after_second.count("<!-- SUPERSEDED: see correction-") == 1


# --- Dry-run does not write -----------------------------------------------


def test_dry_run_does_not_write(tmp_vault: Path):
    inf_id = "inf-20260426-salem-dddddd"
    correction = (
        "<!-- correction 2026-04-27: Mis-inference was Salem's. "
        f"<!-- SUPERSEDES: {inf_id} --> -->"
    )
    rec = _write(
        tmp_vault,
        "person/Dry Run.md",
        _record_with_inferred_block(
            inf_id=inf_id,
            name="Dry Run",
            correction=correction,
        ),
    )

    before = rec.read_text(encoding="utf-8")
    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=False)

    # Dry run reports the candidate but ``marked`` stays at 0 because
    # no write happened.
    assert result.marked == 0
    assert len(result.candidates) == 1
    assert result.candidates[0].attribution == "agent"

    after = rec.read_text(encoding="utf-8")
    assert before == after


# --- Multiple corrections in one record -----------------------------------


def test_multiple_corrections_same_record(tmp_vault: Path):
    """Two distinct inferred blocks each get their own marker."""
    body = dedent(
        """\
        ---
        type: person
        name: Two Blocks
        created: '2026-04-27'
        ---

        # Two Blocks

        <!-- BEGIN_INFERRED marker_id="inf-20260426-salem-block1" -->
        First wrong inference about role.
        <!-- END_INFERRED marker_id="inf-20260426-salem-block1" -->

        <!-- BEGIN_INFERRED marker_id="inf-20260426-salem-block2" -->
        Second wrong inference about address.
        <!-- END_INFERRED marker_id="inf-20260426-salem-block2" -->

        <!-- correction 2026-04-27: Mis-inference was Salem's on the role.
        <!-- SUPERSEDES: inf-20260426-salem-block1 --> -->

        <!-- correction 2026-04-27: Salem recorded incorrectly about the address.
        <!-- SUPERSEDES: inf-20260426-salem-block2 --> -->
        """
    )
    rec = _write(tmp_vault, "person/Two Blocks.md", body)

    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)

    assert result.marked == 2, result.summary_line()
    assert result.errors == []

    after = rec.read_text(encoding="utf-8")
    assert after.count("<!-- SUPERSEDED: see correction-") == 2


# --- Ignore-dirs honored --------------------------------------------------


def test_ignore_dirs_skips_files(tmp_vault: Path):
    """Files under ignored dirs (e.g. ``_templates``) must not be touched."""
    inf_id = "inf-20260426-salem-eeeeee"
    correction = (
        "<!-- correction 2026-04-27: Mis-inference was Salem's. "
        f"<!-- SUPERSEDES: {inf_id} --> -->"
    )
    body = _record_with_inferred_block(
        inf_id=inf_id, name="Template Sample", correction=correction,
    )
    rec = _write(tmp_vault, "_templates/sample.md", body)

    before = rec.read_text(encoding="utf-8")
    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)

    assert result.marked == 0
    after = rec.read_text(encoding="utf-8")
    assert before == after


# --- Cross-record reference is orphaned (same-record only is the rule) ----


def test_cross_record_reference_is_orphaned(tmp_vault: Path):
    """A SUPERSEDES in record A pointing at an inf-XXX in record B is
    counted as orphaned. The brief specifies same-record search only;
    cross-record back-references would need a different mechanism."""
    inf_id = "inf-20260426-salem-ffffff"
    # Record B has the BEGIN_INFERRED.
    _write(
        tmp_vault,
        "person/Original.md",
        _record_with_inferred_block(
            inf_id=inf_id,
            name="Original",
            correction="",
        ),
    )
    # Record A has the SUPERSEDES.
    correction = (
        "<!-- correction 2026-04-27: Mis-inference was Salem's. "
        f"<!-- SUPERSEDES: {inf_id} --> -->"
    )
    rec_a = _write(
        tmp_vault,
        "note/Other.md",
        dedent(
            f"""\
            ---
            type: note
            name: Other
            created: '2026-04-27'
            ---

            # Other

            {correction}
            """
        ),
    )

    result = run_superseded_marker_sweep(_config_for(tmp_vault), apply=True)

    assert result.marked == 0, result.summary_line()
    assert result.orphaned == 1


# --- Discriminator unit tests -------------------------------------------


def test_classify_attribution_agent_phrases():
    from alfred.janitor.superseded_marker import _classify_attribution

    assert _classify_attribution("Mis-inference was Salem's.") == "agent"
    assert _classify_attribution("Salem recorded inaccurately.") == "agent"
    assert _classify_attribution("Hypatia mis-inferred the relationship.") == "agent"
    assert _classify_attribution("KAL-LE recorded incorrectly.") == "agent"
    assert _classify_attribution("You crossed wires on this one.") == "agent"


def test_classify_attribution_user_phrases():
    from alfred.janitor.superseded_marker import _classify_attribution

    assert _classify_attribution("The error was Andrew's.") == "user"
    assert _classify_attribution("Salem recorded accurately.") == "user"
    assert _classify_attribution("Andrew gave wrong info originally.") == "user"


def test_classify_attribution_user_takes_priority():
    """When both phrases match, user-attribution wins because the
    contrastive 'recorded accurately' explicitly denies an LLM error."""
    from alfred.janitor.superseded_marker import _classify_attribution

    text = "Salem recorded accurately — the error was Andrew's, no mis-inference."
    assert _classify_attribution(text) == "user"


def test_classify_attribution_unknown_when_silent():
    from alfred.janitor.superseded_marker import _classify_attribution

    assert _classify_attribution("see updated info below.") == "unknown"
    assert _classify_attribution("") == "unknown"
