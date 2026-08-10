"""#69 guard half — drip's link001 MUTATION path must not touch fenced text.

Written CONTRACT-FIRST from the #69 ruling, deliberately NOT from the
reproduction that motivated it. The repro proves one shape exists; a matrix
derived from it would only ever pin the shape I happened to construct, and the
fence/inline-span space has more corners than that. The repro's exact case is
ONE row below, not the specification.

## Why this is the resume gate

#61 made the SCANNER fence-aware, so a work-list built today carries no
data-links. That is not the same as the mutation being safe:

* the FROZEN work-list was built by a fence-blind scanner and still carries
  whatever it captured — the box rebuild is the other half of #69;
* and even against a clean work-list, a record can carry the SAME target both
  outside a fence (a legitimate finding) and inside one (data). ``work()``
  matched raw text, so it deleted BOTH, and ``verify()`` — equally fence-blind —
  then saw nothing left and recorded DONE. Silent, irreversible data loss.

The bias throughout is **strictly remove fewer**. On a path that cannot be
undone, a missed removal is a retry; an extra one is gone.

## The verify semantics these pin

``verify()`` observes the UNFENCED text only — the same masking ``work()``
mutates through. Two consequences, and both are contract:

* a fenced-only surviving copy is DATA, so it must NOT read as "link still
  present" (no false-pending — the item would retry forever against text
  nothing is allowed to touch);
* a fenced copy must NEVER satisfy "link removed" while an unfenced copy
  survives (no false-done — that is the 907 shape wearing the verifier's
  uniform).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.drip.campaigns import Link001Campaign
from alfred.janitor.parser import extract_wikilinks, mask_code_regions

GHOST = "person/Ghost"
OTHER = "person/Other Ghost"
MARK = "link-provenance"


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "note").mkdir(parents=True, exist_ok=True)
    return v


def _record(tmp_path: Path, body: str, *, frontmatter: str = "type: note") -> Path:
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8",
    )
    return vault


def _campaign(vault: Path, item: str) -> Link001Campaign:
    return Link001Campaign(worklist_items=[item], vault_path=vault)


def _read(vault: Path) -> str:
    return (vault / "note" / "R.md").read_text(encoding="utf-8")


def _scanner_sees(text: str) -> list[str]:
    """What the post-#61 scanner actually extracts from DOCUMENT text.

    The composition ``structural_wikilinks`` uses, not the bare primitive — see
    the pin-evolution note in ``test_wrapped_links.py``.
    """
    return extract_wikilinks(mask_code_regions(text))


# ---------------------------------------------------------------------------
# the matrix — one row per shape named in the ruling
# ---------------------------------------------------------------------------


def test_FENCED_ONLY_is_left_completely_untouched(tmp_path: Path) -> None:
    """A stale work-list item pointing at fenced data. The file must come out
    byte-identical — this is the row that makes the frozen list survivable."""
    body = f"```csv\nname,ref\nwidget,[[{GHOST}]]\n```\n"
    vault = _record(tmp_path, body)
    before = _read(vault)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)

    assert _read(vault) == before, "fenced data must not be mutated at all"


def test_FENCED_ONLY_verifies_as_DONE_not_pending(tmp_path: Path) -> None:
    """No false-pending. The fenced copy is data, so there is nothing left to
    remove — an item that reported "still present" here would retry forever
    against text the guard forbids touching."""
    vault = _record(tmp_path, f"```csv\nx,[[{GHOST}]]\n```\n")
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    assert _campaign(vault, item).verify(item) is True


def test_UNFENCED_ONLY_still_removes_exactly_as_before(tmp_path: Path) -> None:
    """The guard must not become a general brake. Unfenced text is #60's
    behaviour, unchanged."""
    vault = _record(tmp_path, f"See [[{GHOST}]] here.\n")
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)

    assert _read(vault) == "---\ntype: note\n---\n\nSee here.\n"
    assert c.verify(item) is True


def test_BOTH_SAME_TARGET_removes_the_unfenced_and_spares_the_fenced(
    tmp_path: Path,
) -> None:
    """THE #69 row — the reproduction's exact shape, as one case among many.

    Pre-guard: work() deleted both and verify() reported done, gutting the CSV
    row to ``widget,`` with nothing saying so.
    """
    body = f"Real: [[{GHOST}]]\n\n```csv\nname,ref\nwidget,[[{GHOST}]]\n```\n"
    vault = _record(tmp_path, body)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)
    after = _read(vault)

    assert f"widget,[[{GHOST}]]" in after, "the fenced CSV cell survives intact"
    assert "Real:" in after and f"Real: [[{GHOST}]]" not in after, (
        "the unfenced reference is gone"
    )
    assert c.verify(item) is True, (
        "and verify reports done — because the UNFENCED copy is gone, which is "
        "the only copy it is allowed to observe"
    )


def test_BOTH_DIFFERENT_TARGETS_only_the_aimed_unfenced_one_moves(
    tmp_path: Path,
) -> None:
    body = (
        f"Real: [[{GHOST}]]\n\n"
        f"```csv\nname,ref\nwidget,[[{OTHER}]]\n```\n\n"
        f"Also real: [[{OTHER}]]\n"
    )
    vault = _record(tmp_path, body)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)
    after = _read(vault)

    assert GHOST not in "".join(_scanner_sees(after)), "the aimed link is gone"
    assert f"widget,[[{OTHER}]]" in after, "the fenced OTHER survives"
    assert f"Also real: [[{OTHER}]]" in after, "the unfenced OTHER is untouched"


def test_a_fence_immediately_after_the_frontmatter_is_still_a_fence(
    tmp_path: Path,
) -> None:
    """Frontmatter-adjacent position. The masking skips the frontmatter block;
    a fence opening on the very next line must not be swallowed by that skip or
    treated as part of it."""
    vault = _record(tmp_path, f"```csv\nx,[[{GHOST}]]\n```\n\nReal: [[{GHOST}]]\n")
    before = _read(vault)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)
    after = _read(vault)

    assert f"x,[[{GHOST}]]" in after, "the fenced copy survives"
    assert before != after, "the unfenced copy was still removed"
    assert c.verify(item) is True


def test_an_inline_code_span_is_protected_like_a_fence(tmp_path: Path) -> None:
    """Same justification, one scale down — literal text is data."""
    vault = _record(tmp_path, f"Write `[[{GHOST}]]` in the field.\n")
    before = _read(vault)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)

    assert _read(vault) == before


def test_an_UNCLOSED_fence_does_not_shield_the_links_after_it(
    tmp_path: Path,
) -> None:
    """Inherits #61's fail-safe direction, and it matters MORE here: if a stray
    ``` shielded everything below it, one malformed record could silently
    exempt itself from the whole repair campaign."""
    vault = _record(tmp_path, f"```\nnever closed\n\nReal: [[{GHOST}]]\n")
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)

    assert GHOST not in "".join(_scanner_sees(_read(vault)))


# ---------------------------------------------------------------------------
# the annotate branch shares the seam
# ---------------------------------------------------------------------------


def test_annotate_does_not_mark_a_fenced_occurrence(tmp_path: Path) -> None:
    """A provenance comment injected into a CSV cell would corrupt the data as
    surely as deleting it."""
    vault = _record(tmp_path, f"```csv\nname,ref\nwidget,[[{GHOST}]]\n```\n")
    before = _read(vault)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=True)

    _campaign(vault, item).work(item)

    assert _read(vault) == before


def test_annotate_marks_the_unfenced_copy_and_spares_the_fenced(
    tmp_path: Path,
) -> None:
    body = f"Real: [[{GHOST}]]\n\n```csv\nx,[[{GHOST}]]\n```\n"
    vault = _record(tmp_path, body)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=True)
    c = _campaign(vault, item)

    c.work(item)
    after = _read(vault)

    assert after.count(MARK) == 1, "exactly one annotation, on the real one"
    assert f"x,[[{GHOST}]]\n```" in after, "the fenced cell is unannotated"
    assert c.verify(item) is True


def test_annotate_verify_ignores_a_fenced_pseudo_annotation(
    tmp_path: Path,
) -> None:
    """No false-done on the annotate side: an annotation that only exists
    inside a fence is text in a code block, not provenance on a reference."""
    body = (
        f"Real: [[{GHOST}]]\n\n"
        f"```\n[[{GHOST}]] <!-- {MARK}: retained (learn record) -->\n```\n"
    )
    vault = _record(tmp_path, body)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=True)

    assert _campaign(vault, item).verify(item) is False, (
        "the real reference is still unannotated — the fenced text is not "
        "provenance"
    )


def test_annotate_stays_idempotent_across_the_guard(tmp_path: Path) -> None:
    vault = _record(tmp_path, f"Real: [[{GHOST}]]\n\n```\nx,[[{GHOST}]]\n```\n")
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=True)
    c = _campaign(vault, item)

    c.work(item)
    c.work(item)

    assert _read(vault).count(MARK) == 1


# ---------------------------------------------------------------------------
# verify agrees with the SCANNER (the #60 safety property, re-based on #61)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        f"Real: [[{GHOST}]]\n",
        f"```csv\nx,[[{GHOST}]]\n```\n",
        f"Real: [[{GHOST}]]\n\n```csv\nx,[[{GHOST}]]\n```\n",
        f"Write `[[{GHOST}]]` inline.\n",
    ],
)
def test_verify_never_disagrees_with_what_the_scanner_sees(
    tmp_path: Path, body: str,
) -> None:
    """#60's property, re-based on #61's scanner.

    The pins that shipped with #60 compared against the bare
    ``extract_wikilinks``. Production no longer uses that seam for document
    text — ``structural_wikilinks`` composes it with ``mask_code_regions`` —
    so agreement is now measured against the composition. Comparing against
    the old seam would be measuring something production stopped doing, which
    is how a pin goes green for the wrong reason.
    """
    vault = _record(tmp_path, body)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    scanner_sees_it = GHOST in _scanner_sees(_read(vault))
    assert c.verify(item) is not scanner_sees_it


# ---------------------------------------------------------------------------
# ILB — a removal that touched LESS than raw matching would have is observable
# ---------------------------------------------------------------------------


def test_a_skipped_fenced_occurrence_is_logged(tmp_path: Path) -> None:
    """Silence here would be indistinguishable from the pre-guard behaviour on
    the one signal that tells them apart — this is how an operator confirms the
    guard is live rather than that nothing needed guarding."""
    body = f"Real: [[{GHOST}]]\n\n```csv\nx,[[{GHOST}]]\n```\n"
    vault = _record(tmp_path, body)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)

    with structlog.testing.capture_logs() as captured:
        _campaign(vault, item).work(item)

    events = [e for e in captured
              if e.get("event") == "drip.link001.fenced_skip"]
    assert len(events) == 1
    assert events[0]["path"] == "note/R.md"
    assert events[0]["branch"] == "remove"
    assert events[0]["skipped"] == 1


def test_no_fenced_skip_event_when_nothing_was_fenced(tmp_path: Path) -> None:
    """The event means something only if it is absent when nothing was
    skipped."""
    vault = _record(tmp_path, f"Real: [[{GHOST}]]\n")
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)

    with structlog.testing.capture_logs() as captured:
        _campaign(vault, item).work(item)

    assert not [e for e in captured
                if e.get("event") == "drip.link001.fenced_skip"]


# ---------------------------------------------------------------------------
# CRLF twin, driven directly (per #61's lesson about scanner-level CRLF tests)
# ---------------------------------------------------------------------------


def test_CRLF_fenced_content_is_protected(tmp_path: Path) -> None:
    """Written as bytes so the CRLF genuinely reaches the mutation path —
    ``read_text`` normalizes on the way in, so the record is LF by the time
    work() sees it, but the fixture proves the round trip does not strip the
    protection. The masking function's own CRLF matrix lives in
    ``tests/test_janitor_fence_awareness.py`` and drives it directly.
    """
    vault = _vault(tmp_path)
    raw = (
        f"---\r\ntype: note\r\n---\r\n\r\n"
        f"Real: [[{GHOST}]]\r\n\r\n```csv\r\nx,[[{GHOST}]]\r\n```\r\n"
    ).encode("utf-8")
    (vault / "note" / "R.md").write_bytes(raw)
    item = Link001Campaign.build_item("note/R.md", GHOST, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)
    after = _read(vault)

    assert f"x,[[{GHOST}]]" in after, "the fenced cell survives a CRLF record"
    assert c.verify(item) is True
