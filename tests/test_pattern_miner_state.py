"""State-loader tests for the Phase 4 pattern miner.

Pins the load() schema-tolerance contract from CLAUDE.md, the atomic-
write semantics, and the four-state status lifecycle from the design
memo (Q7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from alfred.distiller.pattern_miner_state import (
    PatternMinerState,
    ProposalEntry,
    STATUS_DISCARDED,
    STATUS_PENDING,
    STATUS_PROMOTED,
    STATUS_SUPERSEDED,
)


# ---------------------------------------------------------------------------
# from_dict — schema-tolerance contract
# ---------------------------------------------------------------------------


class TestProposalEntryFromDict:
    def test_known_fields_load(self) -> None:
        entry = ProposalEntry.from_dict({
            "fingerprint": "abc123",
            "cluster_id": "semantic_5",
            "labels": ["llm/quantization"],
            "member_count": 11,
            "proposed_at": "2026-05-10T12:00:00+00:00",
            "proposed_path": "inbox/proposed-canonical/llm-quantization.md",
            "proposed_slug": "llm-quantization",
            "proposed_canonical_type": "principles",
            "status": "pending",
        })
        assert entry.fingerprint == "abc123"
        assert entry.cluster_id == "semantic_5"
        assert entry.labels == ["llm/quantization"]
        assert entry.member_count == 11
        assert entry.proposed_canonical_type == "principles"
        assert entry.status == "pending"

    def test_unknown_fields_silently_dropped(self) -> None:
        # Per CLAUDE.md "State persistence — load() schema-tolerance
        # contract": a future-version state file with extra fields
        # silently ignores them on rollback.
        entry = ProposalEntry.from_dict({
            "fingerprint": "abc",
            "cluster_id": "c1",
            "future_field_v2": "should not crash",
            "another_unknown": 42,
        })
        assert entry.fingerprint == "abc"
        assert entry.cluster_id == "c1"
        # No crash; unknown fields ignored.

    def test_missing_fields_get_defaults(self) -> None:
        entry = ProposalEntry.from_dict({"fingerprint": "abc"})
        assert entry.fingerprint == "abc"
        assert entry.cluster_id == ""
        assert entry.labels == []
        assert entry.member_count == 0
        assert entry.status == STATUS_PENDING

    def test_empty_dict_yields_default_entry(self) -> None:
        entry = ProposalEntry.from_dict({})
        assert entry.fingerprint == ""
        assert entry.status == STATUS_PENDING


class TestProposalEntryToDict:
    def test_round_trip(self) -> None:
        original = ProposalEntry(
            fingerprint="abc123",
            cluster_id="c1",
            labels=["topic/x"],
            member_count=5,
            proposed_at="2026-05-10T12:00:00+00:00",
            proposed_path="inbox/p/x.md",
            proposed_slug="topic-x",
            proposed_canonical_type="architecture",
            status=STATUS_PROMOTED,
        )
        as_dict = original.to_dict()
        restored = ProposalEntry.from_dict(as_dict)
        assert restored == original

    def test_invalid_status_normalizes_to_pending(self) -> None:
        # Defensive: a state file with a bogus status string should
        # round-trip back as pending rather than poison the lifecycle.
        entry = ProposalEntry(fingerprint="abc", status="nonsense")
        as_dict = entry.to_dict()
        assert as_dict["status"] == STATUS_PENDING


# ---------------------------------------------------------------------------
# Per-action fields (promote-proposal / discard-proposal CLI, 2026-05-11).
# Closes 3 deferred follow-ups from the Phase 4 first-promote review.
# ---------------------------------------------------------------------------


class TestPerActionFieldsSchema:
    """Pin the four new fields (promoted_to, promoted_at, discarded_at,
    discarded_reason) round-trip cleanly + schema-tolerance still
    drops genuinely unknown fields not in the new set.
    """

    def test_promoted_fields_round_trip(self) -> None:
        original = ProposalEntry(
            fingerprint="abc123",
            cluster_id="c1",
            proposed_slug="topic-x",
            proposed_canonical_type="architecture",
            status=STATUS_PROMOTED,
            promoted_to="architecture/topic-x.md",
            promoted_at="2026-05-11T10:00:00+00:00",
        )
        restored = ProposalEntry.from_dict(original.to_dict())
        assert restored.promoted_to == "architecture/topic-x.md"
        assert restored.promoted_at == "2026-05-11T10:00:00+00:00"
        # Unset fields stay empty.
        assert restored.discarded_at == ""
        assert restored.discarded_reason == ""

    def test_discarded_fields_round_trip(self) -> None:
        from alfred.distiller.pattern_miner_state import STATUS_DISCARDED

        original = ProposalEntry(
            fingerprint="def456",
            cluster_id="c2",
            proposed_slug="topic-y",
            status=STATUS_DISCARDED,
            discarded_at="2026-05-11T11:00:00+00:00",
            discarded_reason="overlaps with principles/foo.md",
        )
        restored = ProposalEntry.from_dict(original.to_dict())
        assert restored.discarded_at == "2026-05-11T11:00:00+00:00"
        assert restored.discarded_reason == "overlaps with principles/foo.md"
        assert restored.promoted_to == ""
        assert restored.promoted_at == ""

    def test_legacy_entry_without_per_action_fields_loads_clean(
        self,
    ) -> None:
        # Pre-2026-05-11 state files have no per-action fields. The
        # schema-tolerance filter MUST default them to empty strings
        # rather than crash on missing keys.
        entry = ProposalEntry.from_dict({
            "fingerprint": "legacy",
            "cluster_id": "c1",
            "proposed_slug": "old-proposal",
            "status": "promoted",  # already-acted via reconcile sweep
            # NO promoted_to / promoted_at / discarded_at /
            # discarded_reason — legacy schema.
        })
        assert entry.fingerprint == "legacy"
        assert entry.status == "promoted"
        # All four new fields default to empty strings cleanly.
        assert entry.promoted_to == ""
        assert entry.promoted_at == ""
        assert entry.discarded_at == ""
        assert entry.discarded_reason == ""

    def test_unknown_field_still_dropped_after_extension(self) -> None:
        # The schema-tolerance filter still drops fields not in the
        # dataclass — adding the 4 new fields didn't accidentally
        # widen the filter to "accept anything." Pin the existing
        # contract.
        entry = ProposalEntry.from_dict({
            "fingerprint": "abc",
            "promoted_to": "architecture/foo.md",  # new field, kept
            "future_field_v3": "should not survive",  # unknown, dropped
            "really_random": [1, 2, 3],  # unknown, dropped
        })
        assert entry.promoted_to == "architecture/foo.md"
        # No crash; unknown fields silently dropped.
        # (Can't assert "future_field_v3 not present" because dataclasses
        # don't carry arbitrary attrs, but the constructor would have
        # raised TypeError if the filter let unknown keys through.)

    def test_to_dict_emits_all_four_new_fields(self) -> None:
        # to_dict MUST include the 4 new fields so a save → load
        # round-trip preserves them. Pin the on-disk shape.
        entry = ProposalEntry(fingerprint="abc")
        as_dict = entry.to_dict()
        assert "promoted_to" in as_dict
        assert "promoted_at" in as_dict
        assert "discarded_at" in as_dict
        assert "discarded_reason" in as_dict
        # Default values for an unaction'd entry.
        assert as_dict["promoted_to"] == ""
        assert as_dict["promoted_at"] == ""
        assert as_dict["discarded_at"] == ""
        assert as_dict["discarded_reason"] == ""


# ---------------------------------------------------------------------------
# PatternMinerState.load — missing file + schema-tolerance
# ---------------------------------------------------------------------------


class TestStateLoad:
    def test_load_missing_file_emits_no_existing_state_log(
        self, tmp_path: Path,
    ) -> None:
        # Per the universal "intentionally left blank" rule: log the
        # first-run case so observers can distinguish first-run from
        # broken-load.
        state_path = tmp_path / "pattern_miner_state.json"
        state = PatternMinerState(state_path)
        with structlog.testing.capture_logs() as captured:
            state.load()
        events = [c for c in captured if c.get("event") == "pattern_miner_state.no_existing_state"]
        assert len(events) == 1
        assert events[0]["path"] == str(state_path)
        # In-memory state stays empty.
        assert state.proposals == {}

    def test_load_existing_file_emits_loaded_log_with_count(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text(json.dumps({
            "version": 1,
            "last_run": "2026-05-10T00:00:00+00:00",
            "proposals": {
                "fp1": {
                    "fingerprint": "fp1", "cluster_id": "c1",
                    "labels": ["x"], "member_count": 3,
                    "status": "pending",
                },
                "fp2": {
                    "fingerprint": "fp2", "cluster_id": "c2",
                    "labels": ["y"], "member_count": 4,
                    "status": "promoted",
                },
            },
        }))
        state = PatternMinerState(state_path)
        with structlog.testing.capture_logs() as captured:
            state.load()
        events = [c for c in captured if c.get("event") == "pattern_miner_state.loaded"]
        assert len(events) == 1
        assert events[0]["proposals"] == 2
        assert "fp1" in state.proposals
        assert "fp2" in state.proposals
        assert state.proposals["fp2"].status == STATUS_PROMOTED

    def test_load_tolerates_unknown_fields_per_schema_contract(
        self, tmp_path: Path,
    ) -> None:
        # Per CLAUDE.md "State persistence — load() schema-tolerance
        # contract": a state file written by a future version with
        # extra fields silently ignores them on rollback. This is the
        # whole point of the from_dict filter — without it, any future
        # field addition would crash older readers.
        state_path = tmp_path / "s.json"
        state_path.write_text(json.dumps({
            "version": 99,  # future version
            "future_top_level_field": "ignored",
            "proposals": {
                "fp1": {
                    "fingerprint": "fp1", "cluster_id": "c1",
                    "future_proposal_field": "ignored",
                    "labels": ["x"], "member_count": 3,
                },
            },
        }))
        state = PatternMinerState(state_path)
        state.load()  # must not crash
        assert "fp1" in state.proposals

    def test_load_skips_non_dict_proposal_values(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text(json.dumps({
            "proposals": {
                "fp1": "not a dict — corrupted",
                "fp2": {"fingerprint": "fp2", "cluster_id": "c2"},
            },
        }))
        state = PatternMinerState(state_path)
        state.load()
        # fp1 silently skipped; fp2 loaded.
        assert "fp1" not in state.proposals
        assert "fp2" in state.proposals

    def test_load_uses_dict_key_when_entry_fingerprint_missing(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: if the entry's fingerprint field is missing or
        # disagrees with the dict key, the dict key is authoritative.
        state_path = tmp_path / "s.json"
        state_path.write_text(json.dumps({
            "proposals": {
                "fp_real": {
                    # No fingerprint field at all.
                    "cluster_id": "c1",
                    "labels": ["x"],
                },
            },
        }))
        state = PatternMinerState(state_path)
        state.load()
        assert state.proposals["fp_real"].fingerprint == "fp_real"


# ---------------------------------------------------------------------------
# PatternMinerState.save — atomic, idempotent
# ---------------------------------------------------------------------------


class TestStateSave:
    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        state_path = tmp_path / "nested" / "deeper" / "s.json"
        state = PatternMinerState(state_path)
        state.proposals["fp1"] = ProposalEntry(
            fingerprint="fp1", cluster_id="c1",
        )
        state.save()
        assert state_path.is_file()

    def test_save_then_load_round_trip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        state.proposals["fp1"] = ProposalEntry(
            fingerprint="fp1", cluster_id="c1",
            labels=["topic/x"], member_count=5,
            proposed_at="2026-05-10T12:00:00+00:00",
            proposed_path="inbox/p/x.md",
            proposed_slug="topic-x",
            proposed_canonical_type="architecture",
            status=STATUS_PENDING,
        )
        state.save()

        state2 = PatternMinerState(state_path)
        state2.load()
        assert "fp1" in state2.proposals
        loaded = state2.proposals["fp1"]
        assert loaded.cluster_id == "c1"
        assert loaded.member_count == 5
        assert loaded.proposed_canonical_type == "architecture"

    def test_save_writes_via_tmp_then_rename(self, tmp_path: Path) -> None:
        # Atomic-write contract: the .tmp file must be the write target
        # before the rename. After save() the .tmp file is gone (renamed
        # away to the real path).
        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        state.save()
        assert state_path.is_file()
        # The .tmp suffix means "replaces .json" — after rename it's gone.
        tmp_file = state_path.with_suffix(".tmp")
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# Status lifecycle helpers
# ---------------------------------------------------------------------------


class TestStatusLifecycle:
    def test_has_entry_for_fingerprint(self, tmp_path: Path) -> None:
        state = PatternMinerState(tmp_path / "s.json")
        assert not state.has_entry_for_fingerprint("fp1")
        state.record_proposal(ProposalEntry(fingerprint="fp1"))
        assert state.has_entry_for_fingerprint("fp1")

    def test_record_proposal_inserts_keyed_by_fingerprint(
        self, tmp_path: Path,
    ) -> None:
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp1", cluster_id="c1",
        ))
        assert state.proposals["fp1"].cluster_id == "c1"

    def test_mark_status_promoted(self, tmp_path: Path) -> None:
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(fingerprint="fp1"))
        state.mark_status("fp1", STATUS_PROMOTED)
        assert state.proposals["fp1"].status == STATUS_PROMOTED

    def test_mark_status_discarded(self, tmp_path: Path) -> None:
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(fingerprint="fp1"))
        state.mark_status("fp1", STATUS_DISCARDED)
        assert state.proposals["fp1"].status == STATUS_DISCARDED

    def test_supersede_marks_old(self, tmp_path: Path) -> None:
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(fingerprint="fp_old"))
        state.supersede("fp_old")
        assert state.proposals["fp_old"].status == STATUS_SUPERSEDED

    def test_mark_status_invalid_status_logs_and_skips(
        self, tmp_path: Path,
    ) -> None:
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp1", status=STATUS_PENDING,
        ))
        with structlog.testing.capture_logs() as captured:
            state.mark_status("fp1", "bogus")
        events = [c for c in captured if c.get("event") == "pattern_miner_state.invalid_status"]
        assert len(events) == 1
        # Status unchanged.
        assert state.proposals["fp1"].status == STATUS_PENDING

    def test_mark_status_missing_fingerprint_is_noop(
        self, tmp_path: Path,
    ) -> None:
        state = PatternMinerState(tmp_path / "s.json")
        # No entry for fp1 — should be a no-op, not a crash.
        state.mark_status("fp1", STATUS_PROMOTED)
        assert "fp1" not in state.proposals
