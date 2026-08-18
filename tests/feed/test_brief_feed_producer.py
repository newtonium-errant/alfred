"""Brief → feed extractor pins (Feed Phase A producer #2, 3 clean sections).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from alfred.brief.feed_producer import (
    event_feed_items,
    health_feed_items,
    peer_digest_feed_items,
)
from alfred.feed import FeedStore


# --- health: one item per ATTENTION-status tool ------------------------------


def _write_bit(vault: Path, date_str: str = "2026-07-30") -> None:
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / f"Alfred BIT {date_str}.md").write_text(
        "---\ntype: run\n---\n\n"
        "## Summary\n"
        "[OK] curator  (120 ms)\n"
        "[WARN] surveyor  (92 ms) — ollama 404\n"
        "[FAIL] janitor — boom\n"
        "## Detail\n",
        encoding="utf-8",
    )


def _write_bit_lines(vault: Path, summary: str, date_str: str = "2026-07-30") -> None:
    """Write a BIT record whose ``## Summary`` block is exactly ``summary``.

    Replaces any existing record so a test can advance the vault to "the next
    morning's BIT" without leaving the previous day's file to win the
    latest-record lookup.
    """
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    for old in run.glob("Alfred BIT *.md"):
        old.unlink()
    (run / f"Alfred BIT {date_str}.md").write_text(
        "---\ntype: run\n---\n\n## Summary\n" + summary + "## Detail\n",
        encoding="utf-8",
    )


# KAL-LE's real BIT shape, measured 2026-08-03: tool_counts
# {ok: 5, warn: 0, fail: 0, skip: 7}, overall_status "skip". The 7 skipped tools
# are unconfigured on that instance, so this is its STEADY state, not a bad day.
_KALLE_SUMMARY = (
    "[OK] curator  (120 ms)\n"
    "[OK] janitor  (88 ms)\n"
    "[OK] distiller  (95 ms)\n"
    "[OK] brief  (40 ms)\n"
    "[OK] surveyor  (150 ms)\n"
    "[SKIP] talker\n"
    "[SKIP] mail\n"
    "[SKIP] gcal\n"
    "[SKIP] weather\n"
    "[SKIP] transport\n"
    "[SKIP] scribe\n"
    "[SKIP] peer\n"
)
_KALLE_SKIPPED = ["gcal", "mail", "peer", "scribe", "talker", "transport", "weather"]


def test_health_emits_only_attention_tools(tmp_path: Path) -> None:
    _write_bit(tmp_path)
    items = health_feed_items(tmp_path, instance="salem")
    by_id = {it.id: it for it in items}
    assert set(by_id) == {"health:surveyor", "health:janitor"}  # ok curator excluded
    assert by_id["health:surveyor"].mode == "fyi"  # awareness kind
    assert "ollama 404" in by_id["health:surveyor"].title
    assert by_id["health:janitor"].evidence["status"] == "fail"


# --- health: skip produces NO card (the #8 defect) ---------------------------


def test_health_skip_tools_produce_no_cards(tmp_path: Path) -> None:
    """KAL-LE's steady state (5 ok + 7 skip, nothing wrong) must card NOTHING.

    The pre-fix filter was ``if status == "ok": continue``, which carded all 7
    skipped tools as "Health: <tool> SKIP" fyi items every morning.

    Mutation: restore ``if status == "ok": continue`` → this fails with 7 items.
    """
    _write_bit_lines(tmp_path, _KALLE_SUMMARY)
    items = health_feed_items(tmp_path, instance="kalle")
    assert items == []


def test_health_skip_only_vault_is_genuine_empty(tmp_path: Path) -> None:
    """Every tool skipped → ``[]``, the genuine-empty read (NOT ``None``).

    ``[]`` and ``None`` are different contracts: ``[]`` is "I read the health
    and nothing needs attention" (caller reconciles, stale warns clear); ``None``
    is "I could not read it" (caller must not reconcile). An all-skip BIT is a
    successful read, so it owes the caller ``[]``.
    """
    _write_bit_lines(tmp_path, "[SKIP] talker\n[SKIP] mail\n")
    assert health_feed_items(tmp_path, instance="kalle") == []


def test_health_mixed_skip_and_warn_cards_only_the_warn(tmp_path: Path) -> None:
    """Skips are suppressed WITHOUT suppressing a real warn sharing the record.

    Guards the over-broad fix (dropping every non-fail, or bailing out of the
    loop on the first skip) — the warn still has to come through.
    """
    _write_bit_lines(
        tmp_path,
        "[OK] curator  (1 ms)\n[SKIP] talker\n"
        "[WARN] surveyor — ollama 404\n[SKIP] mail\n",
    )
    items = health_feed_items(tmp_path, instance="kalle")
    assert [it.id for it in items] == ["health:surveyor"]
    assert items[0].evidence["status"] == "warn"


def test_health_unknown_status_still_cards(tmp_path: Path) -> None:
    """An UNRECOGNISED status fails OPEN — it still produces a card.

    The suppression is a denylist ({ok, skip}), not an allowlist ({warn, fail}),
    so a future 5th Status value or a hand-edited record surfaces instead of
    being silently dropped. On a health surface a spurious card costs noise; a
    dropped card costs a missed outage.

    Mutation: make the predicate an allowlist (``status in {"warn", "fail"}``)
    → this fails, and nothing else in the suite does.
    """
    _write_bit_lines(tmp_path, "[OK] curator  (1 ms)\n[DEGRADED] surveyor — new status\n")
    items = health_feed_items(tmp_path, instance="kalle")
    assert [it.id for it in items] == ["health:surveyor"]
    assert items[0].evidence["status"] == "degraded"


def test_health_skip_suppression_is_logged(tmp_path: Path) -> None:
    """ILB: suppression must be greppable, not silent.

    "Why is there no health card for my skipped tool?" has to be answerable from
    the log alone — silence there is indistinguishable from a broken producer.
    """
    _write_bit_lines(tmp_path, _KALLE_SUMMARY)
    with structlog.testing.capture_logs() as captured:
        health_feed_items(tmp_path, instance="kalle")
    matches = [c for c in captured if c.get("event") == "brief.health_feed_quiet_tools"]
    assert len(matches) == 1
    assert matches[0]["count"] == 7
    assert matches[0]["tools"] == _KALLE_SKIPPED
    assert matches[0]["instance"] == "kalle"
    assert "reason" in matches[0]


def test_health_no_quiet_tools_logs_nothing(tmp_path: Path) -> None:
    """The suppression line fires only when something was actually suppressed —
    an ok-only instance (Salem's shape) must not emit a daily count=0 line."""
    _write_bit_lines(tmp_path, "[OK] curator  (1 ms)\n[WARN] surveyor — x\n")
    with structlog.testing.capture_logs() as captured:
        health_feed_items(tmp_path, instance="salem")
    assert [c for c in captured if c.get("event") == "brief.health_feed_quiet_tools"] == []


# --- health × reconcile: the second-order fix -------------------------------
#
# The point of suppressing skips is not only quieter cards — it restores the
# store-level clearing behaviour that a skip-bearing instance had lost. These
# drive the real ``FeedStore.reconcile``, because the defect lives in the
# INTERACTION (stable key = tool name) and no per-extractor pin can see it.


def _states(store: FeedStore) -> dict[str, str]:
    return {i.id: i.state for i in store._fold_from_disk().values()}


def test_stale_warn_clears_through_reconcile_on_a_skip_bearing_instance(
    tmp_path: Path,
) -> None:
    """A warn that recovers to ok goes ``acted`` even though the instance has 7
    permanent skips — i.e. the all-clear path is REACHABLE on KAL-LE.

    Pre-fix, ``out`` on that instance always carried the 7 skip cards, so the
    genuine-empty ``[]`` this extractor's contract promises was dead code there.
    """
    vault = tmp_path / "vault"
    store = FeedStore(tmp_path / "feed.jsonl")

    _write_bit_lines(vault, _KALLE_SUMMARY + "[WARN] surveyor2 — ollama 404\n")
    day1 = health_feed_items(vault, instance="kalle")
    assert [it.id for it in day1] == ["health:surveyor2"]
    store.reconcile("health", day1)
    assert _states(store)["health:surveyor2"] == "open"

    # Next morning: surveyor2 recovers. Only the 7 skips remain → genuine [].
    _write_bit_lines(vault, _KALLE_SUMMARY)
    day2 = health_feed_items(vault, instance="kalle")
    assert day2 == []
    # Authoritative: ``health_feed_items`` is a pure read of the BIT lines,
    # so an empty result is a FACT (every check ok/skip), not a failed read.
    # Brief's producer declares the same at its own call site.
    result = store.reconcile("health", day2, empty_is_authoritative=True)
    assert result["retired"] == 1
    assert _states(store)["health:surveyor2"] == "retired"


def test_warn_that_becomes_skip_clears_instead_of_staying_open(tmp_path: Path) -> None:
    """A tool going warn → skip must CLEAR its card.

    The stable key is the tool NAME, so pre-fix the id stayed in the incoming
    open set across that transition — never ABSENT, never ``acted``. The warn
    card silently mutated into a permanently-open "Health: surveyor SKIP".

    Mutation: restore ``if status == "ok": continue`` → the item stays ``open``
    and ``acted`` is 0.
    """
    vault = tmp_path / "vault"
    store = FeedStore(tmp_path / "feed.jsonl")

    _write_bit_lines(vault, "[WARN] surveyor — ollama 404\n")
    store.reconcile("health", health_feed_items(vault, instance="kalle"))
    assert _states(store)["health:surveyor"] == "open"

    # The tool is unconfigured out of the instance → its check now SKIPs.
    _write_bit_lines(vault, "[SKIP] surveyor\n")
    # Authoritative: ``health_feed_items`` is a pure read of the BIT lines,
    # so an empty result is a FACT (every check ok/skip), not a failed read.
    # Brief's producer declares the same at its own call site.
    result = store.reconcile(
        "health", health_feed_items(vault, instance="kalle"),
        empty_is_authoritative=True,
    )
    assert result["retired"] == 1
    assert _states(store)["health:surveyor"] == "retired"


def test_skip_cards_cannot_groundhog_because_none_are_created(tmp_path: Path) -> None:
    """No skip card is ever created, so none can be revived after an ack.

    ``health`` is an EPISODE kind (absent from
    ``feed.model.SNAPSHOT_FINGERPRINT_FIELDS``), so ``_revival_suppressed``
    declines to suppress and reconcile re-opens an acked item on the next fire.
    That is correct for a real warn (ok → warn → ok → warn is genuine news) but
    fatal for a PERMANENT skip: pre-fix, KAL-LE's 7 skip cards came back open
    every morning no matter how many times the operator acked them. Measured on
    the pre-fix build: ack both → next reconcile → both back to ``open``.

    An aggregate "7 tools skipped" card would NOT have fixed this — it would be
    one groundhog instead of seven.
    """
    vault = tmp_path / "vault"
    store = FeedStore(tmp_path / "feed.jsonl")

    _write_bit_lines(vault, _KALLE_SUMMARY)
    store.reconcile("health", health_feed_items(vault, instance="kalle"))
    assert _states(store) == {}  # nothing carded at all

    # A real warn appears, the operator acks it, and it stays acked while the
    # 7 skips keep re-appearing in every BIT.
    _write_bit_lines(vault, _KALLE_SUMMARY + "[WARN] surveyor2 — ollama 404\n")
    store.reconcile("health", health_feed_items(vault, instance="kalle"))
    store.set_state("health:surveyor2", "acked", action="ack")
    assert _states(store)["health:surveyor2"] == "acked"

    # Next morning, same BIT: only the acked warn is in the store, and no skip
    # card exists to be revived alongside it.
    store.reconcile("health", health_feed_items(vault, instance="kalle"))
    assert set(_states(store)) == {"health:surveyor2"}


def test_feed_and_narration_agree_on_every_status(tmp_path: Path) -> None:
    """CROSS-SURFACE DRIFT PIN: a status cards in the feed iff it is spoken by
    the narration.

    This defect existed because two surfaces each kept their OWN copy of
    ``status != "ok"`` — the feed's health cards and ``narration._health_text``.
    Both were skip-blind, and fixing either alone would have left the other
    telling the operator that 7 unconfigured tools need a look. Both now call
    ``health_section.is_attention_status``; this pin fails if either re-localises
    the comparison.

    Mutation: give ``_health_text`` its own ``s.lower() != "ok"`` back → the
    ``skip`` row disagrees and this fails.
    """
    from alfred.brief.narration import _health_text

    for status in ("ok", "skip", "warn", "fail", "degraded"):
        _write_bit_lines(tmp_path, f"[{status.upper()}] surveyor — detail\n")
        carded = [it.id for it in health_feed_items(tmp_path, instance="kalle")]
        spoken = "surveyor" in _health_text([("surveyor", status, "detail")])
        assert bool(carded) == spoken, (
            f"{status}: feed carded={carded!r} but narration spoken={spoken}"
        )


def test_warn_and_fail_still_reconcile_open(tmp_path: Path) -> None:
    """Preservation pin: WARN and FAIL tools still produce OPEN cards.

    The paired half of the suppression — a fix that quieted real problems too
    would pass every skip-focused test above.
    """
    vault = tmp_path / "vault"
    store = FeedStore(tmp_path / "feed.jsonl")
    _write_bit_lines(
        vault,
        "[OK] curator  (1 ms)\n[SKIP] talker\n"
        "[WARN] surveyor — ollama 404\n[FAIL] janitor — boom\n",
    )
    items = health_feed_items(vault, instance="kalle")
    assert {it.id for it in items} == {"health:surveyor", "health:janitor"}
    store.reconcile("health", items)
    assert _states(store) == {"health:surveyor": "open", "health:janitor": "open"}


def test_health_no_record_is_none_not_empty(tmp_path: Path) -> None:
    # No BIT data at all → None (failure sentinel) so the caller does NOT
    # reconcile — a missing record must never mass-``acted`` health.
    assert health_feed_items(tmp_path, instance="salem") is None


def test_health_all_ok_is_empty_list(tmp_path: Path) -> None:
    # Record present, every tool ok → [] (genuine empty) so reconcile marks any
    # stale warns acted.
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "Alfred BIT 2026-07-30.md").write_text(
        "---\ntype: run\n---\n\n## Summary\n[OK] curator  (1 ms)\n[OK] janitor  (1 ms)\n## Detail\n",
        encoding="utf-8",
    )
    assert health_feed_items(tmp_path, instance="salem") == []


# --- event: stable key = date + name (translation-focused) ------------------


def test_event_feed_items_translation(tmp_path: Path, monkeypatch) -> None:
    from alfred.brief.upcoming_events import _UpcomingItem

    def _fake_collect(vault_path, today, max_days, prefs=None):
        return ([
            _UpcomingItem(date_iso="2026-08-01", name="Payroll due", location=None, description=None),
            _UpcomingItem(date_iso="2026-08-03", name="Vet", location=None, description=None, rec_type="task"),
        ], 0)

    monkeypatch.setattr("alfred.brief.upcoming_events._collect_items", _fake_collect)

    class _Cfg:
        max_days_ahead = 30

    items = event_feed_items(_Cfg(), tmp_path, __import__("datetime").date(2026, 7, 30), instance="salem")
    ids = {it.id for it in items}
    assert ids == {"event:2026-08-01|Payroll due", "event:2026-08-03|Vet"}
    assert all(it.kind == "event" and it.mode == "fyi" for it in items)


# --- peer_digest: stable key = peer + date ----------------------------------


def _write_peer_digest(vault: Path, peer: str, today_iso: str, body: str = "body") -> None:
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / f"Peer Digest {peer}.md").write_text(
        f"---\ntype: run\nsource: peer\npeer: {peer}\ncreated: {today_iso}\nreceived_at: {today_iso}T09:00:00+00:00\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_peer_digest_feed_items(tmp_path: Path) -> None:
    _write_peer_digest(tmp_path, "kalle", "2026-07-30")
    items = peer_digest_feed_items(tmp_path, "2026-07-30", instance="salem")
    assert len(items) == 1
    assert items[0].id == "peer_digest:kalle|2026-07-30"
    assert items[0].mode == "fyi"


def test_peer_digest_none_today_is_empty(tmp_path: Path) -> None:
    _write_peer_digest(tmp_path, "kalle", "2026-07-29")  # yesterday
    assert peer_digest_feed_items(tmp_path, "2026-07-30", instance="salem") == []


def test_peer_digest_body_verbatim_when_small(tmp_path: Path) -> None:
    """A short digest carries its body verbatim in evidence, truncated=False —
    the card renders readable content instead of {peer, date} alone."""
    _write_peer_digest(tmp_path, "kalle", "2026-07-30", body="Shipped X.\nBlocked on Y.")
    items = peer_digest_feed_items(tmp_path, "2026-07-30", instance="salem")
    assert len(items) == 1
    ev = items[0].evidence
    assert ev["body"] == "Shipped X.\nBlocked on Y."
    assert ev["truncated"] is False


def test_peer_digest_body_bounded_and_marked_when_oversized(tmp_path: Path) -> None:
    """An oversized digest is hard-truncated at the cap, carries the honest
    marker, and sets truncated=True (bounded so the append-only feed store fold
    stays cheap)."""
    big = "z" * 5000
    _write_peer_digest(tmp_path, "kalle", "2026-07-30", body=big)
    items = peer_digest_feed_items(tmp_path, "2026-07-30", instance="salem")
    assert len(items) == 1
    ev = items[0].evidence
    assert ev["truncated"] is True
    assert ev["body"].startswith("z" * 4000)
    assert ev["body"].endswith("…[truncated]")
    # Bounded: cap chars + the fixed marker, nothing more.
    assert len(ev["body"]) == 4000 + len("\n\n…[truncated]")


def test_peer_digest_stable_key_unchanged_with_body(tmp_path: Path) -> None:
    """Adding body to evidence must NOT shift the id — an already-ack'd digest
    (keyed peer+date) has to stay ack'd across the change."""
    _write_peer_digest(tmp_path, "kalle", "2026-07-30", body="x" * 9000)
    items = peer_digest_feed_items(tmp_path, "2026-07-30", instance="salem")
    assert items[0].id == "peer_digest:kalle|2026-07-30"  # id is peer+date only
