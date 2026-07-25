"""#20 P5 B1 — ad-hoc-T3 recurrence detection + promotion-proposal store (PROPOSE-ONLY) pins.

Contract-first. The load-bearing property is NEVER-AUTO-MUTATE: detection writes ONLY the pending
queue — no ``routine/`` record, no decided row. Plus: the distinct-done-DAYS count (same day once),
the threshold, the source filter (routine-origin excluded), idempotent re-materialize (no dup), the
belt (already-in-a-routine not proposed), the existing-id exclusion, and the degrade-not-crash store.
Regression pins run UNCONDITIONALLY.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog

from alfred.tier import promote


_TODAY = date(2026, 5, 28)


def _write_daily(vault: Path, date_str: str, t3_yaml: str) -> None:
    """Write ``vault/daily/<date>.md`` with a ``tier_curation.t3`` block (the shape
    ``load_daily_curation`` parses)."""
    daily = vault / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / f"{date_str}.md").write_text(
        "---\ntier_curation:\n  t3:\n" + t3_yaml + "---\n\ndaily body\n", encoding="utf-8"
    )


def _t3(item: str, source: str, done_at: str | None) -> str:
    row = f"    - item: {item}\n      source: {source}\n"
    if done_at is not None:
        row += f"      done_at: '{done_at}'\n"
    return row


@dataclass
class _Cfg:
    pending_path: str
    decided_path: str
    threshold_done_days: int = 3
    window_days: int = 30


def _cfg(tmp_path: Path, **over) -> _Cfg:
    return _Cfg(
        pending_path=str(tmp_path / "pending.jsonl"),
        decided_path=str(tmp_path / "decided.jsonl"),
        **over,
    )


# --- proposal_id + store round-trip ----------------------------------------

def test_proposal_id_deterministic_from_key() -> None:
    assert promote.proposal_id("leaves rake") == promote.proposal_id("leaves rake")
    assert promote.proposal_id("leaves rake") != promote.proposal_id("wash car")
    assert promote.proposal_id("leaves rake").startswith("trp-")


def test_pending_store_roundtrip_and_degrades(tmp_path: Path) -> None:
    p = str(tmp_path / "pending.jsonl")
    assert promote.load_pending(p) == []                       # absent → []
    prop = promote.RecurrenceProposal(
        proposal_id="trp-abc", query_key="leaves rake", sample_text="Rake leaves",
        done_days=3, window_days=30, first_seen="2026-05-01", last_seen="2026-05-20")
    promote.append_pending(p, prop)
    # a corrupt line is skipped, not fatal
    with open(p, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    rows = promote.load_pending(p)
    assert len(rows) == 1 and rows[0].proposal_id == "trp-abc" and rows[0].sample_text == "Rake leaves"


def test_decided_ids_reads_both_kinds(tmp_path: Path) -> None:
    p = str(tmp_path / "decided.jsonl")
    assert promote.decided_ids(p) == set()
    promote.append_decision(p, promote.RecurrenceDecision(proposal_id="trp-a", decision="approve", operator="andrew"))
    promote.append_decision(p, promote.RecurrenceDecision(proposal_id="trp-b", decision="reject", operator="andrew"))
    assert promote.decided_ids(p) == {"trp-a", "trp-b"}


# --- detection (the judgment path) -----------------------------------------

def test_detection_proposes_at_threshold_distinct_days(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    # "Rake leaves" done on 3 DISTINCT days (+ a same-day duplicate on one → still 3 days).
    _write_daily(v, "2026-05-10", _t3("Rake leaves", "operator-adhoc", "2026-05-10"))
    _write_daily(v, "2026-05-15", _t3("Rake leaves", "operator-adhoc", "2026-05-15")
                 + _t3("Rake leaves", "operator-adhoc", "2026-05-15"))   # dup same day
    _write_daily(v, "2026-05-20", _t3("Rake leaves", "aspirational", "2026-05-20"))
    props = promote.compute_recurrence_proposals(
        v, today=_TODAY, threshold_done_days=3, window_days=30)
    assert len(props) == 1
    p = props[0]
    assert p.done_days == 3                                    # same-day dup counted once
    assert p.query_key == promote.query_key("Rake leaves")
    assert p.first_seen == "2026-05-10" and p.last_seen == "2026-05-20"


def test_detection_below_threshold_not_proposed(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    _write_daily(v, "2026-05-10", _t3("Rake leaves", "operator-adhoc", "2026-05-10"))
    _write_daily(v, "2026-05-15", _t3("Rake leaves", "operator-adhoc", "2026-05-15"))
    props = promote.compute_recurrence_proposals(
        v, today=_TODAY, threshold_done_days=3, window_days=30)   # only 2 done-days
    assert props == []


def test_detection_excludes_routine_origin_and_undone(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    # routine-origin item done 3× → NOT eligible (already in a routine)
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Pay rent", "auto-due-routine", d))
    # an OPEN (undone) adhoc item on 3 distinct days → not a completion → no signal
    for d in ("2026-05-11", "2026-05-16", "2026-05-22"):
        _write_daily(v, d, _t3("Read a book", "operator-adhoc", None))
    props = promote.compute_recurrence_proposals(
        v, today=_TODAY, threshold_done_days=3, window_days=30)
    assert props == []


def test_detection_window_excludes_old_done(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    # two in-window + one OUTSIDE the 30d window (file date < today-30) → only 2 done-days count
    _write_daily(v, "2026-05-20", _t3("Rake leaves", "operator-adhoc", "2026-05-20"))
    _write_daily(v, "2026-05-25", _t3("Rake leaves", "operator-adhoc", "2026-05-25"))
    _write_daily(v, "2026-04-01", _t3("Rake leaves", "operator-adhoc", "2026-04-01"))  # out of window
    props = promote.compute_recurrence_proposals(
        v, today=_TODAY, threshold_done_days=3, window_days=30)
    assert props == []                                        # only 2 in-window done-days


def test_detection_excludes_existing_ids(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "operator-adhoc", d))
    pid = promote.proposal_id(promote.query_key("Rake leaves"))
    props = promote.compute_recurrence_proposals(
        v, today=_TODAY, threshold_done_days=3, window_days=30, existing_ids={pid})
    assert props == []                                        # already pending/decided → not re-proposed


def test_detection_belt_excludes_already_in_a_routine(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake the leaves", "operator-adhoc", d))
    # belt supplied — the chore already matches an active routine item → not proposed.
    props = promote.compute_recurrence_proposals(
        v, today=_TODAY, threshold_done_days=3, window_days=30,
        routine_item_texts=["Rake the leaves"])
    assert props == []


def test_detection_malformed_daily_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    (v / "daily").mkdir(parents=True)
    (v / "daily" / "2026-05-10.md").write_text("---\n: : broken yaml : :\n---\n", encoding="utf-8")
    (v / "daily" / "not-a-date.md").write_text("junk", encoding="utf-8")  # non-date stem skipped
    for d in ("2026-05-12", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "operator-adhoc", d))
    props = promote.compute_recurrence_proposals(   # must not raise
        v, today=_TODAY, threshold_done_days=3, window_days=30)
    assert len(props) == 1 and props[0].done_days == 3


def test_detection_emits_ilb_scan_log(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    _write_daily(v, "2026-05-20", _t3("Rake leaves", "operator-adhoc", "2026-05-20"))
    with structlog.testing.capture_logs() as cap:
        promote.compute_recurrence_proposals(v, today=_TODAY, threshold_done_days=3, window_days=30)
    scans = [c for c in cap if c.get("event") == "tier.recurrence.scan"]
    assert len(scans) == 1 and scans[0]["proposed"] == 0 and scans[0]["days_parsed"] == 1


# --- materialize: idempotent + NEVER auto-mutate ---------------------------

def test_materialize_appends_then_idempotent(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "operator-adhoc", d))
    cfg = _cfg(tmp_path)
    first = promote.materialize_proposals(v, cfg, _TODAY)
    assert len(first) == 1
    assert len(promote.load_pending(cfg.pending_path)) == 1
    # re-run → the cluster is already pending → NO duplicate appended
    second = promote.materialize_proposals(v, cfg, _TODAY)
    assert len(second) == 1
    assert len(promote.load_pending(cfg.pending_path)) == 1   # idempotent


def test_materialize_never_writes_a_routine_record(tmp_path: Path) -> None:
    """THE load-bearing pin: detection PROPOSES only — it writes the pending queue and NOTHING else.
    No routine/ record is created/modified; no decided row is written (that's B2)."""
    v = tmp_path / "vault"
    (v / "routine").mkdir(parents=True)   # empty routine dir — must stay empty
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "operator-adhoc", d))
    cfg = _cfg(tmp_path)
    props = promote.materialize_proposals(v, cfg, _TODAY)
    assert len(props) == 1                                    # a proposal surfaced
    assert list((v / "routine").glob("*.md")) == []           # NO routine record written
    assert not Path(cfg.decided_path).exists()                # NO decided row written (B2 only)
    assert len(promote.load_pending(cfg.pending_path)) == 1   # only the pending queue grew


def test_materialize_excludes_decided(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "operator-adhoc", d))
    cfg = _cfg(tmp_path)
    pid = promote.proposal_id(promote.query_key("Rake leaves"))
    # simulate B2 having rejected it
    promote.append_decision(cfg.decided_path, promote.RecurrenceDecision(proposal_id=pid, decision="reject"))
    props = promote.materialize_proposals(v, cfg, _TODAY)
    assert props == []                                        # decided → not surfaced, not re-proposed
    assert promote.load_pending(cfg.pending_path) == []       # and not appended to pending
