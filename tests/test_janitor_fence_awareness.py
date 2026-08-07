"""#61 — LINK001 must not fire on wikilinks that are DATA inside a fence.

Written CONTRACT-FIRST from the task message, before reading the scanner's
LINK001 logic.

## Why this exists, and why it gates the drip

#57 taught the ingest path to fence uploaded file content into record bodies.
A bank CSV containing ``[[something]]``, or a fenced code sample discussing
``[[links]]``, is now ordinary vault content — and a fenced wikilink is DATA,
not a reference. The scanner does not know that yet, so every such record
manufactures LINK001 findings about text nobody linked.

The consequence is what makes it load-bearing rather than cosmetic: a LINK001
work-list built after fence-aware records exist would carry those data-links,
and the drip campaign's remove branch performs IRREVERSIBLE deletions. So the
finding is not "noisy report" — it is "automated deletion of data out of a code
block". This has to land before the campaign resumes.

## The direction "safe" points, stated once

Two failure modes, and they are NOT symmetric here:

* a FALSE POSITIVE (data-link reported as a finding) feeds the irreversible
  removal path — the expensive direction;
* a FALSE NEGATIVE (real broken link hidden) leaves a link unfixed — cheap, and
  visible on the next sweep once the document is corrected.

That argues for excluding aggressively. It is bounded, though, by the
malformed-fence case (below), where the ruling goes the other way for a reason
that is about a DIFFERENT risk: one document defect must not be allowed to mask
every other finding in the file.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import structlog

from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.issues import IssueCode
from alfred.janitor.scanner import run_structural_scan
from alfred.janitor.state import JanitorState

# The target every fixture links to. Deliberately absent from every vault built
# here, so any occurrence the scanner SEES becomes a LINK001 — which makes
# "no finding" mean "the scanner did not see it", not "it happened to resolve".
GHOST = "person/Ghost Who Does Not Exist"

FM = dedent(
    """\
    type: note
    name: Fixture
    created: '2026-08-07'
    tags: []"""
)


def _config(vault: Path, state_dir: Path) -> JanitorConfig:
    return JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=[".obsidian", "_templates", "_bases"],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "janitor_state.json")),
    )


def _scan(tmp_path: Path, body: str, *, frontmatter: str = FM) -> list:
    """Write ONE record with ``body`` and return its LINK001 findings."""
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True, exist_ok=True)
    (vault / "note" / "Fixture.md").write_text(
        f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    config = _config(vault, state_dir)
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    issues = run_structural_scan(config, state)
    return [
        i for i in issues
        if i.file == "note/Fixture.md" and i.code == IssueCode.BROKEN_WIKILINK
    ]


# ---------------------------------------------------------------------------
# the headline contract
# ---------------------------------------------------------------------------


def test_a_fenced_dangling_link_is_NOT_a_finding(tmp_path: Path) -> None:
    """THE regression test for #61. This is a bank CSV's content, not a link."""
    findings = _scan(tmp_path, f"```\nname,ref\nwidget,[[{GHOST}]]\n```\n")
    assert findings == [], (
        f"a wikilink inside a fence is DATA, not a reference: "
        f"{[i.message for i in findings]}"
    )


def test_the_same_link_OUTSIDE_a_fence_is_still_a_finding(tmp_path: Path) -> None:
    """The other half, and the one that keeps the fix honest. A change that
    simply stopped reporting LINK001 would pass the test above."""
    findings = _scan(tmp_path, f"See [[{GHOST}]] for context.\n")
    assert len(findings) == 1
    assert GHOST in findings[0].message


def test_one_record_with_both_reports_EXACTLY_the_unfenced_one(
    tmp_path: Path,
) -> None:
    """The discriminating case: same target, twice, one fenced one not. A
    file-level "does this record contain a fence" shortcut would wrongly
    silence the real finding here, and a fence-blind scanner would report two."""
    findings = _scan(
        tmp_path,
        f"Real reference: [[{GHOST}]]\n\n```\ncsv,data\nx,[[{GHOST}]]\n```\n",
    )
    assert len(findings) == 1, (
        f"expected exactly the unfenced occurrence: "
        f"{[i.message for i in findings]}"
    )


def test_a_language_tagged_fence_counts(tmp_path: Path) -> None:
    """The shape #57 actually writes — ```csv, not a bare fence."""
    findings = _scan(tmp_path, f"```csv\nname,ref\nwidget,[[{GHOST}]]\n```\n")
    assert findings == []


def test_a_tilde_fence_counts(tmp_path: Path) -> None:
    """CommonMark allows ~~~ as well as ```. A record that used it would
    otherwise be scanned as if unfenced."""
    findings = _scan(tmp_path, f"~~~\ndata,[[{GHOST}]]\n~~~\n")
    assert findings == []


def test_a_long_fence_counts_and_an_inner_short_run_does_not_close_it(
    tmp_path: Path,
) -> None:
    """#57 grows the fence past any backtick run in the content, so a four-tick
    fence containing a three-tick line is exactly what the ingest path emits
    for a document that quotes a code block. The inner run must NOT terminate
    the block early, or everything after it gets scanned as prose."""
    findings = _scan(
        tmp_path, f"````csv\nrow,```,[[{GHOST}]]\n````\n",
    )
    assert findings == []


def test_content_after_a_CLOSED_fence_is_scanned_again(tmp_path: Path) -> None:
    """Exclusion is scoped to the block, not to everything downstream of the
    first fence."""
    findings = _scan(
        tmp_path,
        f"```\ndata,[[{GHOST}]]\n```\n\nAnd a real one: [[{GHOST}]]\n",
    )
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# malformed fence — fails SAFE, and "safe" here means treat-as-UNFENCED
# ---------------------------------------------------------------------------


def test_an_UNCLOSED_fence_does_not_hide_the_links_after_it(
    tmp_path: Path,
) -> None:
    """Ruled direction, and it is the opposite of the general bias stated in
    the module docstring — deliberately.

    An unterminated fence is a DOCUMENT DEFECT. If it swallowed the rest of the
    file, a single stray ``` would silently switch off LINK001 for everything
    below it, and the janitor would under-report with no signal that it had.
    One defect must not be allowed to mask every other finding in the record.
    Silent under-reporting is the failure this codebase rules against
    everywhere else; a false positive here is noise a human triages.
    """
    findings = _scan(tmp_path, f"```\nopened but never closed\n\n[[{GHOST}]]\n")
    assert len(findings) == 1, (
        "an unclosed fence must not hide real links behind it"
    )


def test_a_lone_closing_fence_at_the_end_does_not_swallow_the_body(
    tmp_path: Path,
) -> None:
    """The same defect from the other side: a stray ``` after real content."""
    findings = _scan(tmp_path, f"A real link [[{GHOST}]]\n\n```\n")
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# frontmatter is never fenced — a body concept must not reach it
# ---------------------------------------------------------------------------


def test_frontmatter_links_are_unaffected_by_body_fences(
    tmp_path: Path,
) -> None:
    """Fences are a BODY construct; YAML has no such thing. A record whose body
    is entirely one fenced block must still have its frontmatter references
    checked — otherwise ingesting a CSV would switch off link checking for the
    record's real relationship fields."""
    fm = dedent(
        f"""\
        type: note
        name: Fixture
        created: '2026-08-07'
        tags: []
        related:
        - '[[{GHOST}]]'"""
    )
    findings = _scan(
        tmp_path, "```csv\na,b\n1,2\n```\n", frontmatter=fm,
    )
    assert len(findings) == 1, (
        "the frontmatter reference is real and must still be reported"
    )


def test_a_fence_marker_inside_frontmatter_does_not_open_a_block(
    tmp_path: Path,
) -> None:
    """Backticks inside a YAML scalar are text. If the fence scan started
    before the frontmatter ended, a record with ``` in a field would silence
    the whole body."""
    fm = dedent(
        """\
        type: note
        name: Fixture
        created: '2026-08-07'
        tags: []
        janitor_note: 'use ``` to fence'"""
    )
    findings = _scan(tmp_path, f"A real link [[{GHOST}]]\n", frontmatter=fm)
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# inline code spans — same logic, narrower blast radius
# ---------------------------------------------------------------------------


def test_a_link_in_an_inline_code_span_is_not_a_finding(
    tmp_path: Path,
) -> None:
    """Decision (stated in the report): inline spans follow the same rule as
    fences, because the justification is identical — text a reader sees as
    literal is data, not a reference. Kept CONSERVATIVE: a span is only
    recognised with matching backtick runs on ONE line, so a stray backtick
    cannot mask links across a paragraph the way an unclosed fence could."""
    findings = _scan(tmp_path, f"Write it as `[[{GHOST}]]` in the field.\n")
    assert findings == []


def test_an_unmatched_backtick_does_not_mask_the_rest_of_the_line(
    tmp_path: Path,
) -> None:
    """The bounding case for the decision above — same reasoning as the
    unclosed fence, one scale down."""
    findings = _scan(tmp_path, f"A stray ` tick and then [[{GHOST}]]\n")
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# ILB — the exclusion is silent per-link, so the SWEEP must count it
# ---------------------------------------------------------------------------


def test_the_sweep_reports_how_many_links_it_skipped(tmp_path: Path) -> None:
    """Per-link silence is by design; per-sweep silence is not. Without a
    counter, "the scanner is fence-aware" and "the scanner stopped finding
    anything" look identical from the logs."""
    with structlog.testing.capture_logs() as captured:
        _scan(tmp_path, f"```\ndata,[[{GHOST}]]\n```\n")
    events = [e for e in captured if "fenced_links_skipped" in e]
    assert events, "no sweep event reported the fenced-link exclusion count"
    assert any(e["fenced_links_skipped"] >= 1 for e in events)


def test_the_counter_is_reported_even_when_nothing_was_skipped(
    tmp_path: Path,
) -> None:
    """ILB. A field that only appears when non-zero cannot answer "is the
    exclusion running?" — the operator would have to already know the answer to
    interpret its absence."""
    with structlog.testing.capture_logs() as captured:
        _scan(tmp_path, f"A plain link [[{GHOST}]]\n")
    events = [e for e in captured if "fenced_links_skipped" in e]
    assert events, "the count must be emitted on every sweep, not only non-zero"
    assert all(e["fenced_links_skipped"] == 0 for e in events)
