"""ops_notable — the delta predicate (operator-approved v1, 2026-08-11).

A notable is a metric that moved in the CONCERNING DIRECTION since the previous
render. The assertion this file exists to defend is the negative one: steady
state — including steady-BAD — produces no card. Re-carding a standing problem
every morning is the undismissable-SKIP incident, and the operator cannot
dismiss his way out of it.

Every "no card" assertion is paired with the card-producing case in the same
test, so none of them can pass against a producer that emits nothing at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from alfred.brief.ops_notable import (
    KEY_CURATOR_FAILURES,
    KEY_JANITOR_CRITICAL,
    KEY_QUARANTINE_RISE,
    OpsBaseline,
    load_baseline,
    ops_notable_feed_items,
)


def _run(tmp_path: Path, **kw):
    return ops_notable_feed_items(
        tmp_path / "data", tmp_path / "vault", tmp_path / "baseline.json",
        instance="salem", **kw,
    )


def _quarantine(tmp_path: Path, n: int) -> None:
    """Put ``n`` freshly-written spam records in the rolling window."""
    d = tmp_path / "vault" / "quarantine" / "spam" / "2026-08"
    d.mkdir(parents=True, exist_ok=True)
    for existing in d.glob("*.md"):
        existing.unlink()
    for i in range(n):
        (d / f"spam-{i}.md").write_text("---\ntype: email\n---\n", encoding="utf-8")


def _write_state(tmp_path: Path, name: str, payload: dict) -> None:
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def _janitor(tmp_path: Path, critical: int) -> None:
    _write_state(tmp_path, "janitor_state.json", {
        "sweeps": {"s1": {"timestamp": "2026-08-11T09:00:00", "issues_by_severity": {
            "CRITICAL": critical, "WARNING": 2,
        }}},
    })


def _curator(tmp_path: Path, failed_files: int = 0, failure_kind: str | None = None) -> None:
    payload: dict = {"failed_attempts": {f"f{i}.md": 2 for i in range(failed_files)}}
    if failure_kind is not None:
        payload["last_agent_failure"] = {"kind": failure_kind, "ts": "2026-08-11T09:00:00"}
        payload["last_run"] = "2026-08-10T09:00:00"  # failure NEWER → still active
    _write_state(tmp_path, "curator_state.json", payload)


# --- first render -----------------------------------------------------------


def test_first_render_records_a_baseline_and_emits_nothing(tmp_path: Path) -> None:
    """With no prior observation every nonzero metric would look "new", and the
    operator would get a flood describing history rather than news.

    Positive control: the SECOND render, with a genuine rise, does card — so
    this cannot pass against a producer that never emits.
    """
    _quarantine(tmp_path, 5)
    _janitor(tmp_path, 3)
    assert _run(tmp_path) == []
    assert load_baseline(tmp_path / "baseline.json").recorded is True

    _quarantine(tmp_path, 9)
    items = _run(tmp_path)
    assert [it.id for it in items] == [f"ops_notable:{KEY_QUARANTINE_RISE}"]


def test_an_unreadable_baseline_suppresses_rather_than_floods(tmp_path: Path) -> None:
    """A baseline we cannot parse is a baseline we do not have — same
    conservative direction as the first render, not "everything is new"."""
    _quarantine(tmp_path, 4)
    (tmp_path / "baseline.json").write_text("{ this is not json", encoding="utf-8")
    assert _run(tmp_path) == []


# --- (a) a count that rose when rising is bad -------------------------------


def test_quarantine_rise_cards_but_steady_and_falling_do_not(tmp_path: Path) -> None:
    """The full direction matrix in one test: up = notable, flat = not,
    down = not."""
    _quarantine(tmp_path, 2)
    _run(tmp_path)  # baseline

    _quarantine(tmp_path, 6)  # UP
    items = _run(tmp_path)
    assert [it.id for it in items] == [f"ops_notable:{KEY_QUARANTINE_RISE}"]
    assert items[0].evidence["previous"] == 2
    assert items[0].evidence["current"] == 6
    assert items[0].evidence["delta"] == 4

    assert _run(tmp_path) == []  # FLAT — same 6, no longer news

    _quarantine(tmp_path, 1)  # DOWN — improvement is not an anomaly
    assert _run(tmp_path) == []


# --- (b) an error count that is nonzero AND newly so ------------------------


def test_steady_bad_is_not_notable_but_becoming_bad_is(tmp_path: Path) -> None:
    """THE load-bearing negative. A janitor sitting on CRITICAL issues for a
    month is not news today; it was news the morning it turned. Re-carding it
    daily is the undismissable-card incident.
    """
    _janitor(tmp_path, 0)
    _run(tmp_path)  # baseline: clean

    _janitor(tmp_path, 4)  # became bad → notable
    items = _run(tmp_path)
    assert [it.id for it in items] == [f"ops_notable:{KEY_JANITOR_CRITICAL}"]
    assert items[0].evidence["previous"] == 0
    assert items[0].evidence["current"] == 4

    # STAYS bad, and even gets WORSE — still not re-carded. The count is an
    # error count, not a rising-is-bad metric; it was notable when it turned.
    _janitor(tmp_path, 9)
    assert _run(tmp_path) == []

    # Recovers, then turns again → notable once more, SAME stable key.
    _janitor(tmp_path, 0)
    assert _run(tmp_path) == []
    _janitor(tmp_path, 1)
    again = _run(tmp_path)
    assert [it.id for it in again] == [f"ops_notable:{KEY_JANITOR_CRITICAL}"]


def test_curator_failures_card_on_becoming_nonzero(tmp_path: Path) -> None:
    _curator(tmp_path, failed_files=0)
    _run(tmp_path)
    _curator(tmp_path, failed_files=3)
    items = _run(tmp_path)
    assert [it.id for it in items] == [f"ops_notable:{KEY_CURATOR_FAILURES}"]
    assert items[0].evidence["current"] == 3
    # Steady → silent.
    assert _run(tmp_path) == []


# --- (c) a first-seen error kind --------------------------------------------


def test_a_failure_kind_cards_once_ever_and_a_new_kind_cards_again(
    tmp_path: Path,
) -> None:
    """"First-seen" means first-seen EVER, so a kind that recurs months later is
    not new. A genuinely different kind is."""
    _curator(tmp_path)
    _run(tmp_path)  # baseline, no failure

    _curator(tmp_path, failure_kind="quota_limited")
    items = _run(tmp_path)
    assert [it.id for it in items] == ["ops_notable:agent_failure_kind|quota_limited"]

    # Same kind again → already seen, not news.
    assert _run(tmp_path) == []

    # A DIFFERENT kind → its own card, its own key.
    _curator(tmp_path, failure_kind="auth")
    items = _run(tmp_path)
    assert [it.id for it in items] == ["ops_notable:agent_failure_kind|auth"]


def test_a_recovered_failure_is_not_a_new_kind(tmp_path: Path) -> None:
    """Mirrors the BIT recovery rule: a failure at-or-before the last success is
    history. Positive control: the same kind while ACTIVE does card."""
    _curator(tmp_path)
    _run(tmp_path)
    _write_state(tmp_path, "curator_state.json", {
        "last_agent_failure": {"kind": "quota_limited", "ts": "2026-08-10T09:00:00"},
        "last_run": "2026-08-11T09:00:00",  # success AFTER the failure → recovered
    })
    assert _run(tmp_path) == []

    _curator(tmp_path, failure_kind="quota_limited")  # active
    assert [it.id for it in _run(tmp_path)] == [
        "ops_notable:agent_failure_kind|quota_limited"
    ]


# --- shape / contract -------------------------------------------------------


def test_items_are_fyi_and_keyed_by_source_not_ordinal(tmp_path: Path) -> None:
    _quarantine(tmp_path, 1)
    _janitor(tmp_path, 0)
    _curator(tmp_path, failed_files=0)
    _run(tmp_path)

    _quarantine(tmp_path, 5)
    _janitor(tmp_path, 2)
    _curator(tmp_path, failed_files=1)
    items = _run(tmp_path)
    assert len(items) == 3
    assert all(it.kind == "ops_notable" for it in items)
    assert all(it.mode == "fyi" and it.attention == "fyi" for it in items)
    # Keys name their SOURCE, so the same anomaly dedups across renders rather
    # than accreting one card per morning.
    assert {it.id for it in items} == {
        f"ops_notable:{KEY_QUARANTINE_RISE}",
        f"ops_notable:{KEY_JANITOR_CRITICAL}",
        f"ops_notable:{KEY_CURATOR_FAILURES}",
    }
    assert not any(it.id[-1].isdigit() and "|" not in it.id for it in items)


def test_unreadable_source_returns_none_not_empty(tmp_path: Path, monkeypatch) -> None:
    """Failure ≠ emptiness (the house extractor contract): a read failure must
    NOT reconcile, or a blip would mass-``acted`` the whole kind.

    The failure is injected rather than staged on disk. A first attempt swapped
    the spam directory for a FILE, which does NOT raise — ``Path.rglob`` on a
    non-directory yields nothing — so the test passed only because the producer
    returned an empty list, proving nothing. The reachable production failure is
    a PermissionError mid-walk, which is what this raises.

    Positive control: the readable case returns a LIST.
    """
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    quarantine_root = tmp_path / "vault" / "quarantine" / "spam"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    assert isinstance(_run(tmp_path), list)

    real_rglob = Path.rglob

    def boom(self, pattern):
        if "quarantine" in str(self):
            raise PermissionError(13, "Permission denied")
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", boom)
    assert _run(tmp_path) is None


def test_baseline_load_is_schema_tolerant_both_directions(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({
        "quarantine_week": 3, "recorded": True, "future_metric": 99,
    }), encoding="utf-8")
    loaded = load_baseline(p)
    assert loaded.quarantine_week == 3
    assert loaded.recorded is True
    assert not hasattr(loaded, "future_metric")
    # A malformed kinds list must not crash the loader.
    p.write_text(json.dumps({"seen_agent_failure_kinds": "auth"}), encoding="utf-8")
    assert load_baseline(p).seen_agent_failure_kinds == []


def test_baseline_round_trips(tmp_path: Path) -> None:
    from alfred.brief.ops_notable import save_baseline

    p = tmp_path / "b.json"
    save_baseline(p, OpsBaseline(
        quarantine_week=2, janitor_critical=1, curator_failed_files=0,
        seen_agent_failure_kinds=["auth", "auth", "other"], recorded=True,
    ))
    back = load_baseline(p)
    assert back.quarantine_week == 2
    assert back.janitor_critical == 1
    assert back.seen_agent_failure_kinds == ["auth", "other"]  # deduped + sorted


# --- ILB --------------------------------------------------------------------


def test_nothing_notable_says_so_in_the_log_not_the_feed(tmp_path: Path) -> None:
    """The ruling puts the quiet signal in the LOG: a card reading "nothing to
    report" is exactly the noise the delta predicate exists to avoid.

    Positive control: a render that DOES card must not emit the quiet line, or
    it carries no signal.
    """
    from structlog.testing import capture_logs

    _quarantine(tmp_path, 2)
    _run(tmp_path)

    with capture_logs() as quiet:
        items = _run(tmp_path)
    assert items == []
    matches = [c for c in quiet if c.get("event") == "ops_notable.nothing_notable"]
    assert len(matches) == 1, quiet
    assert matches[0]["quarantine_week"] == 2

    _quarantine(tmp_path, 7)
    with capture_logs() as noisy:
        assert _run(tmp_path)
    assert not [c for c in noisy if c.get("event") == "ops_notable.nothing_notable"]


def test_the_first_render_announces_that_it_is_a_baseline(tmp_path: Path) -> None:
    """Otherwise "no ops cards on day one" is indistinguishable from a producer
    that never ran."""
    from structlog.testing import capture_logs

    _quarantine(tmp_path, 3)
    with capture_logs() as captured:
        _run(tmp_path)
    matches = [c for c in captured if c.get("event") == "ops_notable.baseline_recorded"]
    assert len(matches) == 1, captured
    assert matches[0]["instance"] == "salem"


# --- the recovery predicate is SHARED, not mirrored (reviewer-g2 fold) -------
#
# `_active_agent_failure_kind` used to implement its own `last_run >= fail_ts`
# on the RAW STRINGS under a docstring saying it "mirrors the BIT recovery
# rule" — a citation that behaved as a copy. It was the THIRD spelling of
# "recovered" in the tree, and it agreed with the real rule only while every
# timestamp shared one ISO spelling.
#
# These are the disagreeing pairs, MEASURED by driving both functions on the
# same inputs across the three forms this codebase tolerates. Uniform-spelling
# pairs always agreed, which is why the divergence stayed invisible: curator
# writes `datetime.now(timezone.utc).isoformat()` everywhere, so production
# only ever produced the agreeing cases. The pin uses the pairs that BITE —
# the ones that appear the day any writer changes timestamp format.

_SAME_INSTANT = [
    # (fail_ts, last_run) — identical instants, mixed spellings. The old string
    # compare called each of these STILL-FAILING because "Z" (0x5A) sorts above
    # "+" (0x2B) and above the naive form's end-of-string.
    ("2026-08-15T00:31:00Z", "2026-08-15T00:31:00+00:00"),
    ("2026-08-15T00:31:00+00:00", "2026-08-15T00:31:00"),
    ("2026-08-15T00:31:00Z", "2026-08-15T00:31:00"),
]


def test_mixed_spelling_recovery_reads_as_recovered() -> None:
    """A success at the SAME INSTANT as the failure is a recovery, whatever
    spelling either timestamp arrived in.

    Direction matters: every one of these previously read as still-failing, so
    the bug was a STUCK ops_notable card naming a quota outage that had already
    cleared — noise rather than silence, but wrong, and it would have outlived
    the outage it described.
    """
    from alfred.brief.ops_notable import _active_agent_failure_kind

    for fail_ts, last_run in _SAME_INSTANT:
        state = {
            "last_agent_failure": {"kind": "quota_limited", "ts": fail_ts},
            "last_run": last_run,
        }
        assert _active_agent_failure_kind(state) is None, (
            f"fail_ts={fail_ts!r} last_run={last_run!r} should read as recovered"
        )


def test_mixed_spelling_active_failure_still_cards() -> None:
    """The positive control for the pin above, in the same spellings.

    Without it, "returns None" would pass just as well against a function that
    returns None for everything — which is precisely the mode a recovery
    predicate must never fail into, because it would swallow live outages.
    """
    from alfred.brief.ops_notable import _active_agent_failure_kind

    for fail_ts, last_run in _SAME_INSTANT:
        # Same spellings, but the success is a full day BEFORE the failure.
        state = {
            "last_agent_failure": {"kind": "quota_limited", "ts": fail_ts},
            "last_run": last_run.replace("2026-08-15", "2026-08-14"),
        }
        assert _active_agent_failure_kind(state) == "quota_limited", (
            f"fail_ts={fail_ts!r} last_run={last_run!r} should still be active"
        )


def test_ops_notable_asks_the_one_canonical_recovery_predicate() -> None:
    """The fold itself: this module must CONSUME the shared predicate rather
    than re-spell it. Patching the canonical helper has to change this
    function's answer — if it does not, a fourth spelling has grown back.
    """
    from unittest.mock import patch

    from alfred.brief import ops_notable

    state = {
        "last_agent_failure": {"kind": "auth", "ts": "2026-08-15T00:31:00+00:00"},
        "last_run": "2026-08-14T00:00:00+00:00",  # genuinely still failing
    }
    assert ops_notable._active_agent_failure_kind(state) == "auth"

    with patch.object(ops_notable, "failure_superseded_by_success", return_value=True):
        assert ops_notable._active_agent_failure_kind(state) is None
