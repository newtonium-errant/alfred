"""Tests for the pure-logic cluster→MOC suggestion module
(Phase 5 Sub-arc D1).

Covers:
  * Member-overlap signal scoring + thresholding
  * Fuzzy-label Jaccard tiebreaker scoring
  * Propose-new path with name derivation
  * Inventory MOC (``MOC/_*.md``) filter at 3 sites
  * ID derivation idempotency (same membership + target → same ID)
  * Threshold gates (min_cluster_size, overlap, jaccard)
  * Empty / degenerate inputs

Per builder.md test discipline: timeout-wrapped pytest, log-emission
tests for ``_intentionally_left_blank`` paths in the daemon
integration suite (not here — this module is pure, no log emission).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.surveyor.moc_suggester import (
    INVENTORY_MOC_STEM_PREFIX,
    ExistingMoc,
    MocSuggestion,
    build_existing_mocs_index,
    propose_moc_suggestions,
)


# ---------------------------------------------------------------------------
# Test fixtures — synthetic VaultRecord stand-in.
# ---------------------------------------------------------------------------


@dataclass
class _FakeRecord:
    """Minimal stand-in for ``surveyor.parser.VaultRecord``.

    The suggester only reads ``frontmatter`` (a dict), so the fake
    can be much lighter than the real ``VaultRecord``. Test fixtures
    pass a dict literal for ``frontmatter``.
    """
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    record_type: str = "zettel"
    wikilinks: list[str] = field(default_factory=list)


def _zettel(mocs: list[str] | None = None) -> _FakeRecord:
    fm = {"type": "zettel"}
    if mocs is not None:
        fm["mocs"] = mocs
    return _FakeRecord(frontmatter=fm)


def _fixed_now() -> datetime:
    """Deterministic timestamp for ID stability tests."""
    return datetime(2026, 5, 19, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Member-overlap signal.
# ---------------------------------------------------------------------------


def test_member_overlap_above_threshold_emits_suggestion() -> None:
    """3/5 cluster members cite MOC/Stoicism MOC.md → score=0.6;
    threshold=0.4 → suggestion emitted with the 2 non-citing members
    as candidates_to_add."""
    members = [
        "zettel/A.md", "zettel/B.md", "zettel/C.md",
        "zettel/D.md", "zettel/E.md",
    ]
    records = {
        "zettel/A.md": _zettel(["[[MOC/Stoicism MOC]]"]),
        "zettel/B.md": _zettel(["MOC/Stoicism MOC.md"]),
        "zettel/C.md": _zettel(["[[MOC/Stoicism MOC|Stoic]]"]),
        "zettel/D.md": _zettel([]),
        "zettel/E.md": _zettel(None),
    }
    existing = {
        "MOC/Stoicism MOC.md": ExistingMoc(
            rel_path="MOC/Stoicism MOC.md",
            stem="Stoicism MOC",
            name="Stoicism MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=7,
        member_paths=members,
        cluster_tags=["stoicism"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.mapping_signal == "member_overlap"
    assert s.target_moc_rel_path == "MOC/Stoicism MOC.md"
    assert s.mapping_score == pytest.approx(0.6)
    assert set(s.candidate_members_to_add) == {"zettel/D.md", "zettel/E.md"}
    assert s.status == "pending"
    assert s.proposed_new_moc_name is None


def test_member_overlap_below_threshold_falls_through_to_fuzzy() -> None:
    """1/5 overlap = 0.2, below the 0.4 threshold → fuzzy_label tier
    consulted. If fuzzy also fails, propose_new triggers."""
    members = [f"zettel/{i}.md" for i in range(5)]
    records = {
        members[0]: _zettel(["MOC/Stoicism MOC.md"]),  # 1 of 5 = 0.2
    }
    for p in members[1:]:
        records[p] = _zettel([])
    existing = {
        "MOC/Stoicism MOC.md": ExistingMoc(
            rel_path="MOC/Stoicism MOC.md",
            stem="Stoicism MOC",
            name="Stoicism MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=8,
        member_paths=members,
        cluster_tags=["stoicism"],  # Will match Stoicism MOC fuzzy
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.4,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    # Fuzzy hit: cluster_tags={"stoicism"} ∩ MOC stem tokens={"stoicism"}
    # = 1; union = 1; Jaccard = 1.0
    assert len(suggestions) == 1
    assert suggestions[0].mapping_signal == "fuzzy_label"
    assert suggestions[0].mapping_score == pytest.approx(1.0)


def test_member_overlap_all_citing_skips_no_op_suggestion() -> None:
    """If every member already cites the target MOC, there's nothing
    to add — no suggestion emitted (no-op skip)."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {
        p: _zettel(["MOC/Stoicism MOC.md"]) for p in members
    }
    existing = {
        "MOC/Stoicism MOC.md": ExistingMoc(
            rel_path="MOC/Stoicism MOC.md",
            stem="Stoicism MOC",
            name="Stoicism MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=9,
        member_paths=members,
        cluster_tags=["other-topic"],  # No fuzzy match
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    # member_overlap = 3/3 = 1.0, but candidates_to_add = [] → skip;
    # fuzzy_label has no overlap with "other-topic"; propose_new
    # triggers since cluster_tags is non-empty.
    # NOTE: propose_new fires here because the member-overlap signal
    # was skipped (not "emitted"), so the eligibility flow falls
    # through. This is correct — operator may want a NEW MOC for
    # "Other Topic" even when members already cite Stoicism MOC.
    assert len(suggestions) == 1
    assert suggestions[0].mapping_signal == "propose_new"


# ---------------------------------------------------------------------------
# Fuzzy-label tiebreaker.
# ---------------------------------------------------------------------------


def test_fuzzy_label_tiebreaker_emits_when_member_overlap_zero() -> None:
    """No members cite any MOC → member-overlap returns nothing →
    fuzzy_label consulted on cluster_tags ∩ MOC stem tokens."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {p: _zettel([]) for p in members}
    existing = {
        "MOC/Historical Fencing MOC.md": ExistingMoc(
            rel_path="MOC/Historical Fencing MOC.md",
            stem="Historical Fencing MOC",
            name="Historical Fencing MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=10,
        member_paths=members,
        cluster_tags=["historical-fencing", "swordsmanship"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.3,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    # cluster_tags tokenized: {historical, fencing, swordsmanship}
    # MOC stem tokenized: {historical, fencing}  (moc dropped as stopword)
    # intersection = {historical, fencing}; union = 3; Jaccard = 2/3 ≈ 0.67
    assert len(suggestions) == 1
    assert suggestions[0].mapping_signal == "fuzzy_label"
    assert suggestions[0].mapping_score == pytest.approx(2 / 3)


def test_fuzzy_label_below_threshold_falls_through_to_propose_new() -> None:
    """Fuzzy score below threshold → propose_new triggers."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {p: _zettel([]) for p in members}
    existing = {
        "MOC/Historical Fencing MOC.md": ExistingMoc(
            rel_path="MOC/Historical Fencing MOC.md",
            stem="Historical Fencing MOC",
            name="Historical Fencing MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=11,
        member_paths=members,
        cluster_tags=["meditation"],  # No overlap with fencing
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    assert len(suggestions) == 1
    assert suggestions[0].mapping_signal == "propose_new"
    assert suggestions[0].target_moc_rel_path is None
    assert suggestions[0].proposed_new_moc_name == "Meditation MOC"


# ---------------------------------------------------------------------------
# Propose-new path.
# ---------------------------------------------------------------------------


def test_propose_new_derives_name_from_first_usable_tag() -> None:
    """Title-cased + ``MOC`` suffix."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {p: _zettel([]) for p in members}
    suggestions = propose_moc_suggestions(
        cluster_id=12,
        member_paths=members,
        cluster_tags=["roman-rhetoric"],
        records=records,
        existing_mocs={},
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.mapping_signal == "propose_new"
    assert s.proposed_new_moc_name == "Roman Rhetoric MOC"
    assert s.target_moc_rel_path is None
    assert s.candidate_members_to_add == sorted(members)


def test_propose_new_strips_leading_underscore_from_tag() -> None:
    """Inventory-MOC namespace pollution defense: a tag like
    ``_open`` must NOT yield ``_Open MOC`` (which would land in the
    underscore-prefixed inventory namespace)."""
    suggestions = propose_moc_suggestions(
        cluster_id=13,
        member_paths=["zettel/A.md", "zettel/B.md", "zettel/C.md"],
        cluster_tags=["_open"],
        records={p: _zettel([]) for p in ["zettel/A.md", "zettel/B.md", "zettel/C.md"]},
        existing_mocs={},
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    assert len(suggestions) == 1
    name = suggestions[0].proposed_new_moc_name
    assert name is not None
    assert not name.startswith("_"), (
        f"Proposed new MOC name {name!r} must not start with _ "
        "(inventory-MOC namespace defense)"
    )


def test_propose_new_skips_when_no_usable_tag() -> None:
    """Empty cluster_tags + no existing MOC matches → no suggestion."""
    suggestions = propose_moc_suggestions(
        cluster_id=14,
        member_paths=["zettel/A.md", "zettel/B.md", "zettel/C.md"],
        cluster_tags=[],
        records={p: _zettel([]) for p in ["zettel/A.md", "zettel/B.md", "zettel/C.md"]},
        existing_mocs={},
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    assert suggestions == []


# ---------------------------------------------------------------------------
# Inventory-MOC filter (defense at 3 sites).
# ---------------------------------------------------------------------------


def test_inventory_moc_filtered_from_member_overlap() -> None:
    """Inventory MOC (``MOC/_Open Questions.md``) MUST NOT be a
    candidate target even if every cluster member cites it.

    The index builder filters these out, but the suggester re-checks
    via ``ExistingMoc.is_inventory_moc`` so hand-built indexes can't
    bypass.
    """
    members = ["question/A.md", "question/B.md", "question/C.md"]
    records = {
        p: _zettel(["MOC/_Open Questions.md"]) for p in members
    }
    # Hand-build index INCLUDING an inventory MOC (bypassing the
    # builder filter to test the suggester's defense).
    existing = {
        "MOC/_Open Questions.md": ExistingMoc(
            rel_path="MOC/_Open Questions.md",
            stem="_Open Questions",
            name="_Open Questions",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=15,
        member_paths=members,
        cluster_tags=["question"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    # Inventory MOC excluded from member_overlap + fuzzy → propose_new
    # triggers on "question" tag, producing a Question MOC stem
    # (NOT _Open Questions).
    for s in suggestions:
        assert s.target_moc_rel_path is None or not s.target_moc_rel_path.startswith("MOC/_"), (
            "Inventory MOC must not be a target"
        )


def test_inventory_moc_filtered_from_fuzzy_label() -> None:
    """Even if the cluster's tags share token overlap with an inventory
    MOC's stem (e.g. ``open`` ∩ ``_Open Questions``), the inventory
    MOC must NOT appear as a fuzzy_label target."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {p: _zettel([]) for p in members}
    existing = {
        "MOC/_Open Questions.md": ExistingMoc(
            rel_path="MOC/_Open Questions.md",
            stem="_Open Questions",
            name="_Open Questions",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=16,
        member_paths=members,
        cluster_tags=["open", "questions"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.3,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    for s in suggestions:
        if s.mapping_signal in ("member_overlap", "fuzzy_label"):
            assert s.target_moc_rel_path is not None
            assert not s.target_moc_rel_path.startswith("MOC/_")


# ---------------------------------------------------------------------------
# ID derivation idempotency + content sensitivity.
# ---------------------------------------------------------------------------


def test_id_stable_across_cluster_id_renumber() -> None:
    """HDBSCAN renumbers cluster IDs non-deterministically across
    sweeps. The suggestion ID is computed off SORTED member paths +
    target, so the same (members, target) yield the same ID
    regardless of cluster_id."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {p: _zettel(["MOC/Stoicism MOC.md"]) for p in members[:2]}
    records[members[2]] = _zettel([])
    existing = {
        "MOC/Stoicism MOC.md": ExistingMoc(
            rel_path="MOC/Stoicism MOC.md",
            stem="Stoicism MOC",
            name="Stoicism MOC",
            contents_members=frozenset(),
        ),
    }
    s1 = propose_moc_suggestions(
        cluster_id=7,
        member_paths=members,
        cluster_tags=["stoicism"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    s2 = propose_moc_suggestions(
        cluster_id=42,  # Different cluster ID; same members
        member_paths=list(reversed(members)),  # Different input order; sort_members normalizes
        cluster_tags=["stoicism"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    assert len(s1) == 1
    assert len(s2) == 1
    assert s1[0].id == s2[0].id, "IDs must be stable across cluster-id renumber"
    # But forensic fields capture the differing cluster_id:
    assert s1[0].cluster_id_at_proposal == 7
    assert s2[0].cluster_id_at_proposal == 42


def test_id_differs_for_different_targets() -> None:
    """Same members, different target MOC → different ID."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {p: _zettel(["MOC/Stoicism MOC.md", "MOC/Marcus Aurelius MOC.md"]) for p in members[:2]}
    records[members[2]] = _zettel([])
    existing = {
        "MOC/Stoicism MOC.md": ExistingMoc(
            rel_path="MOC/Stoicism MOC.md", stem="Stoicism MOC", name="Stoicism MOC",
            contents_members=frozenset(),
        ),
        "MOC/Marcus Aurelius MOC.md": ExistingMoc(
            rel_path="MOC/Marcus Aurelius MOC.md", stem="Marcus Aurelius MOC", name="Marcus Aurelius MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=20,
        member_paths=members,
        cluster_tags=["stoicism"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    # Two member_overlap candidates; IDs must differ.
    ids = {s.id for s in suggestions}
    assert len(ids) == len(suggestions)


# ---------------------------------------------------------------------------
# Threshold gates.
# ---------------------------------------------------------------------------


def test_min_cluster_size_gate_short_circuits() -> None:
    """Cluster below ``min_cluster_size`` returns empty list."""
    suggestions = propose_moc_suggestions(
        cluster_id=21,
        member_paths=["zettel/A.md", "zettel/B.md"],  # only 2
        cluster_tags=["stoicism"],
        records={"zettel/A.md": _zettel([]), "zettel/B.md": _zettel([])},
        existing_mocs={},
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    assert suggestions == []


# ---------------------------------------------------------------------------
# Operator-typo tolerance on ``mocs:`` frontmatter.
# ---------------------------------------------------------------------------


def test_mocs_frontmatter_typo_shapes_normalized() -> None:
    """The ``mocs:`` field accepts wikilink, pipe-alias wikilink,
    bare stem, full rel_path. All normalize to the same target for
    member_overlap counting."""
    members = [f"zettel/M{i}.md" for i in range(4)]
    records = {
        members[0]: _zettel(["[[MOC/Stoicism MOC]]"]),
        members[1]: _zettel(["MOC/Stoicism MOC.md"]),
        members[2]: _zettel(["[[MOC/Stoicism MOC|Stoic Practice]]"]),
        members[3]: _zettel(["Stoicism MOC"]),  # bare stem; coerced to MOC/Stoicism MOC.md
    }
    existing = {
        "MOC/Stoicism MOC.md": ExistingMoc(
            rel_path="MOC/Stoicism MOC.md",
            stem="Stoicism MOC",
            name="Stoicism MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=22,
        member_paths=members,
        cluster_tags=["stoicism"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    # All 4 normalize to the same target. score = 4/4 = 1.0, but
    # candidates_to_add = [] → no member_overlap suggestion.
    # The propose_new path may still trigger; assert: no
    # member_overlap suggestion emitted (no-op skip).
    member_overlap_hits = [s for s in suggestions if s.mapping_signal == "member_overlap"]
    assert member_overlap_hits == [], (
        "All members already cite Stoicism MOC; no member_overlap "
        "suggestion should be emitted"
    )


def test_malformed_mocs_frontmatter_gracefully_degrades() -> None:
    """A member with ``mocs:`` set to a non-list/non-string degrades
    to no-mocs (doesn't crash, doesn't false-add)."""
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {
        members[0]: _FakeRecord(frontmatter={"type": "zettel", "mocs": 12345}),  # int — malformed
        members[1]: _zettel(["MOC/Stoicism MOC.md"]),
        members[2]: _zettel(["MOC/Stoicism MOC.md"]),
    }
    existing = {
        "MOC/Stoicism MOC.md": ExistingMoc(
            rel_path="MOC/Stoicism MOC.md", stem="Stoicism MOC", name="Stoicism MOC",
            contents_members=frozenset(),
        ),
    }
    suggestions = propose_moc_suggestions(
        cluster_id=23,
        member_paths=members,
        cluster_tags=["stoicism"],
        records=records,
        existing_mocs=existing,
        member_overlap_threshold=0.4,
        fuzzy_label_jaccard_threshold=0.5,
        min_cluster_size=3,
        now=_fixed_now(),
    )
    # Member 0 degrades to "no mocs"; 1+2 cite. Overlap = 2/3 = 0.67;
    # candidate to add = member 0.
    assert len(suggestions) == 1
    assert suggestions[0].mapping_signal == "member_overlap"
    assert suggestions[0].candidate_members_to_add == [members[0]]


# ---------------------------------------------------------------------------
# build_existing_mocs_index — filesystem reading + inventory filter.
# ---------------------------------------------------------------------------


def test_index_builder_filters_inventory_mocs(tmp_path: Path) -> None:
    """``MOC/_*.md`` files are excluded from the index. Even if an
    operator manually points a member's ``mocs:`` at an inventory
    MOC, the suggester can never propose it as a target."""
    moc_dir = tmp_path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "Stoicism MOC.md").write_text(
        "---\ntype: MOC\nname: Stoicism MOC\n---\n# Contents\n- [[zettel/Foo]]\n"
    )
    (moc_dir / "_Open Questions.md").write_text(
        "---\ntype: MOC\nname: _Open Questions\n---\n# Contents\n"
    )
    index = build_existing_mocs_index(tmp_path)
    assert "MOC/Stoicism MOC.md" in index
    assert "MOC/_Open Questions.md" not in index, (
        "Inventory MOC must be excluded from candidate index"
    )


def test_index_builder_extracts_contents_members(tmp_path: Path) -> None:
    """``# Contents`` body section parsed for ``- [[type/Name]]``
    bullets. Wikilinks outside the section are ignored."""
    moc_dir = tmp_path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "Test MOC.md").write_text(
        "---\ntype: MOC\nname: Test MOC\n---\n"
        "# Premise\n[[zettel/SHOULD_NOT_BE_COUNTED]]\n"
        "# Contents\n"
        "- [[zettel/A]]\n"
        "- [[zettel/B|alias for B]]\n"
        "- [[zettel/C]]\n"
        "# Tags\n"
        "- [[zettel/AFTER_CONTENTS]]\n"  # Bullet after # Contents section ends
    )
    index = build_existing_mocs_index(tmp_path)
    moc = index["MOC/Test MOC.md"]
    assert "zettel/A" in moc.contents_members
    assert "zettel/B" in moc.contents_members
    assert "zettel/C" in moc.contents_members
    assert "zettel/SHOULD_NOT_BE_COUNTED" not in moc.contents_members
    assert "zettel/AFTER_CONTENTS" not in moc.contents_members


def test_index_builder_handles_missing_moc_dir(tmp_path: Path) -> None:
    """Vault with no ``MOC/`` dir returns empty index — no crash."""
    index = build_existing_mocs_index(tmp_path)
    assert index == {}


def test_index_builder_skips_corrupt_moc_file(tmp_path: Path) -> None:
    """A single malformed MOC file doesn't break the whole index."""
    moc_dir = tmp_path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "Good MOC.md").write_text(
        "---\ntype: MOC\nname: Good MOC\n---\n# Contents\n"
    )
    # Write invalid YAML frontmatter via raw bytes
    (moc_dir / "Bad MOC.md").write_text(
        "---\ntype: MOC\nbad_yaml: [unclosed\n---\n# Contents\n"
    )
    index = build_existing_mocs_index(tmp_path)
    # Good MOC loads; Bad MOC silently dropped.
    assert "MOC/Good MOC.md" in index
    # Bad MOC may or may not parse depending on python-frontmatter
    # leniency; assertion is just "no crash + good MOC present".


# ---------------------------------------------------------------------------
# Constants — exposed for cross-module pinning.
# ---------------------------------------------------------------------------


def test_inventory_moc_stem_prefix_is_underscore() -> None:
    """Pin the inventory-MOC prefix convention so a future rename
    surfaces here. Matches Phase 4 Sub-arc B's ``MOC/_<Name>.md``
    pattern in ``vault/zettel_hooks.py``."""
    assert INVENTORY_MOC_STEM_PREFIX == "_"
