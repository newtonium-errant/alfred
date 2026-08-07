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
from alfred.janitor.parser import extract_wikilinks, mask_code_regions
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


# ---------------------------------------------------------------------------
# CRLF — the masking function itself, driven DIRECTLY
# ---------------------------------------------------------------------------
#
# #61 gate, WARN-1. ``mask_code_regions`` splits on "\n", so a CRLF document
# hands ``_FENCE_LINE_RE`` the string "```\r" — which ``[^\r\n]*`` cannot
# consume and ``$`` will not match before. Without the trailing ``\r?`` the
# pattern fails on EVERY line of such a document and the exclusion silently does
# nothing, reporting 0 skipped because 0 were skipped.
#
# These drive ``mask_code_regions`` DIRECTLY rather than through
# :func:`_scan`, and that is the whole reason they work. My first attempt at
# this matrix went through the scanner and was green with the fix REVERTED —
# because ``Path.read_text()`` applies universal newlines, so a CRLF file on
# disk is already LF by the time the scanner sees it (pinned below). A
# scanner-level CRLF test cannot fail, which makes it worse than no test: it
# looks like coverage of a property it never exercises.


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def test_read_text_normalizes_CRLF_so_the_scanner_never_sees_it(
    tmp_path: Path,
) -> None:
    """The reachability fact, pinned so nobody re-derives it the hard way.

    Every production path into the masking reads through ``read_text`` —
    scanner.py's raw read, the sweep counter, the janitor pipeline — and
    ``frontmatter.loads`` is handed the already-normalized string. So CRLF
    cannot reach ``mask_code_regions`` from disk. The ``\r?`` in the pattern is
    therefore correctness of the FUNCTION, not a live-bug fix: it protects any
    caller whose text did not come through universal-newline decoding.
    """
    f = tmp_path / "crlf.md"
    f.write_bytes(b"---\ntype: note\n---\n\n```csv\r\nx,[[a/b]]\r\n```\r\n")
    assert b"\r\n" in f.read_bytes(), "the file really is CRLF on disk"
    assert "\r\n" not in f.read_text(encoding="utf-8"), (
        "read_text applies universal newlines — this is why a scanner-level "
        "CRLF test cannot exercise the CRLF path"
    )


def _masked_links(text: str) -> list[str]:
    return extract_wikilinks(mask_code_regions(text))


def test_CRLF_a_fenced_link_is_masked(tmp_path: Path) -> None:
    """THE regression test for the gate finding."""
    assert _masked_links(_crlf(f"```csv\nname,ref\nx,[[{GHOST}]]\n```\n")) == []


def test_CRLF_an_unfenced_link_survives_masking(tmp_path: Path) -> None:
    """The other half — the fix must not start eating real references."""
    assert _masked_links(_crlf(f"See [[{GHOST}]] here.\n")) == [GHOST]


def test_CRLF_both_in_one_document_keeps_exactly_the_unfenced_one() -> None:
    text = _crlf(f"Real: [[{GHOST}]]\n\n```\ncsv,data\nx,[[{GHOST}]]\n```\n")
    assert _masked_links(text) == [GHOST]


def test_CRLF_an_unclosed_fence_still_fails_SAFE() -> None:
    """The fail-safe direction must survive the line ending too, or a CRLF file
    with one stray ``` would hide every link below it."""
    assert _masked_links(
        _crlf(f"```\nopened, never closed\n\n[[{GHOST}]]\n")
    ) == [GHOST]


def test_CRLF_a_closer_carrying_an_info_string_does_not_close() -> None:
    """CommonMark: only a bare marker closes. This is the case that separates
    the correct fix from the tempting one — letting ``\r`` fall INTO the info
    group would make every CRLF closer look info-bearing, the block would never
    close, and the rest of the file would be masked away."""
    assert _masked_links(
        _crlf(f"```\ndata,[[{GHOST}]]\n```csv\nstill inside\n```\n")
    ) == []


def test_CRLF_a_tilde_fence_counts() -> None:
    assert _masked_links(_crlf(f"~~~\ndata,[[{GHOST}]]\n~~~\n")) == []


def test_CRLF_a_grown_fence_survives_an_inner_short_run() -> None:
    """#57 emits ````csv when the content quotes a ``` line."""
    assert _masked_links(_crlf(f"````csv\nrow,```,[[{GHOST}]]\n````\n")) == []


def test_CRLF_content_after_a_closed_fence_is_scanned_again() -> None:
    assert _masked_links(
        _crlf(f"```\ndata,[[{GHOST}]]\n```\n\nReal: [[{GHOST}]]\n")
    ) == [GHOST]


def test_CRLF_an_inline_code_span_is_masked() -> None:
    assert _masked_links(_crlf(f"Write `[[{GHOST}]]` in the field.\n")) == []


def test_CRLF_frontmatter_is_left_alone() -> None:
    """Fences are a body construct either way round."""
    text = _crlf(
        f"---\ntype: note\nrelated:\n- '[[{GHOST}]]'\n---\n\n```csv\na,b\n```\n"
    )
    assert _masked_links(text) == [GHOST]


def test_a_MIXED_line_ending_document_is_handled() -> None:
    """Real files get merged, patched and hand-edited, so one document can carry
    both endings. Neither half may switch the other's masking off."""
    text = (
        f"```\r\ncrlf,[[{GHOST}]]\r\n```\r\n"
        f"\n```\nlf,[[{GHOST}]]\n```\n"
        f"\nReal: [[{GHOST}]]\n"
    )
    assert _masked_links(text) == [GHOST]


# ---------------------------------------------------------------------------
# NOTE-2 — the docstring's claim now has an assertion behind it
# ---------------------------------------------------------------------------


def test_backticks_on_SEPARATE_lines_do_not_mask_the_link_between_them(
    tmp_path: Path,
) -> None:
    """The bound on the inline-span decision, asserted rather than asserted-at.

    ``_INLINE_CODE_RE`` matches within ONE line by construction, so a stray
    backtick on one line and another three lines later cannot swallow the prose
    in between. Until this test that was a claim in a docstring; the earlier
    bounding case only covered a single line, which would have stayed green
    even if the pattern had been allowed to span them.
    """
    findings = _scan(
        tmp_path, f"A stray ` tick here\n\n[[{GHOST}]]\n\nand another ` tick\n",
    )
    assert len(findings) == 1, (
        "a link between backticks on DIFFERENT lines is still a reference"
    )


def test_backticks_spanning_lines_do_not_mask_under_CRLF_either(
    tmp_path: Path,
) -> None:
    findings = _scan(
        tmp_path,
        _crlf(f"A stray ` tick\n\n[[{GHOST}]]\n\nanother ` tick\n"),
    )
    assert len(findings) == 1
