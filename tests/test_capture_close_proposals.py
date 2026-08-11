"""Capture-born task propose-close: matcher + proposal policy (#64).

THE PIN THAT MATTERS MOST is `test_the_motivating_case_scores_above_threshold`.
A fulfilment matcher that cannot recognise the case it was built for is worth
nothing, and this one nearly shipped that way: with a Jaccard token score the
workout-plan promise scored 0.40 against its own evidence, below the 0.5 bar.
Coverage (overlap-coefficient) scores the same pair 0.67. The pin exists so a
future "simplification" back to Jaccard fails loudly instead of quietly
turning the feature off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import structlog

from alfred.daily_sync import capture_close_match as ccm
from alfred.daily_sync import capture_close_proposals as ccp

#: The operator's actual words, 2026-08-05.
PROMISE = "I'm going to attach some screenshots of workout plans"
#: A record name of the shape that actually fulfilled it.
EVIDENCE = "Louka Workout Plan"

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)


def candidate(**over) -> ccp.CloseCandidate:
    base = dict(
        task_path="task/attach-screenshots-of-workout-plans.md",
        task_text=PROMISE,
        evidence_path="source/Louka Workout Plan.md",
        evidence_name=EVIDENCE,
        score=0.67,
        match_source="similarity",
    )
    base.update(over)
    return ccp.CloseCandidate(**base)


# ---------------------------------------------------------------------------
# The matcher — the motivating case first
# ---------------------------------------------------------------------------


def test_the_motivating_case_scores_above_threshold():
    """THE REGRESSION PIN. Jaccard scored this pair 0.40 (below the bar);
    coverage scores it 0.67. If this drops below the threshold the feature is
    off for the exact case it exists to catch."""
    score = ccm._similarity(PROMISE, EVIDENCE)
    assert score >= ccm.DEFAULT_CONFIDENCE_THRESHOLD, (
        f"the workout-plan case scores {score:.3f}, below the "
        f"{ccm.DEFAULT_CONFIDENCE_THRESHOLD} bar — the feature would not fire "
        "on the incident that motivated it"
    )


def test_best_match_picks_the_right_record_among_distractors():
    best = ccm.best_match(PROMISE, [
        ("note/Push Day A.md", "Push Day A"),
        ("source/Louka Workout Plan.md", EVIDENCE),
        ("note/Groceries.md", "Groceries"),
    ])
    assert best is not None
    assert best.evidence_name == EVIDENCE
    assert best.source == "similarity"


@pytest.mark.parametrize("name,band", [
    (EVIDENCE, "card"),          # the motivating case
    ("Workout Plans", "card"),   # full coverage
    ("Plan", "near"),            # single-token coincidence — evidence, not a card
    ("Push Day A", "drop"),
    ("Groceries", "drop"),
])
def test_the_three_bands(name: str, band: str):
    """Card / near-miss / dropped. The near-miss band is where the evidence
    for 'the threshold is wrong' accumulates without spending a card."""
    s = ccm._similarity(PROMISE, name)
    if band == "card":
        assert s >= ccm.DEFAULT_CONFIDENCE_THRESHOLD
    elif band == "near":
        assert ccm.DEFAULT_NO_MATCH_FLOOR <= s < ccm.DEFAULT_CONFIDENCE_THRESHOLD
    else:
        assert s < ccm.DEFAULT_NO_MATCH_FLOOR


def test_a_single_shared_token_can_never_reach_the_threshold():
    """The false-positive guard, as a property rather than one example: a
    record whose name is one common word must not spend a card on any task
    that happens to mention it."""
    assert ccm._SINGLE_TOKEN_DAMP < ccm.DEFAULT_CONFIDENCE_THRESHOLD
    for one_word in ("Plan", "Screenshots", "Workout"):
        assert ccm._similarity(PROMISE, one_word) < ccm.DEFAULT_CONFIDENCE_THRESHOLD


def test_the_floor_is_below_the_threshold():
    """Otherwise the near-miss band is empty and the matcher can never learn
    that its bar is too high."""
    assert ccm.DEFAULT_NO_MATCH_FLOOR < ccm.DEFAULT_CONFIDENCE_THRESHOLD


# --- the key ---------------------------------------------------------------


def test_rephrasings_collapse_to_one_key():
    """A verdict given on one phrasing must generalise, or the glossary learns
    a verdict per sentence and never fires twice."""
    assert ccm.query_key(PROMISE) == ccm.query_key(
        "attach workout plan screenshots")


def test_contraction_fragments_do_not_pollute_the_key():
    """Stripping punctuation turns "I'm" into "i m"; the stray "m" survived
    into the key before the stopword list covered fragments."""
    assert " m " not in f" {ccm.query_key(PROMISE)} "
    assert "m" not in ccm.query_key(PROMISE).split()


def test_plurals_fold():
    assert ccm._stem("plans") == "plan"
    assert ccm._stem("screenshots") == "screenshot"
    assert ccm._stem("stories") == "story"
    # ...without mangling words that merely end in s.
    assert ccm._stem("class") == "class"


def test_an_empty_side_scores_zero():
    assert ccm._similarity("", EVIDENCE) == 0.0
    assert ccm._similarity(PROMISE, "") == 0.0


# ---------------------------------------------------------------------------
# The glossary — how being wrong makes it better
# ---------------------------------------------------------------------------


def test_a_confirm_promotes_a_pair_to_certainty():
    g = ccm.Glossary()
    g.confirmed.add((ccm.query_key(PROMISE), ccm.normalize("Plan")))
    best = ccm.best_match(PROMISE, [("n/Plan.md", "Plan")], glossary=g)
    assert best is not None and best.score == 1.0 and best.source == "glossary"


def test_a_reject_excludes_a_pair_outright():
    g = ccm.Glossary()
    g.rejected.add((ccm.query_key(PROMISE), ccm.normalize(EVIDENCE)))
    assert ccm.best_match(
        PROMISE, [("s/x.md", EVIDENCE)], glossary=g) is None


def test_the_corpus_replays_later_verdict_wins(tmp_path):
    path = tmp_path / "corpus.jsonl"
    key = ccm.query_key(PROMISE)
    for verdict in (ccm.VERDICT_REJECTED, ccm.VERDICT_CONFIRMED):
        ccm.append_corpus(path, ccm.MatchCorpusEntry(
            ts=ccm.now_iso(), task_key=key, task_text=PROMISE,
            evidence_name=EVIDENCE, verdict=verdict, score=0.67,
        ))
    g = ccm.load_glossary(path)
    assert g.verdict(key, EVIDENCE) == ccm.VERDICT_CONFIRMED

    ccm.append_corpus(path, ccm.MatchCorpusEntry(
        ts=ccm.now_iso(), task_key=key, task_text=PROMISE,
        evidence_name=EVIDENCE, verdict=ccm.VERDICT_REJECTED, score=0.67,
    ))
    assert ccm.load_glossary(path).verdict(key, EVIDENCE) == ccm.VERDICT_REJECTED


def test_a_corrupt_corpus_line_costs_one_pair_not_the_matcher(tmp_path):
    path = tmp_path / "corpus.jsonl"
    ccm.append_corpus(path, ccm.MatchCorpusEntry(
        ts=ccm.now_iso(), task_key="k", task_text="t",
        evidence_name="E", verdict=ccm.VERDICT_CONFIRMED))
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    with structlog.testing.capture_logs() as captured:
        rows = ccm.iter_corpus(path)
    assert len(rows) == 1
    assert [c for c in captured
            if c.get("event") == "daily_sync.capture_close.corpus_rows_skipped"]


def test_near_misses_round_trip(tmp_path):
    path = tmp_path / "pending.jsonl"
    ccm.append_pending(path, ccm.PendingMatch(
        ts=ccm.now_iso(), task_path="task/x.md", task_key="k",
        task_text=PROMISE, evidence_path="n/Plan.md",
        evidence_name="Plan", score=0.45))
    rows = ccm.load_pending(path)
    assert len(rows) == 1 and rows[0]["evidence_name"] == "Plan"


# ---------------------------------------------------------------------------
# The proposal queue + policy
# ---------------------------------------------------------------------------


def test_a_candidate_above_threshold_raises_one_pending_proposal(tmp_path):
    q = tmp_path / "queue.jsonl"
    raised = ccp.maybe_propose_closes(
        q, [candidate()], threshold=0.5, window_days=7, max_proposals=3,
        now=NOW)
    assert len(raised) == 1
    p = raised[0]
    assert p.state == ccp.STATE_PENDING
    assert p.task_path.endswith("attach-screenshots-of-workout-plans.md")
    # Evidence FROZEN on the row — he must answer the question he was asked.
    assert p.task_text == PROMISE
    assert p.evidence_name == EVIDENCE
    assert p.score == 0.67
    assert ccp.list_pending(q) == [p] or len(ccp.list_pending(q)) == 1


def test_below_threshold_never_becomes_a_card(tmp_path):
    q = tmp_path / "queue.jsonl"
    with structlog.testing.capture_logs() as captured:
        raised = ccp.maybe_propose_closes(
            q, [candidate(score=0.42)], threshold=0.5, window_days=7,
            max_proposals=3, now=NOW)
    assert raised == []
    assert ccp.list_pending(q) == []
    reasons = [c.get("reason") for c in captured
               if c.get("event") == ccp.TRIGGER_EVENT]
    assert ccp.REASON_BELOW_THRESHOLD in reasons


def test_one_pending_proposal_per_task(tmp_path):
    """Two cards asking the same question are two chances to answer it
    differently."""
    q = tmp_path / "queue.jsonl"
    ccp.maybe_propose_closes(q, [candidate()], threshold=0.5, window_days=7,
                             max_proposals=3, now=NOW)
    with structlog.testing.capture_logs() as captured:
        again = ccp.maybe_propose_closes(
            q, [candidate()], threshold=0.5, window_days=7, max_proposals=3,
            now=NOW + timedelta(days=1))
    assert again == []
    assert len(ccp.list_pending(q)) == 1
    assert ccp.REASON_PENDING_EXISTS in [
        c.get("reason") for c in captured if c.get("event") == ccp.TRIGGER_EVENT]


def test_an_accepted_task_is_never_proposed_again(tmp_path):
    q = tmp_path / "queue.jsonl"
    raised = ccp.maybe_propose_closes(q, [candidate()], threshold=0.5,
                                      window_days=7, max_proposals=3, now=NOW)
    assert ccp.resolve_proposal(q, raised[0].proposal_id, ccp.STATE_ACCEPTED,
                                resolved_at=NOW.isoformat()) is True
    with structlog.testing.capture_logs() as captured:
        again = ccp.maybe_propose_closes(
            q, [candidate()], threshold=0.5, window_days=7, max_proposals=3,
            now=NOW + timedelta(days=30))
    assert again == []
    assert ccp.REASON_ALREADY_ACCEPTED in [
        c.get("reason") for c in captured if c.get("event") == ccp.TRIGGER_EVENT]


def test_a_rejection_starts_a_per_task_cooldown(tmp_path):
    q = tmp_path / "queue.jsonl"
    raised = ccp.maybe_propose_closes(q, [candidate()], threshold=0.5,
                                      window_days=7, max_proposals=3, now=NOW)
    ccp.resolve_proposal(q, raised[0].proposal_id, ccp.STATE_REJECTED,
                         resolved_at=NOW.isoformat())

    blocked = ccp.maybe_propose_closes(
        q, [candidate()], threshold=0.5, window_days=7, max_proposals=3,
        now=NOW + timedelta(days=3))
    assert blocked == []

    allowed = ccp.maybe_propose_closes(
        q, [candidate()], threshold=0.5, window_days=7, max_proposals=3,
        now=NOW + timedelta(days=8))
    assert len(allowed) == 1


def test_the_cooldown_is_PER_TASK_not_global(tmp_path):
    """A rejection is a statement about ONE task's evidence. Silencing every
    other task on the strength of it would be the machine over-reading a
    narrow answer — the one place this deliberately differs from the demotion
    queue's per-kind cooldown."""
    q = tmp_path / "queue.jsonl"
    a = candidate(task_path="task/a.md")
    b = candidate(task_path="task/b.md")
    raised = ccp.maybe_propose_closes(q, [a], threshold=0.5, window_days=7,
                                      max_proposals=3, now=NOW)
    ccp.resolve_proposal(q, raised[0].proposal_id, ccp.STATE_REJECTED,
                         resolved_at=NOW.isoformat())

    other = ccp.maybe_propose_closes(
        q, [b], threshold=0.5, window_days=7, max_proposals=3,
        now=NOW + timedelta(days=1))
    assert len(other) == 1 and other[0].task_path == "task/b.md"


def test_a_later_rejection_restarts_the_clock(tmp_path):
    q = tmp_path / "queue.jsonl"
    first = ccp.maybe_propose_closes(q, [candidate()], threshold=0.5,
                                     window_days=7, max_proposals=3, now=NOW)
    ccp.resolve_proposal(q, first[0].proposal_id, ccp.STATE_REJECTED,
                         resolved_at=NOW.isoformat())
    second = ccp.maybe_propose_closes(
        q, [candidate()], threshold=0.5, window_days=7, max_proposals=3,
        now=NOW + timedelta(days=8))
    ccp.resolve_proposal(q, second[0].proposal_id, ccp.STATE_REJECTED,
                         resolved_at=(NOW + timedelta(days=8)).isoformat())
    assert ccp.maybe_propose_closes(
        q, [candidate()], threshold=0.5, window_days=7, max_proposals=3,
        now=NOW + timedelta(days=12)) == []


def test_the_per_run_budget_bounds_the_cards_and_says_what_it_suppressed(tmp_path):
    """The deck is a scarce surface. A first run over a long backlog must not
    raise dozens at once — but the suppressed count has to be visible, because
    each one is a promise still going unnoticed."""
    q = tmp_path / "queue.jsonl"
    cands = [candidate(task_path=f"task/t{i}.md") for i in range(7)]
    with structlog.testing.capture_logs() as captured:
        raised = ccp.maybe_propose_closes(
            q, cands, threshold=0.5, window_days=7, max_proposals=3, now=NOW)
    assert len(raised) == 3
    budget = [c for c in captured
              if c.get("event") == ccp.TRIGGER_EVENT
              and c.get("reason") == ccp.REASON_BUDGET_SPENT]
    assert len(budget) == 1
    assert budget[0]["suppressed"] == 4
    assert budget[0]["raised"] == 3


def test_no_candidates_still_logs_the_trigger(tmp_path):
    """Intentionally-left-blank: the common healthy day. Without this line
    'nothing to propose' is indistinguishable from 'the pass stopped
    running'."""
    q = tmp_path / "queue.jsonl"
    with structlog.testing.capture_logs() as captured:
        assert ccp.maybe_propose_closes(
            q, [], threshold=0.5, window_days=7, max_proposals=3, now=NOW) == []
    events = [c for c in captured if c.get("event") == ccp.TRIGGER_EVENT]
    assert len(events) == 1
    assert events[0]["reason"] == ccp.REASON_NO_CANDIDATES
    assert not q.exists()          # nothing written on a quiet day


# --- queue mechanics -------------------------------------------------------


def test_resolve_is_order_preserving_and_in_place(tmp_path):
    q = tmp_path / "queue.jsonl"
    ids = []
    for i in range(3):
        r = ccp.maybe_propose_closes(
            q, [candidate(task_path=f"task/t{i}.md")], threshold=0.5,
            window_days=7, max_proposals=3, now=NOW + timedelta(seconds=i))
        ids.append(r[0].proposal_id)
    assert ccp.resolve_proposal(q, ids[1], ccp.STATE_ACCEPTED,
                                resolved_at=NOW.isoformat()) is True
    rows = ccp.iter_proposals(q)
    assert [r.proposal_id for r in rows] == ids       # order preserved
    assert rows[1].state == ccp.STATE_ACCEPTED
    assert rows[1].resolved_at == NOW.isoformat()
    assert rows[0].state == ccp.STATE_PENDING


def test_resolving_an_unknown_id_or_bad_state_is_false(tmp_path):
    q = tmp_path / "queue.jsonl"
    ccp.maybe_propose_closes(q, [candidate()], threshold=0.5, window_days=7,
                             max_proposals=3, now=NOW)
    assert ccp.resolve_proposal(q, "cc-nope", ccp.STATE_ACCEPTED,
                                resolved_at=NOW.isoformat()) is False
    assert ccp.resolve_proposal(q, "cc-nope", "banana",
                                resolved_at=NOW.isoformat()) is False


def test_find_proposal(tmp_path):
    q = tmp_path / "queue.jsonl"
    raised = ccp.maybe_propose_closes(q, [candidate()], threshold=0.5,
                                      window_days=7, max_proposals=3, now=NOW)
    assert ccp.find_proposal(q, raised[0].proposal_id) is not None
    assert ccp.find_proposal(q, "cc-absent") is None


def test_the_queue_is_schema_tolerant(tmp_path):
    """A row from a newer build loads without its extra field; one that cannot
    be placed is declined rather than taking the reader down."""
    q = tmp_path / "queue.jsonl"
    good = {"proposal_id": "cc-1", "ts": NOW.isoformat(),
            "state": "pending", "task_path": "task/a.md",
            "future_field": "from a newer build"}
    bad_state = {"proposal_id": "cc-2", "ts": "", "state": "banana",
                 "task_path": "task/b.md"}
    no_id = {"proposal_id": "", "ts": "", "state": "pending",
             "task_path": "task/c.md"}
    no_task = {"proposal_id": "cc-4", "ts": "", "state": "pending",
               "task_path": ""}
    import json
    q.write_text("\n".join(json.dumps(r) for r in
                           (good, bad_state, no_id, no_task)) + "\n",
                 encoding="utf-8")
    with structlog.testing.capture_logs() as captured:
        rows = ccp.iter_proposals(q)
    assert [r.proposal_id for r in rows] == ["cc-1"]
    assert [c for c in captured
            if c.get("event") == "daily_sync.capture_close.rows_skipped"]


def test_proposal_ids_are_stable_and_distinct(tmp_path):
    a = ccp.make_proposal_id("task/a.md", "s/e.md", NOW.isoformat())
    again = ccp.make_proposal_id("task/a.md", "s/e.md", NOW.isoformat())
    later = ccp.make_proposal_id(
        "task/a.md", "s/e.md", (NOW + timedelta(days=8)).isoformat())
    assert a == again          # same triple → same id
    assert a != later          # a re-proposal after cooldown is a NEW question
    assert a.startswith("cc-")


def test_a_missing_queue_reads_as_empty(tmp_path):
    assert ccp.iter_proposals(tmp_path / "absent.jsonl") == []
    assert ccp.list_pending(tmp_path / "absent.jsonl") == []
    assert ccp.cooldown_until(tmp_path / "absent.jsonl", "task/a.md", 7) is None


def test_an_unparseable_resolved_at_does_not_block_forever(tmp_path):
    """A row this build cannot place in time must not silence the question
    permanently — he can decline again."""
    q = tmp_path / "queue.jsonl"
    raised = ccp.maybe_propose_closes(q, [candidate()], threshold=0.5,
                                      window_days=7, max_proposals=3, now=NOW)
    ccp.resolve_proposal(q, raised[0].proposal_id, ccp.STATE_REJECTED,
                         resolved_at="not-a-timestamp")
    assert ccp.cooldown_until(q, candidate().task_path, 7) is None
