"""Surveyor + distiller are fence-aware (#70, the #61 follow-up).

Since #57 the ingest path fences uploaded file content into record bodies,
and the capability SKILL pass now has agents writing fenced CSV into bodies
too — so the fenced population is GROWING. A ``[[thing]]`` inside a bank CSV
is DATA, not a reference.

Janitor was fixed in #61. These two were left fence-blind, and their
severity is different but not zero: surveyor's links feed clusterer →
labeler → ``writer.write_relationships``, so a false link is INDIRECTLY
WRITTEN BACK into the vault as a relationship wikilink; distiller's drive
candidate selection and cluster grouping. No irreversible deletes (that was
janitor's LINK001 risk), but a growing fenced population means growing
false signal that then becomes vault content.

TWO SITE CLASSES, and the split is the point:
  * DOCUMENT TEXT (parse_file x2, backfill) — masked.
  * FRONTMATTER FIELD VALUES (candidates, pipeline) — deliberately blind,
    the same call janitor's annotation site makes: a field value has no
    fences, and one containing backticks is still a real link.
"""

from __future__ import annotations

import pytest

from alfred.distiller import backfill as distiller_backfill
from alfred.distiller import candidates as distiller_candidates
from alfred.distiller import parser as distiller_parser
from alfred.distiller import pipeline as distiller_pipeline
from alfred.janitor.parser import mask_code_regions
from alfred.surveyor import parser as surveyor_parser

# A record whose BODY carries a fenced CSV containing wikilink-shaped text,
# plus one REAL link in the body and one in the frontmatter.
FENCED_RECORD = """---
type: note
project: "[[project/Real Project]]"
---

Some prose linking to [[person/Real Person]].

```csv
name,ref
Widget,[[project/Not A Link]]
Gadget,[[person/Also Not A Link]]
```

Trailing prose with `[[inline/Not A Link]]` in a code span.
"""


def write(tmp_path, text: str = FENCED_RECORD, name: str = "note/rec.md"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Document-text sites — masked
# ---------------------------------------------------------------------------


def test_surveyor_parse_file_ignores_fenced_wikilinks(tmp_path):
    write(tmp_path)
    record = surveyor_parser.parse_file(tmp_path, "note/rec.md")

    assert "person/Real Person" in record.wikilinks
    assert "project/Real Project" in record.wikilinks   # frontmatter survives
    for fake in ("project/Not A Link", "person/Also Not A Link",
                 "inline/Not A Link"):
        assert fake not in record.wikilinks


def test_distiller_parse_file_ignores_fenced_wikilinks(tmp_path):
    write(tmp_path)
    record = distiller_parser.parse_file(tmp_path, "note/rec.md")

    assert "person/Real Person" in record.wikilinks
    assert "project/Real Project" in record.wikilinks
    for fake in ("project/Not A Link", "person/Also Not A Link",
                 "inline/Not A Link"):
        assert fake not in record.wikilinks


def test_distiller_backfill_ignores_fenced_wikilinks(tmp_path):
    """Backfill reads the SAME documents through a different door — leaving
    it unmasked would reintroduce the defect on every backfilled source."""
    path = write(tmp_path, name="session/src.md")
    record = distiller_backfill._parse_source_file(path)

    assert record is not None
    assert "person/Real Person" in record.wikilinks
    for fake in ("project/Not A Link", "person/Also Not A Link"):
        assert fake not in record.wikilinks


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_both_fence_markers_and_grown_fences_are_honoured(tmp_path, fence):
    """CommonMark allows ``~`` as well as backticks, and #57's grown fences
    (````csv wrapping a ``` line) are exactly what the ingest path emits."""
    text = (
        "---\ntype: note\n---\n\nreal [[person/Keep]]\n\n"
        f"{fence}csv\nx,[[person/Drop]]\n{fence}\n"
    )
    write(tmp_path, text)
    record = surveyor_parser.parse_file(tmp_path, "note/rec.md")
    assert "person/Keep" in record.wikilinks
    assert "person/Drop" not in record.wikilinks


def test_frontmatter_links_are_never_masked(tmp_path):
    """Fences are a BODY construct; YAML has none. Masking the frontmatter
    would let a backtick in a field value silence a record's real links."""
    text = (
        "---\ntype: note\nproject: \"[[project/Kept]]\"\n"
        "note: \"a `backtick` value\"\n---\n\nbody\n"
    )
    write(tmp_path, text)
    for mod in (surveyor_parser, distiller_parser):
        record = mod.parse_file(tmp_path, "note/rec.md")
        assert "project/Kept" in record.wikilinks


def test_an_unclosed_fence_does_not_swallow_the_rest(tmp_path):
    """Janitor's deliberate bias, inherited: an unterminated fence is a
    document defect and must not switch link extraction off for everything
    below it."""
    text = (
        "---\ntype: note\n---\n\n```csv\nx,y\n\n"
        "later prose with [[person/Still Found]]\n"
    )
    write(tmp_path, text)
    record = surveyor_parser.parse_file(tmp_path, "note/rec.md")
    assert "person/Still Found" in record.wikilinks


def test_a_record_with_no_fences_is_unchanged(tmp_path):
    """The masking must not cost anything on the common case."""
    text = "---\ntype: note\n---\n\nlink to [[person/A]] and [[org/B]].\n"
    write(tmp_path, text)
    record = surveyor_parser.parse_file(tmp_path, "note/rec.md")
    assert record.wikilinks == ["person/A", "org/B"]


# ---------------------------------------------------------------------------
# Field-value sites — deliberately blind
# ---------------------------------------------------------------------------


def test_candidates_project_link_stays_blind_to_backticks():
    """A frontmatter field value has no fences. One whose link sits inside
    backticks is STILL a real link the operator wrote, and masking would
    blank it away — the same call janitor's annotation site makes.

    The backticks are what make this test able to fail: a plain
    ``[[project/Eagle Farm]]`` survives masking untouched, so a version of
    this test without them passes whether the site is masked or not. Probed
    directly: ``mask_code_regions("`[[project/Eagle Farm]]`")`` yields no
    links, the plain form yields one.
    """
    from alfred.distiller.parser import VaultRecord

    record = VaultRecord(
        rel_path="note/x.md",
        frontmatter={"project": "`[[project/Eagle Farm]]`"},
        body="",
        record_type="note",
    )
    assert distiller_candidates._get_project_link(record) == "Eagle Farm"


def test_pipeline_project_grouping_stays_blind():
    """The sibling field-value site groups learns by project; it must read
    the field as written."""
    from alfred.distiller.parser import VaultRecord

    learns = [
        VaultRecord(
            rel_path=f"learn/{i}.md",
            # Backticked for the same reason as the sibling test above: it is
            # what makes masking observable, and therefore what makes this
            # test capable of failing if the site is wrongly masked.
            frontmatter={"project": ["`[[project/Eagle Farm]]`"]},
            body="",
            record_type="decision",
        )
        for i in range(3)
    ]
    clusters = distiller_pipeline._find_analysis_clusters(learns, min_cluster_size=2)
    assert any(c.get("project") == "Eagle Farm" for c in clusters)


# ---------------------------------------------------------------------------
# The shared rule
# ---------------------------------------------------------------------------


def test_all_three_tools_use_the_SAME_masker():
    """ONE implementation of "is this fenced". Two is how they drift, and
    the drift is only visible after a wrong relationship is already in the
    vault."""
    assert surveyor_parser.mask_code_regions is mask_code_regions
    assert distiller_parser.mask_code_regions is mask_code_regions
    assert distiller_backfill.mask_code_regions is mask_code_regions

    from alfred.drip.campaigns import unfenced_view
    probe = "a ```\n[[x/y]]\n``` b"
    assert unfenced_view(probe) == mask_code_regions(probe)


def test_the_crlf_hardening_rides_along():
    """The ``\\r?`` form in janitor's fence pattern is what makes masking
    work on text that did NOT come through universal-newline decoding. The
    tools now share that hardening by sharing the function — pinned here so
    a future copy-paste cannot silently lose it."""
    crlf = "---\r\ntype: note\r\n---\r\n\r\n```csv\r\nx,[[person/Drop]]\r\n```\r\n"
    masked = mask_code_regions(crlf)
    assert "person/Drop" not in masked


def test_masking_preserves_length_so_offsets_never_shift():
    """Blanking rather than deleting is what lets a later pass measure the
    masked text and apply results to the original."""
    probe = "abc ```x``` def"
    assert len(mask_code_regions(probe)) == len(probe)
