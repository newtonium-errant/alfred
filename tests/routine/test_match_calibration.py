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


# ---------------------------------------------------------------------------
# Phase 2 — corpus (learned glossary) + matcher consultation
# ---------------------------------------------------------------------------


def test_query_key_collapses_phrasings() -> None:
    """Different phrasings of the same completion → the same key (so a learned
    verdict generalises)."""
    assert mc.query_key("I walked the dog") == mc.query_key("walked dog")
    assert mc.query_key("Walk dog") == mc.query_key("dog, walking")


def test_corpus_append_load_last_write_wins(tmp_path: Path) -> None:
    p = tmp_path / "corpus.jsonl"
    qk = mc.query_key("walk dog")
    # reject first, then confirm the SAME pair → confirm wins (last-write).
    mc.append_corpus(p, mc.MatchCorpusEntry(
        type=mc.CORPUS_REJECT, query_key=qk, item_text="Walk dog"))
    mc.append_corpus(p, mc.MatchCorpusEntry(
        type=mc.CORPUS_CONFIRM, query_key=qk, item_text="Walk dog"))
    g = mc.load_glossary(p)
    assert g.verdict(qk, "Walk dog") == "confirm"
    assert (qk, "Walk dog") not in g.rejected


def test_corpus_load_absent_is_empty(tmp_path: Path) -> None:
    g = mc.load_glossary(tmp_path / "nope.jsonl")
    assert g.is_empty()
    assert g.verdict("x", "y") is None


def test_corpus_load_schema_tolerant(tmp_path: Path) -> None:
    p = tmp_path / "corpus.jsonl"
    p.write_text(
        json.dumps({"type": mc.CORPUS_REJECT, "query_key": "k",
                    "item_text": "I", "extra": "ignored"}) + "\n"
        + "{bad json\n",
        encoding="utf-8",
    )
    g = mc.load_glossary(p)
    assert g.verdict("k", "I") == "reject"


def test_matcher_empty_glossary_equals_baseline() -> None:
    """The behavior-preservation pin: empty glossary (or None) → byte-identical
    matcher results to the 2-arg form."""
    from alfred.routine.cli import _matches_item

    g = mc.Glossary(set(), set(), {})
    for q, item in [
        ("walk dog", "Walk the dog every morning before work"),
        ("xyzzy", "Walk dog"),
        ("Walk dog", "Walk dog"),
        ("tilray registration", "Meds"),
    ]:
        assert _matches_item(q, item) == _matches_item(q, item, None)
        assert _matches_item(q, item) == _matches_item(q, item, g)


def test_matcher_reject_short_circuits() -> None:
    """A confirmed-reject pair → matcher returns False even though the fuzzy
    ladder would have matched."""
    from alfred.routine.cli import _matches_item

    item = "Walk the dog every morning before work"
    assert _matches_item("walk dog", item) is True  # fuzzy would match
    g = mc.Glossary(
        confirmed=set(),
        rejected={(mc.query_key("walk dog"), item)},
        aliases={},
    )
    assert _matches_item("walk dog", item, g) is False


def test_matcher_confirm_promotes() -> None:
    """A confirmed-good pair → matcher returns True for a phrasing the fuzzy
    ladder rejects (zero token overlap)."""
    from alfred.routine.cli import _matches_item

    assert _matches_item("tilray registration", "Meds") is False  # fuzzy: no
    g = mc.Glossary(
        confirmed={(mc.query_key("tilray registration"), "Meds")},
        rejected=set(), aliases={},
    )
    assert _matches_item("tilray registration", "Meds", g) is True


def test_corpus_path_default_matches_constant() -> None:
    """Drift-guard: the routine config corpus default binds the shared constant."""
    from alfred.routine.config import MatchCalibrationConfig

    assert MatchCalibrationConfig().corpus_path == mc.DEFAULT_CORPUS_PATH


# ---------------------------------------------------------------------------
# Phase 3 — no-match / alias capture (the false-NEGATIVE half of the loop)
# ---------------------------------------------------------------------------


def test_no_match_floor_default_matches_constant() -> None:
    """Drift-guard: the routine config no_match_floor default binds the constant."""
    from alfred.routine.config import MatchCalibrationConfig

    assert MatchCalibrationConfig().no_match_floor == mc.DEFAULT_NO_MATCH_FLOOR


def test_no_match_captures_closest_candidate(tmp_path: Path) -> None:
    """A completion that matches NOTHING but has a plausible closest candidate
    → one no_match pending row carrying the closest as matched_to."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Feed the cat"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(config, "", "feed the birds", today_override="2026-06-28")

    assert code == 1  # still the unknown_item canary — capture is additive
    rows = mc.load_pending(config.match_calibration.pending_path)
    assert len(rows) == 1
    assert rows[0].kind == mc.KIND_NO_MATCH
    assert rows[0].query == "feed the birds"
    assert rows[0].matched_to == "Feed the cat"  # the closest candidate
    assert rows[0].record == "Daily"
    assert rows[0].confidence >= config.match_calibration.no_match_floor


def test_no_match_below_floor_captures_nothing(tmp_path: Path) -> None:
    """A completion with NO plausible candidate (closest below the floor) →
    no capture (ILB 'nothing close' instead of a bad suggestion)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Feed the cat"}],
    })
    config = _config(vault, tmp_path)

    import structlog
    with structlog.testing.capture_logs() as cap:
        code = cmd_done(config, "", "xyzzy nonexistent", today_override="2026-06-28")

    assert code == 1
    assert mc.load_pending(config.match_calibration.pending_path) == []
    assert [
        c for c in cap
        if c.get("event") == "routine.match_calibration.no_match_nothing_close"
    ]


def test_no_match_empty_vault_captures_nothing(tmp_path: Path) -> None:
    """No active routine items at all → ILB 'nothing close' (reason flagged), no
    capture, no crash."""
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True)
    config = _config(vault, tmp_path)

    import structlog
    with structlog.testing.capture_logs() as cap:
        code = cmd_done(config, "", "feed the birds", today_override="2026-06-28")

    assert code == 1
    assert mc.load_pending(config.match_calibration.pending_path) == []
    nothing = [
        c for c in cap
        if c.get("event") == "routine.match_calibration.no_match_nothing_close"
    ]
    assert nothing and nothing[0].get("reason") == "no_active_items"


def test_no_match_capture_writes_only_pending_not_corpus(tmp_path: Path) -> None:
    """no-silent-alias guardrail: the no-match capture writes ONLY the pending
    sink — it must NOT create/mutate the corpus (aliasing is operator-reply
    only)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Feed the cat"}],
    })
    config = _config(vault, tmp_path)
    config.match_calibration.corpus_path = str(tmp_path / "corpus.jsonl")

    cmd_done(config, "", "feed the birds", today_override="2026-06-28")

    assert Path(config.match_calibration.pending_path).exists()
    assert not Path(config.match_calibration.corpus_path).exists()


def test_no_match_already_rejected_is_not_recaptured(tmp_path: Path) -> None:
    """A no-match suggestion the operator already REJECTED is not re-surfaced
    (recorded, not re-asked) — the capture path consults the glossary."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Feed the cat"}],
    })
    config = _config(vault, tmp_path)
    config.match_calibration.corpus_path = str(tmp_path / "corpus.jsonl")
    # Operator previously rejected the (feed the birds → Feed the cat) suggestion.
    mc.append_corpus(config.match_calibration.corpus_path, mc.MatchCorpusEntry(
        type=mc.CORPUS_REJECT,
        query_key=mc.query_key("feed the birds"),
        item_text="Feed the cat",
    ))

    import structlog
    with structlog.testing.capture_logs() as cap:
        cmd_done(config, "", "feed the birds", today_override="2026-06-28")

    assert mc.load_pending(config.match_calibration.pending_path) == []
    assert [
        c for c in cap
        if c.get("event") == "routine.match_calibration.no_match_already_rejected"
    ]


def test_no_match_capture_emits_captured_log(tmp_path: Path) -> None:
    """Observability pin: a surfaced no-match suggestion emits the
    ``no_match_captured`` event with the candidate + score."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Feed the cat"}],
    })
    config = _config(vault, tmp_path)

    import structlog
    with structlog.testing.capture_logs() as cap:
        cmd_done(config, "", "feed the birds", today_override="2026-06-28")

    captured = [
        c for c in cap
        if c.get("event") == "routine.match_calibration.no_match_captured"
    ]
    assert len(captured) == 1
    assert captured[0]["candidate"] == "Feed the cat"
    assert captured[0]["query"] == "feed the birds"
