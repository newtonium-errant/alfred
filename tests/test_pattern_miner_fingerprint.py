"""Fingerprint-stability tests for the Phase 4 pattern miner.

Pins the contract from project_kalle_phase4_pattern_miner.md (Q7):
fingerprint stays stable across mine runs as long as BOTH:
  - the sorted member-file list is unchanged
  - the sorted label tuple is unchanged

Either changing yields a new fingerprint and a new proposal opportunity
(the supersede path in the lifecycle).
"""

from __future__ import annotations

from alfred.distiller.pattern_miner import fingerprint_cluster


class TestFingerprintStability:
    def test_same_inputs_yield_same_hash(self) -> None:
        members = ["a.md", "b.md", "c.md"]
        labels = ["topic/x", "topic/y"]
        fp1 = fingerprint_cluster(members, labels)
        fp2 = fingerprint_cluster(members, labels)
        assert fp1 == fp2

    def test_member_order_does_not_affect_fingerprint(self) -> None:
        # Surveyor doesn't guarantee a stable order; sorting before
        # hashing protects against false fingerprint changes.
        fp_a = fingerprint_cluster(
            ["a.md", "b.md", "c.md"], ["topic/x"],
        )
        fp_b = fingerprint_cluster(
            ["c.md", "a.md", "b.md"], ["topic/x"],
        )
        assert fp_a == fp_b

    def test_label_order_does_not_affect_fingerprint(self) -> None:
        fp_a = fingerprint_cluster(
            ["a.md"], ["topic/x", "topic/y"],
        )
        fp_b = fingerprint_cluster(
            ["a.md"], ["topic/y", "topic/x"],
        )
        assert fp_a == fp_b

    def test_empty_inputs_yield_stable_hash(self) -> None:
        fp1 = fingerprint_cluster([], [])
        fp2 = fingerprint_cluster([], [])
        assert fp1 == fp2
        # Sanity — non-empty.
        assert fp1


class TestFingerprintChange:
    def test_added_member_changes_fingerprint(self) -> None:
        fp_before = fingerprint_cluster(
            ["a.md", "b.md"], ["topic/x"],
        )
        fp_after = fingerprint_cluster(
            ["a.md", "b.md", "c.md"], ["topic/x"],
        )
        assert fp_before != fp_after

    def test_removed_member_changes_fingerprint(self) -> None:
        fp_before = fingerprint_cluster(
            ["a.md", "b.md", "c.md"], ["topic/x"],
        )
        fp_after = fingerprint_cluster(
            ["a.md", "b.md"], ["topic/x"],
        )
        assert fp_before != fp_after

    def test_changed_label_changes_fingerprint(self) -> None:
        fp_before = fingerprint_cluster(
            ["a.md"], ["topic/x"],
        )
        fp_after = fingerprint_cluster(
            ["a.md"], ["topic/y"],
        )
        assert fp_before != fp_after

    def test_added_label_changes_fingerprint(self) -> None:
        fp_before = fingerprint_cluster(
            ["a.md"], ["topic/x"],
        )
        fp_after = fingerprint_cluster(
            ["a.md"], ["topic/x", "topic/y"],
        )
        assert fp_before != fp_after


class TestFingerprintShape:
    def test_returns_hex_sha256(self) -> None:
        fp = fingerprint_cluster(["a.md"], ["topic/x"])
        # SHA-256 hex is 64 chars, all hex digits.
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_label_collision_with_member_path_does_not_collide(self) -> None:
        # The "\n--\n" divider keeps the two halves unambiguous so a
        # label that happens to look like a member path can't collide
        # with that member.
        fp_a = fingerprint_cluster(
            ["topic/x"], [],  # member path, empty labels
        )
        fp_b = fingerprint_cluster(
            [], ["topic/x"],  # empty members, same string as label
        )
        assert fp_a != fp_b
