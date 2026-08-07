"""#60 — the wrapped-frontmatter-link false-verify defect.

Written CONTRACT-FIRST from task #60, deliberately without reading the parked
WIP's own tests: a saturated predecessor's code is not evidence of the contract.

## The defect, as caught live on the watched first increment (2026-08-06)

The janitor SCANNER finds dangling links by parsing YAML frontmatter, and
``janitor.parser.extract_wikilinks`` whitespace-normalizes every target it
captures. So the frozen work-list carries the target in FLAT form — single
spaces — regardless of how the file spells it on disk.

``work()`` and ``verify()`` then looked for the exact substring
``f"[[{target}]]"`` in the RAW file text. When a long title makes PyYAML fold
the scalar across physical lines, that substring is not there:

    sources:
    - '[[note/2026-07-14 Weekly Operations Review Meeting Notes And Follow Up
      For The Northern Pulp Remediation Programme]]'

Two consequences, and the second is the campaign-integrity bug:

1. ``work()`` no-ops — it finds nothing to remove, so nothing is removed;
2. ``verify()``'s ``link not in body`` check PASSES **because of** the very
   wrapping that defeated the mutation, so the item is marked DONE.

Measured: 4 of the first 12 increment items were false-dones (~33%; long titles
wrap constantly). The failure direction was conservative — nothing was wrongly
deleted — but a verifier satisfiable by the exact defect it exists to catch is
worthless, which is why this is a fix and not a note.

## What the tests below hold

* the mutation must FIND a folded link and land on it, leaving valid YAML;
* the verifier must be normalized so wrapping can no longer satisfy it —
  including the headline regression: **wrapped link still present ⇒ NOT done**;
* the verifier agrees with the SCANNER, not with the mutator. A verifier that
  reuses the mutator's matcher is satisfied by the mutator's own blind spots,
  which is this defect's shape one level up.

Every wrapped fixture asserts that the fold ACTUALLY happened
(:func:`_assert_folded`). Without that guard, shortening a title would silently
turn each of these into a non-wrapped test that passes for the wrong reason —
the fixture-integrity trap that made an entire failure class invisible on #18.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml

from alfred.drip.campaigns import Link001Campaign
from alfred.janitor.parser import extract_wikilinks

#: Long enough that PyYAML's 80-column default folds it. The length is
#: load-bearing, not decorative — see ``_assert_folded``.
LONG_TARGET = (
    "note/2026-07-14 Weekly Operations Review Meeting Notes And Follow Up "
    "Actions For The Northern Pulp Remediation Programme"
)

PROVENANCE = "link-provenance"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "note").mkdir(parents=True, exist_ok=True)
    return v


def _assert_folded(text: str, target: str) -> None:
    """The fixture must actually exhibit the defect it is testing.

    A wrapped-link test whose fixture is not wrapped passes trivially and
    proves nothing. This is the guard that keeps the whole file honest.
    """
    assert f"[[{target}]]" not in text, (
        "FIXTURE INTEGRITY: this text is NOT folded, so it cannot exercise the "
        "wrapped-link path — lengthen the target"
    )
    assert target in extract_wikilinks(text), (
        "FIXTURE INTEGRITY: the scanner must still see the link, otherwise the "
        "fixture is not the production shape"
    )


def _folded_record(
    tmp_path: Path, target: str = LONG_TARGET, *, body: str = "Body text.\n",
) -> tuple[Path, str]:
    """A record whose frontmatter link is FOLDED by PyYAML, as on the box.

    Built by the real dumper rather than hand-typed, so the fixture cannot
    drift away from the shape production actually writes.
    """
    vault = _vault(tmp_path)
    fm = yaml.safe_dump(
        {"sources": [f"[[{target}]]"], "type": "constraint"},
        default_flow_style=False, sort_keys=True,
    )
    text = f"---\n{fm}---\n\n{body}"
    _assert_folded(text, target)
    (vault / "note" / "R.md").write_text(text, encoding="utf-8")
    return vault, "note/R.md"


def _campaign(vault: Path, item: str) -> Link001Campaign:
    return Link001Campaign(worklist_items=[item], vault_path=vault)


def _read(vault: Path) -> str:
    return (vault / "note" / "R.md").read_text(encoding="utf-8")


def _fm_parses(text: str) -> object:
    """Parse the frontmatter block, raising if the mutation broke the YAML."""
    assert text.startswith("---\n")
    block = text.split("---\n", 2)[1]
    return yaml.safe_load(block)


# ---------------------------------------------------------------------------
# the premise — pinned, because everything below depends on it
# ---------------------------------------------------------------------------


def test_pyyaml_folds_a_long_wikilink_and_the_scanner_still_sees_it() -> None:
    """The mechanism, measured rather than assumed.

    If PyYAML ever stops folding at 80 columns this pin fails LOUDLY, instead
    of every wrapped test below quietly becoming a non-wrapped one.
    """
    dumped = yaml.safe_dump(
        {"sources": [f"[[{LONG_TARGET}]]"]},
        default_flow_style=False, sort_keys=True,
    )
    assert "\n  " in dumped, "PyYAML folded the scalar onto a continuation line"
    assert f"[[{LONG_TARGET}]]" not in dumped, "…so the exact substring is gone"
    assert extract_wikilinks(dumped) == [LONG_TARGET], (
        "…but the scanner normalizes whitespace, so the work-list target is FLAT"
    )
    assert yaml.safe_load(dumped)["sources"] == [f"[[{LONG_TARGET}]]"], (
        "…and YAML round-trips the fold back to a single space"
    )


# ---------------------------------------------------------------------------
# CONTRACT 2 — the headline regression: verify must FAIL on a wrapped link
# ---------------------------------------------------------------------------


def test_verify_says_NOT_done_while_a_wrapped_link_is_still_present(
    tmp_path: Path,
) -> None:
    """THE regression test for #60, stated exactly as the task states it.

    Nothing has been worked. The link is still there and the scanner still
    reports it. The pre-fix verifier returned True here — satisfied by the
    wrapping — and the runner wrote DONE on an untouched record.
    """
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    c = _campaign(vault, item)

    assert c.verify(item) is False, (
        "a wrapped, still-present link must NOT satisfy the remove verifier"
    )


def test_verify_agrees_with_the_scanner_not_with_the_raw_substring(
    tmp_path: Path,
) -> None:
    """The principle under the fix, pinned independently of the mechanism.

    Whatever normalization verify uses, its answer must track what the SCANNER
    sees. While the scanner reports the link, the item is not done.
    """
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    c = _campaign(vault, item)

    scanner_sees_it = LONG_TARGET in extract_wikilinks(_read(vault))
    assert scanner_sees_it is True
    assert c.verify(item) is not scanner_sees_it, (
        "verify and the scanner must never disagree about the same record"
    )


# ---------------------------------------------------------------------------
# CONTRACT 1 — wrap-tolerant work(), valid YAML, landing where aimed
# ---------------------------------------------------------------------------


def test_remove_finds_and_deletes_a_folded_link(tmp_path: Path) -> None:
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)

    after = _read(vault)
    assert LONG_TARGET not in extract_wikilinks(after), (
        "the scanner must no longer see the link — that is what 'removed' means"
    )
    assert c.verify(item) is True


def test_remove_on_a_folded_link_leaves_valid_yaml(tmp_path: Path) -> None:
    """'Must produce valid YAML.' A mutation that corrupts frontmatter turns a
    link repair into an unparseable record — strictly worse than the break."""
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)

    fm = _fm_parses(_read(vault))
    assert isinstance(fm, dict)
    assert fm["type"] == "constraint", "the untargeted keys survive intact"


def test_annotate_finds_and_marks_a_folded_link(tmp_path: Path) -> None:
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=True)
    c = _campaign(vault, item)

    c.work(item)

    after = _read(vault)
    assert PROVENANCE in after, "the annotate branch must actually annotate"
    assert LONG_TARGET in extract_wikilinks(after), (
        "…and the annotate branch KEEPS the link"
    )
    assert isinstance(_fm_parses(after), dict), "…leaving valid YAML"


def test_annotate_verify_accepts_a_folded_annotated_link(tmp_path: Path) -> None:
    """The annotate branch's half of the same defect, in the other direction.

    An un-normalized annotate verifier looks for the flat ``[[t]] <mark>`` and
    cannot find it next to a folded link — so a correctly-annotated item is
    marked FAILED and retried forever. Conservative, but still wrong, and the
    state-repair pass would demote genuinely-done rows without this.
    """
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=True)
    c = _campaign(vault, item)

    assert c.verify(item) is False, "not annotated yet"
    c.work(item)
    assert c.verify(item) is True, "annotated — and the fold must not hide it"


def test_annotate_on_a_folded_link_is_idempotent(tmp_path: Path) -> None:
    """A re-run after a crash must not double-annotate a wrapped link."""
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=True)
    c = _campaign(vault, item)

    c.work(item)
    c.work(item)

    assert _read(vault).count(PROVENANCE) == 1


def test_remove_on_a_folded_link_is_idempotent(tmp_path: Path) -> None:
    vault, rel = _folded_record(tmp_path)
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)
    first = _read(vault)
    c.work(item)

    assert _read(vault) == first, "a second removal must change nothing"


def test_the_mutation_lands_where_aimed_and_spares_the_neighbour(
    tmp_path: Path,
) -> None:
    """Mutation-must-land-where-aimed. Two folded links in one record; only the
    targeted one may move. A matcher loose enough to catch the fold must not be
    loose enough to catch the sibling."""
    other = (
        "note/2026-07-15 Some Other Extremely Long Record Title That Also Folds "
        "Across The Eighty Column Boundary Comfortably"
    )
    vault = _vault(tmp_path)
    fm = yaml.safe_dump(
        {"sources": [f"[[{LONG_TARGET}]]", f"[[{other}]]"], "type": "constraint"},
        default_flow_style=False, sort_keys=True,
    )
    text = f"---\n{fm}---\n\nBody.\n"
    _assert_folded(text, LONG_TARGET)
    _assert_folded(text, other)
    (vault / "note" / "R.md").write_text(text, encoding="utf-8")

    item = Link001Campaign.build_item("note/R.md", LONG_TARGET, citer_is_learn=False)
    c = _campaign(vault, item)
    c.work(item)

    seen = extract_wikilinks(_read(vault))
    assert LONG_TARGET not in seen, "the aimed link is gone"
    assert other in seen, "the neighbouring folded link is untouched"


def test_a_folded_link_in_the_body_is_also_repaired(tmp_path: Path) -> None:
    """Frontmatter is where YAML folds, but the matcher must not be
    frontmatter-only: the same record can carry the link in prose, and work()
    has always removed every occurrence."""
    vault = _vault(tmp_path)
    folded_body = "See [[note/A Title That Was Hard\n  Wrapped By Hand]] here.\n"
    (vault / "note" / "R.md").write_text(
        f"---\ntype: note\n---\n\n{folded_body}", encoding="utf-8",
    )
    target = "note/A Title That Was Hard Wrapped By Hand"
    item = Link001Campaign.build_item("note/R.md", target, citer_is_learn=False)
    c = _campaign(vault, item)

    assert c.verify(item) is False, "still present, however it is spelled"
    c.work(item)
    assert target not in extract_wikilinks(_read(vault))
    assert c.verify(item) is True


# ---------------------------------------------------------------------------
# the non-wrapped path must not regress
# ---------------------------------------------------------------------------


def test_single_line_remove_still_works(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        "---\ntype: note\n---\n\nSee [[person/Ghost]] here.\n", encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "person/Ghost",
                                      citer_is_learn=False)
    c = _campaign(vault, item)
    c.work(item)

    assert _read(vault) == "---\ntype: note\n---\n\nSee here.\n", (
        "the whitespace-healing behaviour is unchanged on the flat path"
    )
    assert c.verify(item) is True


def test_single_line_annotate_still_works(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        "---\ntype: note\n---\n\nSee [[learn/L]] here.\n", encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "learn/L", citer_is_learn=True)
    c = _campaign(vault, item)
    c.work(item)

    body = _read(vault)
    assert "[[learn/L]]" in body and PROVENANCE in body
    assert c.verify(item) is True


@pytest.mark.parametrize(
    "before,after",
    [
        ("See [[person/Ghost]] here.\n", "See here.\n"),
        ("Spoke to [[person/Ghost]].\n", "Spoke to.\n"),
        ("[[person/Ghost]] called.\n", "called.\n"),
        ("Called [[person/Ghost]]\n", "Called\n"),
        ("A [[person/Ghost]] B [[person/Ghost]] C\n", "A B C\n"),
        ("one\n[[person/Ghost]]\ntwo\n", "one\n\ntwo\n"),
    ],
)
def test_whitespace_healing_survives_the_wrap_tolerant_matcher(
    tmp_path: Path, before: str, after: str,
) -> None:
    """Carried over verbatim from the shipped pins. A wrap-tolerant matcher
    whose whitespace class now spans newlines could easily start eating line
    breaks — the last case is the one that would catch it."""
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        f"---\ntype: note\n---\n\n{before}", encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "person/Ghost",
                                      citer_is_learn=False)
    c = _campaign(vault, item)
    c.work(item)

    assert _read(vault) == f"---\ntype: note\n---\n\n{after}"


# ---------------------------------------------------------------------------
# valid-YAML guard — refuse to write a corrupt record
# ---------------------------------------------------------------------------


def test_a_mutation_that_would_corrupt_the_frontmatter_is_refused(
    tmp_path: Path,
) -> None:
    """Measured, not assumed: annotating a link inside an unquoted PLAIN scalar
    injects ``": "`` into it and PyYAML then reports 'mapping values are not
    allowed here'. Writing that would convert a broken link into an unparseable
    record. The mutation must refuse and raise, which the runner records as
    FAILED — visible, and the file is left exactly as it was.
    """
    vault = _vault(tmp_path)
    original = "---\ntitle: See [[learn/L]] here\ntype: note\n---\n\nBody.\n"
    (vault / "note" / "R.md").write_text(original, encoding="utf-8")
    item = Link001Campaign.build_item("note/R.md", "learn/L", citer_is_learn=True)
    c = _campaign(vault, item)

    with pytest.raises(ValueError, match="YAML"):
        c.work(item)

    assert _read(vault) == original, "the refused mutation touched nothing"


def test_the_guard_does_not_fire_on_a_record_whose_yaml_was_already_broken(
    tmp_path: Path,
) -> None:
    """The guard compares BEFORE against AFTER. A record that already had
    unparseable frontmatter must still be repairable — otherwise the guard
    quarantines exactly the records most in need of the sweep."""
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        "---\nbroken: [unclosed\n---\n\nSee [[person/Ghost]] here.\n",
        encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "person/Ghost",
                                      citer_is_learn=False)
    c = _campaign(vault, item)

    c.work(item)   # must not raise

    assert "person/Ghost" not in extract_wikilinks(_read(vault))


def test_the_valid_yaml_guard_logs_what_it_refused(tmp_path: Path) -> None:
    """Observability pin driving the production path (builder rule 9)."""
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        "---\ntitle: See [[learn/L]] here\ntype: note\n---\n\nBody.\n",
        encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "learn/L", citer_is_learn=True)
    c = _campaign(vault, item)

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(ValueError):
            c.work(item)

    events = [e for e in captured if e.get("event") == "drip.link001.yaml_guard"]
    assert len(events) == 1
    assert events[0]["path"] == "note/R.md"
    assert events[0]["branch"] == "annotate"
    assert events[0]["campaign"] == "link001_repair"
