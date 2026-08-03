"""Self-correcting matcher — Phase 1 Daily Sync surface pins.

Covers the read-only ``routine_match`` section: renders pending low-confidence
matches when enabled, the intentionally-left-blank sentinel when enabled-empty,
omits (None) when disabled; plus the config drift-guard (the section's pending
path default MUST equal the routine tool's).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog

from alfred.daily_sync import routine_match_section as rms
from alfred.daily_sync.config import DailySyncConfig, RoutineMatchConfig
from alfred.routine import match_calibration as mc


def _cfg(
    pending_path: Path,
    *,
    enabled: bool = True,
    corpus_path: Path | None = None,
    max_age_days: int = mc.DEFAULT_PENDING_MAX_AGE_DAYS,
    max_items: int = mc.DEFAULT_PENDING_MAX_ITEMS,
) -> DailySyncConfig:
    rm = RoutineMatchConfig(
        enabled=enabled,
        pending_path=str(pending_path),
        pending_max_age_days=max_age_days,
        pending_max_items=max_items,
    )
    if corpus_path is not None:
        rm.corpus_path = str(corpus_path)
    return DailySyncConfig(enabled=True, routine_match=rm)


def _seed_pending(p: Path, *entries: mc.PendingMatch) -> None:
    for e in entries:
        mc.append_pending(p, e)


def test_disabled_omits_section(tmp_path: Path) -> None:
    out = rms.routine_match_section(
        _cfg(tmp_path / "pending.jsonl", enabled=False), date(2026, 6, 28),
    )
    assert out is None


def test_enabled_empty_emits_ilb_sentinel(tmp_path: Path) -> None:
    """Enabled but nothing to review → explicit sentinel, NOT silent omit
    (intentionally-left-blank)."""
    with structlog.testing.capture_logs() as cap:
        out = rms.routine_match_section(
            _cfg(tmp_path / "pending.jsonl"), date(2026, 6, 28),
        )
    assert out is not None
    # Markdown ``##`` header consistent with sibling sections (attribution /
    # friction / radar) — the assembler doesn't wrap titles.
    assert out.startswith("## Routine match review")
    assert "No low-confidence routine matches to review" in out
    # ILB log pinned (per feedback_log_emission_test_pattern).
    assert [c for c in cap if c.get("event") == "routine_match.no_pending"]


def test_enabled_with_pending_renders_numbered_list(tmp_path: Path) -> None:
    p = tmp_path / "pending.jsonl"
    _seed_pending(
        p,
        mc.PendingMatch(query="walk doggo", matched_to="Walk dog",
                        record="Daily", confidence=0.40, captured_at="2026-06-27T12:00:00+00:00"),
        mc.PendingMatch(query="meds", matched_to="Take meds",
                        record="Health", confidence=0.33, captured_at="2026-06-27T12:00:00+00:00"),
    )
    with structlog.testing.capture_logs() as cap:
        out = rms.routine_match_section(_cfg(p), date(2026, 6, 28), start_index=1)
    assert out is not None
    # ``##`` header with item count, matching the sibling sections.
    assert out.startswith("## Routine match review (2 items)")
    assert "walk doggo" in out and "Walk dog" in out
    assert "0.40" in out and "0.33" in out
    assert "1." in out and "2." in out
    surfaced = [c for c in cap if c.get("event") == "routine_match.surfaced"]
    assert len(surfaced) == 1 and surfaced[0]["count"] == 2


def test_start_index_offsets_numbering(tmp_path: Path) -> None:
    """Item numbering honours the assembler's global start_index so it stays
    continuous after earlier sections."""
    p = tmp_path / "pending.jsonl"
    _seed_pending(p, mc.PendingMatch(
        query="q", matched_to="m", record="R", confidence=0.2,
        captured_at="2026-06-27T12:00:00+00:00"))
    out = rms.routine_match_section(_cfg(p), date(2026, 6, 28), start_index=7)
    assert out is not None
    assert "7." in out
    # peek count drives item_count_after → next section starts at 8.
    assert rms.peek_last_batch_count() == 1


def test_register_adds_provider(tmp_path: Path) -> None:
    from alfred.daily_sync import assembler

    assembler.clear_providers()
    rms.register()
    rms.register()  # idempotent — the daemon re-registers every fire
    assert assembler.registered_providers().count("routine_match") == 1
    assembler.clear_providers()


def test_pending_path_default_matches_routine_tool() -> None:
    """Drift-guard: the Daily Sync section's pending-path dataclass default MUST
    equal the routine tool's capture default (same file — the CLI writes, the
    section reads). Both bind the shared module constant."""
    assert RoutineMatchConfig().pending_path == mc.DEFAULT_PENDING_PATH


def test_daily_sync_loads_routine_match_block() -> None:
    from alfred.daily_sync.config import load_from_unified

    cfg = load_from_unified({
        "daily_sync": {
            "enabled": True,
            "routine_match": {"enabled": True, "pending_path": "/x/p.jsonl"},
        },
    })
    assert cfg.routine_match.enabled is True
    # Explicit daily_sync override is honoured (intentional split — non-silent).
    assert cfg.routine_match.pending_path == "/x/p.jsonl"


# ---------------------------------------------------------------------------
# pending-path single-source (reviewer NOTE #2) — load-time derivation
# ---------------------------------------------------------------------------


def test_pending_path_derives_from_routine_override() -> None:
    """Single-source: when ``routine.match_calibration.pending_path`` is
    overridden and the daily_sync field is NOT, daily_sync TRACKS the routine
    override — the silent-drift surface is closed (the routine CLI writes the
    file the section reads, even under a custom path)."""
    from alfred.daily_sync.config import load_from_unified

    cfg = load_from_unified({
        "daily_sync": {"enabled": True, "routine_match": {"enabled": True}},
        "routine": {"match_calibration": {"pending_path": "/custom/pend.jsonl"}},
        "telegram": {"instance": {"name": "Salem"}},
    })
    assert cfg.routine_match.pending_path == "/custom/pend.jsonl"


def test_pending_path_explicit_daily_sync_override_wins_over_routine() -> None:
    """An explicit ``daily_sync.routine_match.pending_path`` is honoured even
    when routine sets its own — the operator's deliberate split is respected
    (not silently overwritten by the derivation)."""
    from alfred.daily_sync.config import load_from_unified

    cfg = load_from_unified({
        "daily_sync": {
            "enabled": True,
            "routine_match": {"enabled": True, "pending_path": "/ds/explicit.jsonl"},
        },
        "routine": {"match_calibration": {"pending_path": "/custom/pend.jsonl"}},
        "telegram": {"instance": {"name": "Salem"}},
    })
    assert cfg.routine_match.pending_path == "/ds/explicit.jsonl"


def test_pending_path_no_override_tracks_routine_default() -> None:
    """No overrides anywhere → daily_sync tracks the routine tool's RESOLVED
    default (which honours ``logging.dir``), still landing on the shared
    constant when log dir is the default ``./data``."""
    from alfred.daily_sync.config import load_from_unified
    from alfred.routine.config import load_from_unified as load_routine

    raw = {
        "daily_sync": {"enabled": True, "routine_match": {"enabled": True}},
        "telegram": {"instance": {"name": "Salem"}},
    }
    cfg = load_from_unified(raw)
    # Tracks routine's resolved default — single source of the resolution logic.
    assert (
        cfg.routine_match.pending_path
        == load_routine(raw).match_calibration.pending_path
    )


def test_pending_path_derive_failure_logs_debug(monkeypatch) -> None:
    """Reviewer ILB note (on 9b89cb7): a routine-config resolution failure during
    pending_path derivation must be LOGGED (debug), not silently swallowed — a
    real breakage would re-introduce read/write drift via the constant fallback,
    and silence makes it undiagnosable. The fallback still keeps daily_sync load
    from crashing (lands on the dataclass default)."""
    from alfred.daily_sync.config import load_from_unified

    def _boom(raw):  # noqa: ANN001
        raise RuntimeError("routine config boom")

    # Function-level ``from alfred.routine.config import load_from_unified`` in
    # the derivation reads the module attribute at call time → patching it here
    # makes the derivation raise.
    monkeypatch.setattr("alfred.routine.config.load_from_unified", _boom)

    with structlog.testing.capture_logs() as cap:
        cfg = load_from_unified({
            "daily_sync": {"enabled": True, "routine_match": {"enabled": True}},
        })

    # Fell back to the dataclass defaults (the shared constants) — no crash.
    assert cfg.routine_match.pending_path == mc.DEFAULT_PENDING_PATH
    assert cfg.routine_match.corpus_path == mc.DEFAULT_CORPUS_PATH
    assert cfg.routine_match.pending_max_age_days == mc.DEFAULT_PENDING_MAX_AGE_DAYS
    assert cfg.routine_match.pending_max_items == mc.DEFAULT_PENDING_MAX_ITEMS
    matches = [
        c for c in cap
        if c.get("event") == "daily_sync.routine_match.config_derive_failed"
    ]
    assert len(matches) == 1
    assert "boom" in matches[0]["error"]
    # The event now covers the whole derived set, not just pending_path — the
    # field list is part of the signal (renamed from pending_path_derive_failed).
    assert set(matches[0]["fields"]) == {
        "pending_path", "corpus_path", "pending_max_age_days", "pending_max_items",
    }


# ---------------------------------------------------------------------------
# Phase 2b — RoutineMatchItem display item + consume_last_batch (routing surface)
# ---------------------------------------------------------------------------


def test_consume_last_batch_returns_numbered_routine_match_items(tmp_path: Path) -> None:
    """After the section renders, consume_last_batch yields RoutineMatchItems
    carrying the GLOBAL item_number (start_index offset) + the captured-match
    fields — the routing surface the daemon persists for reply_dispatch."""
    p = tmp_path / "pending.jsonl"
    _seed_pending(
        p,
        mc.PendingMatch(query="walk doggo", matched_to="Walk dog",
                        record="Daily", confidence=0.40,
                        completion_date="2026-06-28", captured_at="t1"),
        mc.PendingMatch(query="meds", matched_to="Take meds",
                        record="Health", confidence=0.33, captured_at="2026-06-27T12:00:00+00:00"),
    )
    rms.routine_match_section(_cfg(p), date(2026, 6, 28), start_index=5)
    batch = rms.consume_last_batch()
    assert [i.item_number for i in batch] == [5, 6]
    assert batch[0].query == "walk doggo"
    assert batch[0].matched_to == "Walk dog"
    assert batch[0].record == "Daily"
    assert batch[0].confidence == 0.40
    assert batch[0].completion_date == "2026-06-28"
    # to_dict carries item_number so the dispatcher can route "item 5 confirm".
    d = batch[0].to_dict()
    assert d["item_number"] == 5 and d["query"] == "walk doggo"
    # consume clears the holder.
    assert rms.consume_last_batch() == []


def test_routine_match_item_from_dict_schema_tolerant() -> None:
    """from_dict drops unknown keys, defaults absent optional ones (load
    contract) — a row written by a newer/older tool version never crashes."""
    item = rms.RoutineMatchItem.from_dict({
        "item_number": 3, "query": "q", "matched_to": "m", "record": "r",
        "confidence": 0.2, "future_field": "ignored",
    })
    assert item.item_number == 3 and item.query == "q"
    assert item.completion_date == "" and item.captured_at == ""


def test_no_match_item_renders_did_you_mean(tmp_path: Path) -> None:
    """Phase 3: a no_match item renders the 'did you mean…' shape, distinct from
    the low_conf 'X → Y (conf)' shape; both carry through consume_last_batch."""
    p = tmp_path / "pending.jsonl"
    _seed_pending(
        p,
        mc.PendingMatch(query="walk doggo", matched_to="Walk dog",
                        record="Daily", confidence=0.40,  # low_conf (default)
                        captured_at="2026-06-27T12:00:00+00:00"),
        mc.PendingMatch(query="feed the birds", matched_to="Feed the cat",
                        record="Daily", confidence=0.50,
                        kind=mc.KIND_NO_MATCH, captured_at="2026-06-27T12:00:00+00:00"),
    )
    out = rms.routine_match_section(_cfg(p), date(2026, 6, 28), start_index=1)
    assert out is not None
    # low_conf shape
    assert "“walk doggo” → “Walk dog”" in out and "conf 0.40" in out
    # no_match shape — distinct, suggestion-framed, no "conf" phrasing
    assert "nothing matched — did you mean “Feed the cat”?" in out
    # kind survives into the routing surface
    batch = rms.consume_last_batch()
    assert [i.kind for i in batch] == ["low_conf", "no_match"]
    assert batch[1].to_dict()["kind"] == "no_match"


def test_disabled_clears_holder(tmp_path: Path) -> None:
    """Disabled → section omitted AND the batch holder cleared (no stale items
    leak into a later fire's persist)."""
    p = tmp_path / "pending.jsonl"
    _seed_pending(p, mc.PendingMatch(
        query="q", matched_to="m", record="R", confidence=0.2,
        captured_at="2026-06-27T12:00:00+00:00"))
    # First an enabled fire populates the holder…
    rms.routine_match_section(_cfg(p), date(2026, 6, 28))
    assert rms.peek_last_batch_count() == 1
    # …then a disabled fire must clear it.
    rms.routine_match_section(_cfg(p, enabled=False), date(2026, 6, 28))
    assert rms.peek_last_batch_count() == 0


# ---------------------------------------------------------------------------
# Reject-suppression (screenshot round 2026-08-03) — the section must not
# re-surface rows the operator already ruled on.
# ---------------------------------------------------------------------------


def _reject(query: str, item_text: str) -> mc.MatchCorpusEntry:
    return mc.MatchCorpusEntry(
        type=mc.CORPUS_REJECT,
        query_key=mc.query_key(query),
        item_text=item_text,
    )


def test_rejected_row_is_not_re_surfaced(tmp_path: Path) -> None:
    """THE SCREENSHOT BUG, end-to-end through the section.

    "Clean hammer → Fully Clean House" was re-dealt every morning despite a
    daily NO swipe: the reject wrote its corpus verdict (the matcher honoured
    it) but the review surface read the append-only pending sink and never
    consulted the corpus, so the card came back and the deck's
    revival-after-acted resurrected it.

    Mutation: revert the section to render ``pending`` instead of the filtered
    list → this fails."""
    p, corpus = tmp_path / "pending.jsonl", tmp_path / "corpus.jsonl"
    _seed_pending(p, mc.PendingMatch(
        query="Clean hammer", matched_to="Sundays",
        record="Fully Clean House", confidence=0.33,
        captured_at="2026-08-02T12:00:00+00:00", kind=mc.KIND_NO_MATCH,
    ))
    mc.append_corpus(corpus, _reject("Clean hammer", "Sundays"))

    out = rms.routine_match_section(
        _cfg(p, corpus_path=corpus), date(2026, 8, 3),
    )
    assert out is not None
    assert "Clean hammer" not in out
    # Falls through to the ILB sentinel — nothing to review is stated, not
    # silently omitted.
    assert "No low-confidence routine matches to review" in out


def test_suppressed_rows_do_not_reach_the_deck(tmp_path: Path) -> None:
    """The daemon feeds the deck from ``consume_last_batch``, so filtering in
    the section has to empty the FEED batch too — otherwise the card keeps
    being dealt even though the Daily Sync message stopped showing it."""
    p, corpus = tmp_path / "pending.jsonl", tmp_path / "corpus.jsonl"
    _seed_pending(p, mc.PendingMatch(
        query="Clean hammer", matched_to="Sundays",
        record="Fully Clean House", confidence=0.33,
        captured_at="2026-08-02T12:00:00+00:00", kind=mc.KIND_NO_MATCH,
    ))
    mc.append_corpus(corpus, _reject("Clean hammer", "Sundays"))

    rms.routine_match_section(_cfg(p, corpus_path=corpus), date(2026, 8, 3))
    assert rms.consume_last_batch() == []


def test_unresolved_row_still_surfaces_alongside_a_rejected_one(
    tmp_path: Path,
) -> None:
    """The filter is surgical: only the ruled-on row drops out."""
    p, corpus = tmp_path / "pending.jsonl", tmp_path / "corpus.jsonl"
    _seed_pending(
        p,
        mc.PendingMatch(query="Clean hammer", matched_to="Sundays",
                        record="Fully Clean House", confidence=0.33,
                        captured_at="2026-08-02T12:00:00+00:00"),
        mc.PendingMatch(query="walk doggo", matched_to="Walk dog",
                        record="Daily", confidence=0.40,
                        captured_at="2026-08-02T12:00:00+00:00"),
    )
    mc.append_corpus(corpus, _reject("Clean hammer", "Sundays"))

    out = rms.routine_match_section(
        _cfg(p, corpus_path=corpus), date(2026, 8, 3), start_index=1,
    )
    assert out is not None
    assert out.startswith("## Routine match review (1 item)")
    assert "walk doggo" in out
    assert "Clean hammer" not in out


def test_suppression_is_logged_with_its_reasons(tmp_path: Path) -> None:
    """ILB (feedback_log_emission_test_pattern): a shrinking review list must be
    explicable — "already ruled on" vs "the section broke" must be greppable.

    Mutation: drop the ``pending_suppressed`` log → this fails."""
    p, corpus = tmp_path / "pending.jsonl", tmp_path / "corpus.jsonl"
    _seed_pending(
        p,
        mc.PendingMatch(query="Clean hammer", matched_to="Sundays",
                        record="Fully Clean House", confidence=0.33,
                        captured_at="2026-08-02T12:00:00+00:00"),
        mc.PendingMatch(query="ancient", matched_to="Old thing",
                        record="Daily", confidence=0.2,
                        captured_at="2026-01-01T12:00:00+00:00"),
    )
    mc.append_corpus(corpus, _reject("Clean hammer", "Sundays"))

    with structlog.testing.capture_logs() as cap:
        rms.routine_match_section(
            _cfg(p, corpus_path=corpus), date(2026, 8, 3),
        )
    matches = [c for c in cap if c.get("event") == "routine_match.pending_suppressed"]
    assert len(matches) == 1
    ev = matches[0]
    assert ev["captured"] == 2
    assert ev["surfaced"] == 0
    assert ev["resolved"] == 1
    assert ev["aged_out"] == 1
    assert ev["corpus_path"] == str(corpus)


def test_no_suppression_emits_no_suppression_log(tmp_path: Path) -> None:
    """The quiet path stays quiet — the log fires on actual suppression, so it
    stays a signal rather than daily noise."""
    p = tmp_path / "pending.jsonl"
    _seed_pending(p, mc.PendingMatch(
        query="walk doggo", matched_to="Walk dog", record="Daily",
        confidence=0.40, captured_at="2026-08-02T12:00:00+00:00",
    ))
    with structlog.testing.capture_logs() as cap:
        rms.routine_match_section(
            _cfg(p, corpus_path=tmp_path / "absent.jsonl"), date(2026, 8, 3),
        )
    assert [c for c in cap if c.get("event") == "routine_match.pending_suppressed"] == []


def test_missing_corpus_file_surfaces_everything(tmp_path: Path) -> None:
    """No corpus yet (fresh instance) → empty glossary → nothing suppressed.
    The filter must never swallow the review list just because the operator
    hasn't ruled on anything yet."""
    p = tmp_path / "pending.jsonl"
    _seed_pending(p, mc.PendingMatch(
        query="walk doggo", matched_to="Walk dog", record="Daily",
        confidence=0.40, captured_at="2026-08-02T12:00:00+00:00",
    ))
    out = rms.routine_match_section(
        _cfg(p, corpus_path=tmp_path / "nope.jsonl"), date(2026, 8, 3),
    )
    assert out is not None
    assert "walk doggo" in out
