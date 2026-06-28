"""Self-correcting matcher — Phase 1 capture pins.

Covers the ``routine.match_calibration`` pending sink (append/load + schema
tolerance) and the ``cmd_done`` capture hook (low-confidence fuzzy match →
pending row; high-confidence → none; the no-silent-mutation guardrail: the
match path writes ONLY to the pending sink).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from alfred.routine import match_calibration as mc
from alfred.routine.cli import cmd_done
from alfred.routine.config import RoutineConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(vault: Path, tmp_path: Path, *, threshold: float = 0.5) -> RoutineConfig:
    config = RoutineConfig(vault_path=str(vault), instance_name="salem")
    config.state.path = str(tmp_path / "routine_state.json")
    config.match_calibration.pending_path = str(tmp_path / "pending.jsonl")
    config.match_calibration.threshold = threshold
    return config


def _write_routine(vault: Path, name: str, payload: dict) -> Path:
    routine_dir = vault / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm}---\n\n# {name}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Pending sink — append / load / schema tolerance
# ---------------------------------------------------------------------------


def test_append_then_load_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "pending.jsonl"
    mc.append_pending(p, mc.PendingMatch(
        query="walk doggo", matched_to="Walk dog", record="Daily",
        confidence=0.4, completion_date="2026-06-28", captured_at="t",
    ))
    rows = mc.load_pending(p)
    assert len(rows) == 1
    assert rows[0].query == "walk doggo"
    assert rows[0].matched_to == "Walk dog"
    assert rows[0].confidence == 0.4


def test_load_absent_file_is_empty(tmp_path: Path) -> None:
    assert mc.load_pending(tmp_path / "nope.jsonl") == []


def test_load_is_schema_tolerant(tmp_path: Path) -> None:
    """Unknown keys dropped, absent optional keys defaulted, malformed rows
    skipped — the reader degrades gracefully on schema drift / corruption."""
    p = tmp_path / "pending.jsonl"
    p.write_text(
        # extra unknown field + missing optional fields → still loads
        json.dumps({"query": "q", "matched_to": "m", "record": "r",
                    "confidence": 0.3, "future_field": "ignored"}) + "\n"
        # malformed JSON → skipped
        + "{not json\n"
        # missing required field (no 'record') → skipped (TypeError)
        + json.dumps({"query": "q2", "matched_to": "m2", "confidence": 0.1}) + "\n",
        encoding="utf-8",
    )
    rows = mc.load_pending(p)
    assert len(rows) == 1
    assert rows[0].query == "q"
    assert rows[0].completion_date == ""  # absent optional → default


# ---------------------------------------------------------------------------
# cmd_done capture hook — threshold gate + guardrail
# ---------------------------------------------------------------------------


def test_low_confidence_match_is_captured(tmp_path: Path) -> None:
    """A vault-wide fuzzy match below threshold → one pending row with the
    ORIGINAL query (not the canonicalised item)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        # Long item → a 2-token query is a low-Jaccard (conf 0.4) match.
        "items": [{"text": "Walk the dog every morning before work"}],
    })
    config = _config(vault, tmp_path)

    cmd_done(config, "", "walk dog", today_override="2026-06-28")

    rows = mc.load_pending(config.match_calibration.pending_path)
    assert len(rows) == 1
    assert rows[0].query == "walk dog"  # original query, pre-canonicalise
    assert rows[0].matched_to == "Walk the dog every morning before work"
    assert rows[0].record == "Daily"
    assert rows[0].confidence < 0.5
    assert rows[0].completion_date == "2026-06-28"


def test_high_confidence_match_is_not_captured(tmp_path: Path) -> None:
    """An exact (confidence 1.0) match is above threshold → NO pending row."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog"}],
    })
    config = _config(vault, tmp_path)

    cmd_done(config, "", "Walk dog", today_override="2026-06-28")

    assert mc.load_pending(config.match_calibration.pending_path) == []


def test_capture_writes_only_pending_not_a_glossary(tmp_path: Path) -> None:
    """GUARDRAIL: the match path writes ONLY the pending sink — it must NOT
    create/mutate any corpus/glossary file (the glossary is operator-reply
    only). Phase-1 form of the no-silent-mutation invariant."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk the dog every morning before work"}],
    })
    config = _config(vault, tmp_path)
    corpus_guard = tmp_path / "routine_match_corpus.salem.jsonl"

    rc = cmd_done(config, "", "walk dog", today_override="2026-06-28")

    # The completion still succeeded (capture is best-effort, additive).
    assert rc == 0
    # Pending captured; NO corpus file materialised by the match path.
    assert Path(config.match_calibration.pending_path).exists()
    assert not corpus_guard.exists()
