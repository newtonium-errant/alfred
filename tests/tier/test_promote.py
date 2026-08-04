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


# --- B2: approval + routine-record promotion write (the mutation) ----------

import frontmatter  # noqa: E402


def _routine_items(vault: Path, record: str) -> list:
    post = frontmatter.load(str(vault / "routine" / f"{record}.md"))
    return post.metadata.get("items") or []


def _seed_pending(cfg, *, first="2026-05-01", last="2026-05-22", done_days=4,
                  text="Rake leaves") -> str:  # span 21 / 3 gaps = 7 → snaps to weekly
    pid = promote.proposal_id(promote.query_key(text))
    promote.append_pending(cfg.pending_path, promote.RecurrenceProposal(
        proposal_id=pid, query_key=promote.query_key(text), sample_text=text,
        done_days=done_days, window_days=30, first_seen=first, last_seen=last))
    return pid


def test_infer_cadence_snaps_to_common() -> None:
    def _p(first, last, n):
        return promote.RecurrenceProposal(proposal_id="x", query_key="k", sample_text="t",
                                          done_days=n, window_days=30, first_seen=first, last_seen=last)
    assert promote.infer_cadence_days(_p("2026-05-01", "2026-05-08", 2)) == 7     # 7/1 → 7
    assert promote.infer_cadence_days(_p("2026-05-01", "2026-05-29", 5)) == 7     # 28/4=7 → 7
    assert promote.infer_cadence_days(_p("2026-05-01", "2026-05-03", 3)) == 1     # 2/2=1 → 1
    assert promote.infer_cadence_days(_p("bad", "x", 3)) == 7                     # fallback


def test_append_promoted_item_creates_soft_cadence_item(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    result = promote.append_promoted_item(v, "Recurring Chores", text="Rake leaves", cadence_days=7)
    assert result == "appended"
    items = _routine_items(v, "Recurring Chores")
    assert len(items) == 1
    it = items[0]
    assert it["text"] == "Rake leaves" and it["priority"] == "aspirational"
    assert it["target_cadence_days"] == 7 and "due_pattern" not in it   # soft-cadence, no deadline
    # the record is a proper routine record
    post = frontmatter.load(str(v / "routine" / "Recurring Chores.md"))
    assert post.metadata["type"] == "routine"


def test_append_promoted_item_idempotent_by_text(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    promote.append_promoted_item(v, "Recurring Chores", text="Rake leaves", cadence_days=7)
    # a differently-phrased same chore (same query_key) → duplicate, no second item
    second = promote.append_promoted_item(v, "Recurring Chores", text="rake the leaves", cadence_days=3)
    assert second == "duplicate"
    assert len(_routine_items(v, "Recurring Chores")) == 1
    assert not list((v / "routine").glob("*.promote.tmp"))   # atomic: no temp left behind


def test_append_promoted_item_preserves_existing(tmp_path: Path) -> None:
    v = tmp_path / "vault"
    (v / "routine").mkdir(parents=True)
    (v / "routine" / "Home.md").write_text(
        "---\ntype: routine\nname: Home\nitems:\n  - text: Water plants\n    priority: tracked\n---\n\nbody\n",
        encoding="utf-8")
    promote.append_promoted_item(v, "Home", text="Rake leaves", cadence_days=7)
    items = _routine_items(v, "Home")
    assert len(items) == 2
    assert {i["text"] for i in items} == {"Water plants", "Rake leaves"}   # existing item preserved


def test_approve_writes_routine_and_records_decision(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pid = _seed_pending(cfg)
    v = tmp_path / "vault"
    res = promote.approve_proposal(v, cfg, pid, routine_record="Recurring Chores", operator="andrew")
    assert res["approved"] == pid and res["write"] == "appended" and res["cadence_days"] == 7
    assert len(_routine_items(v, "Recurring Chores")) == 1
    assert promote.decided_ids(cfg.decided_path) == {pid}          # decided-row written
    decisions = promote.load_decisions(cfg.decided_path)
    assert decisions[0].decision == "approve" and decisions[0].operator == "andrew"


def test_approve_honors_explicit_cadence(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pid = _seed_pending(cfg)
    v = tmp_path / "vault"
    promote.approve_proposal(v, cfg, pid, routine_record="Chores", operator="a", cadence_days=14)
    assert _routine_items(v, "Chores")[0]["target_cadence_days"] == 14


def test_approve_core_no_junk_default_guard(tmp_path: Path) -> None:
    """#20 P5 B2 hardening: the no-junk-default refusal lives IN approve_proposal (fail-closed), NOT
    only at the CLI boundary — so a caller bypassing the CLI (e.g. B2b's reply path) inherits it. A
    blank/whitespace routine_record refuses BEFORE any write: no routine record, no decided-row."""
    cfg = _cfg(tmp_path)
    pid = _seed_pending(cfg)
    v = tmp_path / "vault"
    for rr in ("", "   "):
        res = promote.approve_proposal(v, cfg, pid, routine_record=rr, operator="a")
        assert "error" in res and "no routine target" in res["error"]
    assert not (v / "routine").exists()                        # NO routine record written
    assert promote.decided_ids(cfg.decided_path) == set()      # NO decided-row (proposal stays pending)


def test_approve_refuses_already_decided_and_non_pending(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    v = tmp_path / "vault"
    # non-pending id
    assert "error" in promote.approve_proposal(v, cfg, "trp-nope", routine_record="X", operator="a")
    assert not (v / "routine").exists()                            # no write on refusal
    # already-decided
    pid = _seed_pending(cfg)
    promote.approve_proposal(v, cfg, pid, routine_record="Chores", operator="a")
    res2 = promote.approve_proposal(v, cfg, pid, routine_record="Chores", operator="a")  # re-approve
    assert "error" in res2 and "already decided" in res2["error"]
    assert len(_routine_items(v, "Chores")) == 1                   # idempotent — no duplicate item


def test_reject_writes_only_decided_no_routine(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pid = _seed_pending(cfg)
    v = tmp_path / "vault"
    res = promote.reject_proposal(cfg, pid, operator="andrew")
    assert res["rejected"] == pid
    assert promote.decided_ids(cfg.decided_path) == {pid}
    assert not (v / "routine").exists()                            # NO routine touch on reject


def test_rejected_proposal_never_resurfaces(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    v = tmp_path / "vault"
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "operator-adhoc", d))
    first = promote.materialize_proposals(v, cfg, _TODAY)
    pid = first[0].proposal_id
    promote.reject_proposal(cfg, pid, operator="a")
    assert promote.materialize_proposals(v, cfg, _TODAY) == []      # decided → never re-surfaces


def test_aspirational_eligible_and_approve_does_not_touch_the_source(tmp_path: Path) -> None:
    """#20 P5 B2 NOTE-2: an ``aspirational`` chore IS promotion-eligible (the prime use-case), and
    approve ADDS the committed item to the TARGET routine ONLY — it never removes/mutates the source
    aspirational entry (the daily t3 rows it came from). Exactly one routine-side write, source untouched."""
    cfg = _cfg(tmp_path)
    v = tmp_path / "vault"
    dailies: dict[str, str] = {}
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "aspirational", d))   # aspirational source, no routine match
        dailies[d] = (v / "daily" / f"{d}.md").read_text()
    props = promote.materialize_proposals(v, cfg, _TODAY)
    assert len(props) == 1                                          # aspirational is eligible
    promote.approve_proposal(v, cfg, props[0].proposal_id, routine_record="Chores", operator="a")
    assert {p.name for p in (v / "routine").glob("*.md")} == {"Chores.md"}   # ONLY the target written
    for d, before in dailies.items():
        assert (v / "daily" / f"{d}.md").read_text() == before      # source aspirational entry UNTOUCHED


def test_approve_is_the_only_routine_writer(tmp_path: Path) -> None:
    """THE B2 gate pin: detection/materialize NEVER writes a routine record — only approve does."""
    cfg = _cfg(tmp_path)
    v = tmp_path / "vault"
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(v, d, _t3("Rake leaves", "operator-adhoc", d))
    promote.materialize_proposals(v, cfg, _TODAY)   # detection + surface projection
    assert not (v / "routine").exists()             # detection wrote NO routine record
    pid = promote.load_pending(cfg.pending_path)[0].proposal_id
    promote.approve_proposal(v, cfg, pid, routine_record="Chores", operator="a")
    assert len(_routine_items(v, "Chores")) == 1     # ONLY approve wrote the routine record


# --- arc #18 M4: containment at the create primitive ------------------------


def test_approve_refuses_escaping_routine_record_and_keeps_proposal_pending(
    tmp_path: Path,
) -> None:
    """Containment refusal must NOT burn the proposal.

    ``append_promoted_item`` is a CREATE primitive (no ``.exists()`` gate —
    absence is a supported branch), so an escaping ``routine_record`` was
    arbitrary-file-create. M4 gates it. The behaviour that needs pinning is what
    happens NEXT: a refused write must not record a decided-row, because the
    decided-set permanently excludes a proposal from detection — recording it
    would burn the proposal on a typo and leave no way back.

    Structurally the same contract as the no-junk-default guard above: refuse
    before any write, proposal stays PENDING, operator retries with a real name.
    """
    cfg = _cfg(tmp_path)
    pid = _seed_pending(cfg)
    v = tmp_path / "vault"
    (v / "routine").mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "escaped"

    res = promote.approve_proposal(
        v, cfg, pid, routine_record="../../escaped/Evil", operator="a",
    )

    assert "error" in res
    assert res["proposal_id"] == pid
    assert not outside.exists(), "no directories created outside the vault"
    assert promote.decided_ids(cfg.decided_path) == set(), (
        "a containment refusal must leave the proposal PENDING — recording a "
        "decision would burn it permanently"
    )


def test_approve_still_works_for_a_real_record_name(tmp_path: Path) -> None:
    """The gate is transparent to legitimate names, including the punctuation
    shapes real vault records carry."""
    cfg = _cfg(tmp_path)
    pid = _seed_pending(cfg)
    v = tmp_path / "vault"
    res = promote.approve_proposal(
        v, cfg, pid, routine_record="Recurring Bills + Admin", operator="a",
    )
    assert res.get("write") == "appended"
    assert promote.decided_ids(cfg.decided_path) == {pid}
