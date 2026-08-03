"""Self-correcting matcher — Phase 1 capture pins.

Covers the ``routine.match_calibration`` pending sink (append/load + schema
tolerance) and the ``cmd_done`` capture hook (low-confidence fuzzy match →
pending row; high-confidence → none; the no-silent-mutation guardrail: the
match path writes ONLY to the pending sink).
"""

from __future__ import annotations

import json
from datetime import date
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


# ---------------------------------------------------------------------------
# Review filter — the reject-suppression gap (screenshot round 2026-08-03)
# ---------------------------------------------------------------------------
#
# The pending sink is append-only with no prune API, and the surfacing path
# never consulted the corpus. So an operator reject wrote a verdict the MATCHER
# honoured while the REVIEW CARD came back every morning forever (the deck's
# revival-after-acted resurrected it each day). These pin the filter that
# closes the read side.


TODAY = date(2026, 8, 3)


def _pending(
    query: str = "Clean hammer",
    matched_to: str = "Fully Clean House",
    *,
    record: str = "Sundays",
    captured_at: str = "2026-08-02T12:00:00+00:00",
    kind: str = mc.KIND_NO_MATCH,
    confidence: float = 0.33,
) -> mc.PendingMatch:
    return mc.PendingMatch(
        query=query, matched_to=matched_to, record=record,
        confidence=confidence, captured_at=captured_at, kind=kind,
    )


def _glossary(*entries: mc.MatchCorpusEntry) -> mc.Glossary:
    g = mc.Glossary(confirmed=set(), rejected=set(), aliases={})
    for e in entries:
        pair = (e.query_key, e.item_text)
        if e.type == mc.CORPUS_REJECT:
            g.rejected.add(pair)
        elif e.type == mc.CORPUS_ALIAS:
            g.aliases[e.query_key] = e.item_text
            g.confirmed.add(pair)
        else:
            g.confirmed.add(pair)
    return g


def test_filter_keeps_unresolved_fresh_row() -> None:
    kept, stats = mc.filter_pending_for_review(
        [_pending()], _glossary(), today=TODAY,
    )
    assert len(kept) == 1
    assert (stats.captured, stats.surfaced, stats.suppressed()) == (1, 1, 0)


def test_filter_drops_row_the_operator_rejected() -> None:
    """REGRESSION (2026-08-03): "Clean hammer → Fully Clean House" was re-dealt
    daily despite a daily NO swipe. The reject reached the corpus; the surfacing
    path just never read it.

    Mutation: drop the ``glossary.verdict`` check → this fails."""
    entry = _pending()
    g = _glossary(mc.MatchCorpusEntry(
        type=mc.CORPUS_REJECT,
        query_key=mc.query_key(entry.query),
        item_text=entry.matched_to,
    ))
    kept, stats = mc.filter_pending_for_review([entry], g, today=TODAY)
    assert kept == []
    assert (stats.resolved, stats.surfaced) == (1, 0)


def test_filter_drops_row_the_operator_confirmed() -> None:
    entry = _pending(kind=mc.KIND_LOW_CONF)
    g = _glossary(mc.MatchCorpusEntry(
        type=mc.CORPUS_CONFIRM,
        query_key=mc.query_key(entry.query),
        item_text=entry.matched_to,
    ))
    kept, stats = mc.filter_pending_for_review([entry], g, today=TODAY)
    assert kept == []
    assert stats.resolved == 1


def test_filter_drops_row_whose_query_was_aliased_elsewhere() -> None:
    """An alias resolves the PHRASE, not just the pair. Once the operator has
    said "Clean hammer means Tidy Shed", re-asking "did you mean Fully Clean
    House?" for the same phrase is the same groundhog — the pair never
    matched, so a pair-only check would miss it.

    Mutation: drop the ``alias_for`` check → this fails."""
    entry = _pending(matched_to="Fully Clean House")
    g = _glossary(mc.MatchCorpusEntry(
        type=mc.CORPUS_ALIAS,
        query_key=mc.query_key("Clean hammer"),
        item_text="Tidy Shed",
    ))
    kept, stats = mc.filter_pending_for_review([entry], g, today=TODAY)
    assert kept == []
    assert stats.resolved == 1


def test_filter_retires_rows_older_than_max_age() -> None:
    """The screenshot's row was a 3-week-old no_match at 0.33 confidence still
    being asked about daily."""
    old = _pending(captured_at="2026-07-06T12:00:00+00:00")  # 28 days back
    kept, stats = mc.filter_pending_for_review(
        [old], _glossary(), today=TODAY, max_age_days=21,
    )
    assert kept == []
    assert (stats.aged_out, stats.surfaced) == (1, 0)


def test_filter_age_boundary_is_inclusive_of_the_limit_day() -> None:
    """Exactly max_age_days old still surfaces; one day older does not."""
    at_limit = _pending(captured_at="2026-07-13T12:00:00+00:00")  # 21 days
    past = _pending(captured_at="2026-07-12T12:00:00+00:00")      # 22 days
    kept_at, _ = mc.filter_pending_for_review(
        [at_limit], _glossary(), today=TODAY, max_age_days=21,
    )
    kept_past, _ = mc.filter_pending_for_review(
        [past], _glossary(), today=TODAY, max_age_days=21,
    )
    assert len(kept_at) == 1
    assert kept_past == []


def test_filter_falls_back_to_completion_date_when_captured_at_absent() -> None:
    entry = mc.PendingMatch(
        query="q", matched_to="m", record="r", confidence=0.4,
        completion_date="2026-07-01", captured_at="",
    )
    kept, stats = mc.filter_pending_for_review(
        [entry], _glossary(), today=TODAY, max_age_days=21,
    )
    assert kept == []
    assert stats.aged_out == 1


def test_filter_fails_open_on_an_undateable_row() -> None:
    """A row we can't date is KEPT — never silently retire something whose age
    is unknown. Mutation: default the parse failure to "old" → this fails."""
    entry = mc.PendingMatch(
        query="q", matched_to="m", record="r", confidence=0.4,
        completion_date="not-a-date", captured_at="garbage",
    )
    kept, stats = mc.filter_pending_for_review(
        [entry], _glossary(), today=TODAY, max_age_days=1,
    )
    assert len(kept) == 1
    assert stats.aged_out == 0


def test_filter_caps_the_days_list_fifo_oldest_first() -> None:
    """FIFO (ruled 2026-08-03): the cap keeps the OLDEST unresolved rows, so
    nothing can be starved out of existence.

    This pin INVERTED from its original form — it previously asserted
    ``["q2", "q3"]`` (newest-wins). The change is the ruling, not drift.

    Mutation: revert to ``kept[-max_items:]`` → this fails."""
    rows = [
        _pending(query=f"q{i}", captured_at=f"2026-08-0{i}T12:00:00+00:00")
        for i in range(1, 4)
    ]
    kept, stats = mc.filter_pending_for_review(
        rows, _glossary(), today=TODAY, max_items=2,
    )
    assert [k.query for k in kept] == ["q1", "q2"]
    assert (stats.capped, stats.surfaced) == (1, 2)


def test_fifo_cap_never_starves_a_row_out_of_existence() -> None:
    """The reason FIFO was ruled: with newest-wins, the oldest rows sit below
    the cut every single day and expire unseen. Under FIFO the oldest are
    exactly the ones surfaced, so the backlog drains from the front.

    Reproduces the reviewer's construction (15 rows, cap 10) and asserts the
    survivors are the OLDEST ten — under the old rule they were the newest ten
    and rows q1..q5 would never have been shown before aging out."""
    rows = [
        _pending(query=f"q{i:02d}", captured_at=f"2026-08-01T{i:02d}:00:00+00:00")
        for i in range(1, 16)
    ]
    kept, stats = mc.filter_pending_for_review(
        rows, _glossary(), today=TODAY, max_items=10,
    )
    assert [k.query for k in kept] == [f"q{i:02d}" for i in range(1, 11)]
    assert (stats.capped, stats.surfaced) == (5, 10)


def test_filter_stats_account_for_every_captured_row() -> None:
    """surfaced + suppressed == captured, always — the ILB line has to add up."""
    resolved = _pending(query="resolved")
    aged = _pending(query="aged", captured_at="2026-01-01T12:00:00+00:00")
    fresh_a, fresh_b = _pending(query="a"), _pending(query="b")
    g = _glossary(mc.MatchCorpusEntry(
        type=mc.CORPUS_REJECT,
        query_key=mc.query_key("resolved"),
        item_text="Fully Clean House",
    ))
    _, stats = mc.filter_pending_for_review(
        [resolved, aged, fresh_a, fresh_b], g, today=TODAY, max_items=1,
    )
    assert stats.captured == 4
    assert (stats.resolved, stats.aged_out, stats.capped) == (1, 1, 1)
    assert stats.surfaced == 1
    assert stats.surfaced + stats.suppressed() == stats.captured


# ---------------------------------------------------------------------------
# Conflict rule — alias then rejects on the SAME pair (Salem's real corpus)
# ---------------------------------------------------------------------------
#
# Salem's live corpus for "clean hammer" holds, in order: a 2026-07-31
# match_alias {query_key "clean hammer", item_text "Fully Clean House",
# record "Sundays"} — a first-contact-day right-swipe recorded before verb
# stamping existed, contrary to the operator's stated intent — then
# match_reject rows on 08-01/02/03 for the SAME pair. These pin what that
# sequence resolves to.

REAL_QKEY = "clean hammer"
REAL_ITEM = "Fully Clean House"


def _alias_then_rejects(path: Path) -> None:
    """Salem's real row order: one alias, then three rejects of the same pair."""
    mc.append_corpus(path, mc.MatchCorpusEntry(
        type=mc.CORPUS_ALIAS, query_key=REAL_QKEY, item_text=REAL_ITEM,
        record="Sundays", action_at="2026-07-31T15:00:00+00:00",
    ))
    for day in ("01", "02", "03"):
        mc.append_corpus(path, mc.MatchCorpusEntry(
            type=mc.CORPUS_REJECT, query_key=REAL_QKEY, item_text=REAL_ITEM,
            record="Sundays", action_at=f"2026-08-{day}T15:00:00+00:00",
        ))


def test_later_reject_beats_earlier_alias(tmp_path: Path) -> None:
    """CONFLICT RULE: later-verdict-wins per (query_key, item_text).

    The operator's alias was a mis-swipe; the three rejects that followed are
    the real intent. If the alias could win, the matcher would auto-match a
    future "clean hammer" to "Fully Clean House" — a false-positive WRITE
    against a routine record."""
    corpus = tmp_path / "corpus.jsonl"
    _alias_then_rejects(corpus)
    g = mc.load_glossary(corpus)
    assert g.verdict(REAL_QKEY, REAL_ITEM) == "reject"
    assert (REAL_QKEY, REAL_ITEM) not in g.confirmed
    assert (REAL_QKEY, REAL_ITEM) in g.rejected


def test_reject_retracts_the_alias_it_contradicts(tmp_path: Path) -> None:
    """A reject withdraws an alias pointing at the rejected item.

    ``verdict`` was always correct here, so the MATCHER never mis-fired. But
    the alias map outlived the reject, and ``alias_for`` is consulted by the
    review filter — a stale alias would silently resolve OTHER captures of the
    same phrase the operator never ruled on.

    Mutation: drop the ``del aliases[...]`` line → this fails."""
    corpus = tmp_path / "corpus.jsonl"
    _alias_then_rejects(corpus)
    g = mc.load_glossary(corpus)
    assert g.alias_for(REAL_QKEY) is None


def test_reject_of_another_item_leaves_the_alias_standing(tmp_path: Path) -> None:
    """The retraction is surgical: rejecting a DIFFERENT pair for the same
    phrase does not withdraw an alias to some other item."""
    corpus = tmp_path / "corpus.jsonl"
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_ALIAS, query_key=REAL_QKEY, item_text="Tidy Shed",
    ))
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_REJECT, query_key=REAL_QKEY, item_text=REAL_ITEM,
    ))
    g = mc.load_glossary(corpus)
    assert g.alias_for(REAL_QKEY) == "Tidy Shed"
    assert g.verdict(REAL_QKEY, REAL_ITEM) == "reject"


def test_later_alias_beats_earlier_reject(tmp_path: Path) -> None:
    """Later-verdict-wins runs both directions — a re-alias after a reject
    restores the pair (the operator changed their mind back)."""
    corpus = tmp_path / "corpus.jsonl"
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_REJECT, query_key=REAL_QKEY, item_text=REAL_ITEM,
    ))
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_ALIAS, query_key=REAL_QKEY, item_text=REAL_ITEM,
    ))
    g = mc.load_glossary(corpus)
    assert g.verdict(REAL_QKEY, REAL_ITEM) == "confirm"
    assert g.alias_for(REAL_QKEY) == REAL_ITEM


def test_filter_suppresses_on_the_real_alias_then_rejects_shape(
    tmp_path: Path,
) -> None:
    """END-TO-END on Salem's actual row shape: the pending row for
    "Clean hammer" must drop out of the review list given the real corpus
    (alias 07-31, rejects 08-01/02/03), with the REAL field orientation
    (matched_to "Fully Clean House", record "Sundays")."""
    corpus = tmp_path / "corpus.jsonl"
    _alias_then_rejects(corpus)
    entry = mc.PendingMatch(
        query="Clean hammer", matched_to="Fully Clean House", record="Sundays",
        confidence=0.33, captured_at="2026-07-16T12:00:00+00:00",
        kind=mc.KIND_NO_MATCH,
    )
    kept, stats = mc.filter_pending_for_review(
        [entry], mc.load_glossary(corpus), today=date(2026, 8, 4),
        max_age_days=90,  # isolate the resolved path from the age path
    )
    assert kept == []
    assert stats.resolved == 1
    assert stats.aged_out == 0


def test_alias_on_one_query_does_not_suppress_a_different_query(
    tmp_path: Path,
) -> None:
    """The alias check is keyed on the ROW's own query, not on "any alias
    exists". An alias for "clean hammer" must not resolve a pending row for an
    unrelated phrase the operator has never ruled on.

    Driven through ``load_glossary`` (the production loader) rather than a
    hand-built Glossary, so the pin covers the real load path — a loader change
    that leaked aliases across keys would slip past a hand-built fixture.

    Mutation: key the alias check on anything other than the row's own
    query_key → this fails."""
    corpus = tmp_path / "corpus.jsonl"
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_ALIAS,
        query_key=mc.query_key("Clean hammer"),
        item_text="Fully Clean House",
    ))
    unrelated = mc.PendingMatch(
        query="walk doggo", matched_to="Walk dog", record="Daily",
        confidence=0.40, captured_at="2026-08-02T12:00:00+00:00",
    )
    kept, stats = mc.filter_pending_for_review(
        [unrelated], mc.load_glossary(corpus), today=TODAY,
    )
    assert [k.query for k in kept] == ["walk doggo"]
    assert (stats.resolved, stats.surfaced) == (0, 1)
