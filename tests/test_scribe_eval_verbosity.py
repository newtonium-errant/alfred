"""#14 slice 14d-i — the eval words-per-claim verbosity axis (RECORDED-ONLY, Q2).

Pins: the per-case words_per_claim metric (+ div-by-zero), the corpus DISTRIBUTION over cases WITH
claims only, the zero-claim EXCLUSION + explicit excluded-count (ILB), the recorded-only default
(no verdict), the DORMANT pass/fail path exercised only on an explicit target (so it can't bit-rot),
the co-visible render with the completeness axes + the Q2 recorded-only note, and report-only (the
axis carries no gate). All eval-harness-only — no note-gen/pipeline/grounding touch.
"""

from __future__ import annotations

from alfred.scribe.eval.scorecard import aggregate, render_scorecard_md
from alfred.scribe.eval.scoring import AxisScore, CaseScore


def _cs(cid, words, claims):
    a = AxisScore(axis="fabrication", scored=True, passed=True, detail="")
    return CaseScore(case_id=cid, title=cid, primary_axis="fabrication",
                     fabrication=a, wrong_drug=a, missed_mh=a,
                     grounding_flag_count=0, speaker_flag_count=0,
                     word_count=words, claim_count=claims)


def _card(cases, **kw):
    return aggregate(cases, mode="synthetic", model="test", **kw)


# --- per-case metric --------------------------------------------------------

def test_words_per_claim_metric():
    assert _cs("a", 30, 3).words_per_claim == 10.0
    assert _cs("b", 100, 4).words_per_claim == 25.0


def test_words_per_claim_zero_claims_is_zero_not_crash():
    assert _cs("d", 5, 0).words_per_claim == 0.0            # degenerate → 0.0, no ZeroDivisionError


# --- corpus distribution + zero-claim exclusion -----------------------------

def test_distribution_over_with_claim_cases():
    # wpc: a=10, b=25, c=10 ; d (zero-claim) EXCLUDED.
    card = _card([_cs("a", 30, 3), _cs("b", 100, 4), _cs("c", 10, 1), _cs("d", 5, 0)])
    assert card.mean_words_per_claim == 15.0               # (10+25+10)/3
    assert card.median_words_per_claim == 10.0
    assert card.p90_words_per_claim == 25.0                # the tail
    assert card.words_per_claim_cases == 3 and card.zero_claim_cases == 1


def test_zero_claim_cases_do_not_skew_the_mean():
    # WITHOUT the exclusion, adding a 0-claim case would drag the mean toward 0. It must not.
    base = _card([_cs("a", 50, 2), _cs("b", 50, 2)])       # both 25 → mean 25
    with_zero = _card([_cs("a", 50, 2), _cs("b", 50, 2), _cs("z", 0, 0)])
    assert base.mean_words_per_claim == 25.0 and with_zero.mean_words_per_claim == 25.0
    assert with_zero.zero_claim_cases == 1


def test_all_zero_claim_corpus_no_crash():
    card = _card([_cs("d1", 3, 0), _cs("d2", 4, 0)])
    assert card.mean_words_per_claim == 0.0 and card.words_per_claim_cases == 0
    assert card.zero_claim_cases == 2                       # every case surfaced as excluded


# --- recorded-only default + dormant pass/fail activation -------------------

def test_recorded_only_default_has_no_verdict():
    card = _card([_cs("a", 30, 3), _cs("b", 100, 4)])
    assert card.succinctness_target is None and card.verbose_rate is None   # Q2 — no bar


def test_dormant_pass_fail_activates_only_on_explicit_target():
    cases = [_cs("a", 30, 3), _cs("b", 100, 4), _cs("c", 10, 1)]   # wpc 10, 25, 10
    card = _card(cases, succinctness_target=20.0)
    assert card.succinctness_target == 20.0
    assert abs(card.verbose_rate - (1 / 3)) < 1e-9         # only b=25 > 20 → 1/3
    # a tighter target catches more
    assert _card(cases, succinctness_target=5.0).verbose_rate == 1.0        # all > 5


def test_target_not_auto_pulled_from_profile_25():
    # Q2 decouple: the DEFAULT aggregate never applies a bar (would smuggle the guessed 25 back in).
    card = _card([_cs("a", 300, 1)])                        # 300 w/claim — wildly over any guess
    assert card.verbose_rate is None                       # NO implicit target ⇒ NO verdict


# --- render: co-visible, recorded-only note, ILB excluded, per-case column --

def test_render_recorded_only_and_co_visible():
    md = render_scorecard_md(_card([_cs("a", 30, 3), _cs("b", 100, 4), _cs("d", 5, 0)]))
    assert "Words / atomic claim" in md
    assert "RECORDED-ONLY, no pass/fail target set (Q2)" in md   # never read as a gate
    assert "missed_mh" in md                                     # co-visible with the completeness axis
    assert "1** zero-claim case(s) EXCLUDED" in md               # ILB — the exclusion is explicit
    assert "configured target" not in md                        # no verdict line when target unset


def test_render_per_case_column_blanks_zero_claim():
    md = render_scorecard_md(_card([_cs("a", 30, 3), _cs("d", 5, 0)]))
    assert "W/claim" in md                                       # the per-case column exists
    rows = [ln for ln in md.splitlines() if ln.startswith("| `")]
    a_row = next(r for r in rows if "`a`" in r)
    d_row = next(r for r in rows if "`d`" in r)
    assert a_row.rstrip().endswith("10.0 |")                     # with-claim shows the value
    assert d_row.rstrip().endswith("— |")                       # zero-claim shows "—", excluded-but-visible


def test_render_shows_verdict_only_when_target_configured():
    md = render_scorecard_md(_card([_cs("a", 30, 3), _cs("b", 100, 4)], succinctness_target=20.0))
    assert "configured target **20** w/claim" in md and "over target" in md
